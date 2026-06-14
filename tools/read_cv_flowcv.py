"""
Log in to FlowCV, read the current CV as structured data, save to .tmp/cv_raw.json.
Also caches the rendered FlowCV HTML to .tmp/cv_master.html so subsequent runs of
the pipeline can build the tailored PDF fully offline.

Default behavior: SKIP the FlowCV trip entirely if .tmp/cv_master.html already
exists — the build pipeline only needs the cache and cv_raw.json. Pass --refresh
after editing your FlowCV to force a fresh pull.

Usage:
    python3 tools/read_cv_flowcv.py             # use cache if present
    python3 tools/read_cv_flowcv.py --refresh   # force re-fetch from FlowCV

Requires: FLOWCV_EMAIL, FLOWCV_PASSWORD in .env (only when actually fetching)
Output:   .tmp/cv_raw.json, .tmp/cv_master.html, .tmp/flowcv_session.json
"""

import json
import os
import re
import sys
import asyncio
from dotenv import load_dotenv

load_dotenv()
os.makedirs(".tmp", exist_ok=True)

FLOWCV_EMAIL = os.getenv("FLOWCV_EMAIL", "")
FLOWCV_PASSWORD = os.getenv("FLOWCV_PASSWORD", "")


async def login(page) -> None:
    await page.goto("https://app.flowcv.com/login", wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    await page.locator("button", has_text="Login with Email").click()
    await page.wait_for_selector('input[name="email"]', timeout=10000)

    await page.locator('input[name="email"]').fill(FLOWCV_EMAIL)
    await page.locator('input[name="password"]').fill(FLOWCV_PASSWORD)

    await page.locator('form button[type="submit"]').click()

    try:
        await page.wait_for_function(
            "() => !window.location.pathname.includes('/login')",
            timeout=20000,
        )
    except Exception:
        body_text = await page.locator("body").inner_text()
        if "incorrect" in body_text.lower() or "invalid" in body_text.lower() or "wrong" in body_text.lower():
            raise RuntimeError("FlowCV login failed: credentials rejected")
        raise RuntimeError(f"FlowCV login did not redirect away from /login. Current URL: {page.url}")

    print(f"Logged in to FlowCV — landed on {page.url}")


async def open_first_resume(page) -> str:
    """Navigate to the editor for the first resume. Returns the resume ID."""
    await page.wait_for_load_state("networkidle", timeout=20000)
    await page.wait_for_timeout(2000)

    resume_id = await page.evaluate(
        """() => {
            const els = document.querySelectorAll('[class*="resumeid-"]');
            for (const el of els) {
                const m = el.className.match(/resumeid-([a-f0-9-]{30,})/);
                if (m) return m[1];
            }
            return null;
        }"""
    )

    if not resume_id:
        raise RuntimeError("Could not find resume ID on /resumes page")

    print(f"Resume ID: {resume_id}")
    await page.goto(f"https://app.flowcv.com/resume/{resume_id}", wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(3000)
    print(f"Opened resume editor at {page.url}")
    return resume_id


async def extract_text_by_selector(page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(timeout=3000)
            text = await el.inner_text()
            if text.strip():
                return text.strip()
        except Exception:
            continue
    return ""


async def extract_all_sections(page, resume_id: str) -> dict:
    """Extract the rendered resume content as raw text. Claude parses it downstream.
    Also snapshots the rendered .resumePage HTML + all stylesheets to
    .tmp/cv_master.html so build_tailored_pdf.py can work fully offline."""
    html_dump = await page.content()
    with open(".tmp/flowcv_page_dump.html", "w", encoding="utf-8") as f:
        f.write(html_dump)

    resume_text = ""
    try:
        resume_page = page.locator("div.resumePage").first
        await resume_page.wait_for(timeout=5000)
        resume_text = await resume_page.inner_text()
    except Exception:
        pass

    if not resume_text:
        try:
            resume_page = page.locator(f"[class*='resumeid-{resume_id}']").first
            resume_text = await resume_page.inner_text()
        except Exception:
            pass

    if not resume_text:
        body = await page.locator("body").inner_text()
        nav_markers = ["Overview", "Content", "Customize", "AI Tools", "Download"]
        for marker in nav_markers:
            idx = body.find(marker)
            if idx >= 0 and idx < 200:
                body = body[idx + len(marker):].strip()
        resume_text = body

    # Snapshot the full .resumePage HTML + every stylesheet so the build script
    # can render the CV without re-fetching FlowCV.
    snapshot = await page.evaluate(
        """() => {
            const el = document.querySelector('.resumePage');
            if (!el) return null;
            const sheets = [];
            for (const sheet of document.styleSheets) {
                try {
                    const rules = Array.from(sheet.cssRules || []);
                    sheets.push(rules.map(r => r.cssText).join('\\n'));
                } catch (e) { /* CORS-restricted sheet */ }
            }
            return { html: el.outerHTML, css: sheets.join('\\n') };
        }"""
    )

    if snapshot and snapshot.get("html"):
        master_html = (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'>"
            f"<base href='https://app.flowcv.com/'>"
            f"<style>{snapshot['css']}</style>"
            "<style>"
            "html, body { margin: 0; padding: 0; background: white; }"
            ".resumePage { position: static !important; margin: 0 auto !important; "
            "  box-shadow: none !important; transform: none !important; }"
            "@page { margin: 0; size: A4; }"
            "</style>"
            f"</head><body>{snapshot['html']}</body></html>"
        )
        with open(".tmp/cv_master.html", "w", encoding="utf-8") as f:
            f.write(master_html)
        print(f"Master CV cached to .tmp/cv_master.html ({len(master_html)} chars)")
    else:
        print("Warning: could not snapshot .resumePage — cv_master.html NOT updated")

    return {
        "resume_id": resume_id,
        "raw_text": resume_text.strip(),
    }


def _regenerate_cv_raw_from_cache() -> dict:
    """When cv_master.html exists but cv_raw.json doesn't, extract the resume text
    and resume ID directly from the cached HTML so the rest of the pipeline can run
    without hitting FlowCV."""
    from bs4 import BeautifulSoup

    with open(".tmp/cv_master.html", "r", encoding="utf-8") as f:
        html = f.read()

    soup = BeautifulSoup(html, "lxml")
    resume_el = soup.select_one("div.resumePage") or soup.body

    text = resume_el.get_text("\n", strip=True) if resume_el else ""

    resume_id = ""
    for el in soup.select("[class*='resumeid-']"):
        cls = " ".join(el.get("class", [])) if el.has_attr("class") else ""
        m = re.search(r"resumeid-([a-f0-9-]{30,})", cls)
        if m:
            resume_id = m.group(1)
            break

    return {"resume_id": resume_id, "raw_text": text}


def _use_cache_if_available() -> bool:
    """If cv_master.html exists, ensure cv_raw.json exists (regenerating from cache
    if needed) and return True to signal the caller can skip the FlowCV trip."""
    if not os.path.exists(".tmp/cv_master.html"):
        return False

    if not os.path.exists(".tmp/cv_raw.json"):
        print("Cache hit: cv_master.html present but cv_raw.json missing — regenerating from cache")
        cv = _regenerate_cv_raw_from_cache()
        with open(".tmp/cv_raw.json", "w", encoding="utf-8") as f:
            json.dump(cv, f, indent=2, ensure_ascii=False)

    print("Using cached CV (.tmp/cv_master.html) — skipping FlowCV login. Pass --refresh to force re-fetch.")
    return True


async def main():
    refresh = "--refresh" in sys.argv

    if not refresh and _use_cache_if_available():
        return

    if not FLOWCV_EMAIL or not FLOWCV_PASSWORD:
        print("Error: FLOWCV_EMAIL and FLOWCV_PASSWORD must be set in .env")
        raise SystemExit(1)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        await login(page)
        resume_id = await open_first_resume(page)
        cv = await extract_all_sections(page, resume_id)

        await context.storage_state(path=".tmp/flowcv_session.json")
        print("Session saved to .tmp/flowcv_session.json")

        await browser.close()

    with open(".tmp/cv_raw.json", "w", encoding="utf-8") as f:
        json.dump(cv, f, indent=2, ensure_ascii=False)

    print("CV saved to .tmp/cv_raw.json")
    print(f"  Resume ID:  {cv['resume_id']}")
    print(f"  Text:       {len(cv['raw_text'])} chars extracted")
    if cv["raw_text"]:
        preview = cv["raw_text"][:200].replace("\n", " | ")
        print(f"  Preview:    {preview}...")
    else:
        print("\nWarning: no resume text extracted. Check .tmp/flowcv_page_dump.html")


if __name__ == "__main__":
    asyncio.run(main())
