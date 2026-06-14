"""
Scrape a job posting URL and save structured data to .tmp/job_description.json.

Usage:
    python3 tools/scrape_job.py <JOB_URL>

Output:
    .tmp/job_description.json  with keys: title, company, url, description, apply_url
"""

import sys
import json
import os
import re
import asyncio
import httpx
from bs4 import BeautifulSoup

os.makedirs(".tmp", exist_ok=True)


def detect_platform(url: str) -> str:
    if "linkedin.com/jobs" in url:
        return "linkedin"
    if "greenhouse.io" in url or "boards.greenhouse.io" in url:
        return "greenhouse"
    if "jobs.lever.co" in url or "lever.co" in url:
        return "lever"
    if "myworkdayjobs.com" in url or "myworkday.com" in url:
        return "workday"
    return "generic"


def clean_text(text: str) -> str:
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()[:8000]


def clean_title(text: str) -> str:
    """Strip trailing job-board / site-name suffixes from a page title.
    Stellenwerk, Indeed, LinkedIn and most boards append ` | Site Name`,
    `– Site Name`, etc. to the <title> tag. Removes that noise so the email
    subject and sheet row show only the real job title."""
    if not text:
        return text
    text = clean_text(text)
    # Strip a single ` | ...` suffix (Stellenwerk uses this).
    text = re.sub(r"\s*\|\s*[^|]+$", "", text)
    # Strip explicit known-board suffixes (covers ` - LinkedIn`, ` – Indeed`, etc.).
    text = re.sub(
        r"\s*[\-–—:]\s*(LinkedIn|Indeed|StepStone|Stepstone|Xing|Stellenwerk[^\s]*|Greenhouse|Lever|Workday)\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip()


async def scrape_with_playwright(url: str, platform: str) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.goto(url, timeout=30000)

        title = ""
        company = ""
        description = ""
        apply_url = url

        if platform == "linkedin":
            try:
                await page.wait_for_selector("h1.top-card-layout__title", timeout=10000)
                title = await page.locator("h1.top-card-layout__title").first.inner_text()
            except Exception:
                pass
            try:
                company = await page.locator("a.topcard__org-name-link").first.inner_text()
            except Exception:
                try:
                    company = await page.locator("span.topcard__flavor").first.inner_text()
                except Exception:
                    pass
            try:
                description = await page.locator("div.show-more-less-html__markup").first.inner_text()
            except Exception:
                try:
                    description = await page.locator("div.description__text").first.inner_text()
                except Exception:
                    try:
                        description = await page.locator("article").first.inner_text()
                    except Exception:
                        pass
            apply_url = url

        elif platform == "workday":
            try:
                await page.wait_for_selector(
                    "div[data-automation-id='jobPostingDescription']", timeout=20000
                )
                title = await page.locator("h2[data-automation-id='jobPostingHeader']").first.inner_text()
            except Exception:
                try:
                    title = await page.title()
                except Exception:
                    pass
            try:
                description = await page.locator(
                    "div[data-automation-id='jobPostingDescription']"
                ).first.inner_text()
            except Exception:
                pass
            try:
                apply_btn = page.locator("a[data-automation-id='applyButton']").first
                apply_url = await apply_btn.get_attribute("href") or url
            except Exception:
                apply_url = url

        else:
            content = await page.content()
            soup = BeautifulSoup(content, "lxml")
            title = soup.title.string if soup.title else ""
            body_el = soup.find("body")
            description = clean_text(body_el.get_text(separator="\n") if body_el else "")

        await browser.close()

    return {
        "title": clean_title(title),
        "company": clean_text(company),
        "url": url,
        "description": clean_text(description),
        "apply_url": apply_url,
    }


def scrape_with_httpx(url: str, platform: str) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    title = ""
    company = ""
    description = ""
    apply_url = url

    if platform == "greenhouse":
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
        company_el = soup.find(class_="company-name") or soup.find("span", {"id": "company-name"})
        if company_el:
            company = company_el.get_text(strip=True)
        else:
            parts = url.split("/")
            company = parts[3] if len(parts) > 3 else ""
        content_el = (
            soup.find("div", {"id": "content"})
            or soup.find("div", class_="job-post")
            or soup.find("div", {"id": "app_body"})
        )
        if content_el:
            description = clean_text(content_el.get_text(separator="\n"))
        apply_link = soup.find("a", href=re.compile(r"/jobs/\d+/apply"))
        if apply_link:
            href = apply_link["href"]
            apply_url = href if href.startswith("http") else f"https://boards.greenhouse.io{href}"

    elif platform == "lever":
        title_el = soup.find("h2", class_="posting-headline") or soup.find("h2")
        title = title_el.get_text(strip=True) if title_el else ""
        parts = url.rstrip("/").split("/")
        company = parts[3] if len(parts) > 3 else ""
        desc_el = soup.find("div", class_="section-wrapper") or soup.find("div", class_="posting-description")
        if desc_el:
            description = clean_text(desc_el.get_text(separator="\n"))
        apply_link = soup.find("a", {"data-qa": "btn-apply-bottom"}) or soup.find("a", string=re.compile(r"apply", re.I))
        if apply_link:
            href = apply_link.get("href", "")
            apply_url = href if href.startswith("http") else url

    else:
        title_el = soup.find("h1") or soup.find("title")
        title = title_el.get_text(strip=True) if title_el else ""
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find("div", {"id": "job-description"})
            or soup.find("body")
        )
        if main:
            description = clean_text(main.get_text(separator="\n"))
        apply_link = soup.find("a", string=re.compile(r"apply( now)?", re.I))
        if apply_link:
            href = apply_link.get("href", "")
            apply_url = href if href.startswith("http") else url

    return {
        "title": clean_title(title),
        "company": clean_text(company),
        "url": url,
        "description": clean_text(description),
        "apply_url": apply_url,
    }


async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 tools/scrape_job.py <JOB_URL>")
        sys.exit(1)

    url = sys.argv[1]
    platform = detect_platform(url)
    print(f"Platform detected: {platform}")

    if platform in ("linkedin", "workday", "generic"):
        data = await scrape_with_playwright(url, platform)
    else:
        try:
            data = scrape_with_httpx(url, platform)
        except Exception as e:
            print(f"httpx failed ({e}), falling back to Playwright")
            data = await scrape_with_playwright(url, platform)

    if not data["title"]:
        print("Warning: could not extract job title")
    if not data["description"]:
        print("Warning: could not extract job description")

    out = ".tmp/job_description.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Saved to {out}")
    print(f"  Title:   {data['title']}")
    print(f"  Company: {data['company']}")
    print(f"  Apply:   {data['apply_url']}")


if __name__ == "__main__":
    asyncio.run(main())
