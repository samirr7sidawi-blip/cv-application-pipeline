"""
Download the user's CV PDF from FlowCV using the saved session.

NOTE: This tool currently downloads the EXISTING CV from FlowCV without applying
the tailored sections. Editing FlowCV's contenteditable fields via Playwright is
fragile, so the tailored content is delivered via the cover letter and the Google
Sheets log instead. The user can manually copy tailored bullets into FlowCV if
they want the PDF itself updated.

Usage:
    python3 tools/update_cv_flowcv.py

Requires:
    .tmp/flowcv_session.json (from read_cv_flowcv.py)
    .tmp/cv_raw.json  (for resume_id)

Output:
    .tmp/tailored_cv.pdf  (currently the original FlowCV PDF)
"""

import json
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()
os.makedirs(".tmp", exist_ok=True)

FLOWCV_EMAIL = os.getenv("FLOWCV_EMAIL", "")
FLOWCV_PASSWORD = os.getenv("FLOWCV_PASSWORD", "")


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def login_if_needed(page) -> None:
    """If the saved session expired, log in fresh."""
    if "login" not in page.url:
        return
    print("Session expired — logging in again")
    await page.wait_for_timeout(2000)
    await page.locator("button", has_text="Login with Email").click()
    await page.wait_for_selector('input[name="email"]', timeout=10000)
    await page.locator('input[name="email"]').fill(FLOWCV_EMAIL)
    await page.locator('input[name="password"]').fill(FLOWCV_PASSWORD)
    await page.locator('form button[type="submit"]').click()
    await page.wait_for_function(
        "() => !window.location.pathname.includes('/login')",
        timeout=20000,
    )


async def main():
    cv = load_json(".tmp/cv_raw.json")
    resume_id = cv.get("resume_id")
    if not resume_id:
        raise SystemExit("No resume_id in .tmp/cv_raw.json — run read_cv_flowcv.py first")

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context_args = {"accept_downloads": True, "viewport": {"width": 1280, "height": 900}}
        if os.path.exists(".tmp/flowcv_session.json"):
            context_args["storage_state"] = ".tmp/flowcv_session.json"

        context = await browser.new_context(**context_args)
        page = await context.new_page()

        await page.goto(f"https://app.flowcv.com/resume/{resume_id}", wait_until="networkidle", timeout=30000)
        await login_if_needed(page)
        if "login" in page.url:
            await page.goto(f"https://app.flowcv.com/resume/{resume_id}", wait_until="networkidle", timeout=30000)

        await page.wait_for_timeout(3000)

        download_btn = page.locator("button", has_text="Download").first
        try:
            await download_btn.wait_for(timeout=10000)
        except Exception:
            raise SystemExit("Could not find Download button on FlowCV editor page")

        async with page.expect_download(timeout=20000) as dl_info:
            await download_btn.click()
        download = await dl_info.value
        await download.save_as(".tmp/tailored_cv.pdf")
        print(f"Downloaded: {download.suggested_filename}")
        print("Saved to .tmp/tailored_cv.pdf")

        await context.storage_state(path=".tmp/flowcv_session.json")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
