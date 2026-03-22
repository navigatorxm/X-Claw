"""
XClaw Skill: Personal Computer
Browser automation and web interaction — XClaw's own computer.

Uses Playwright (optional) for real browser automation.
Falls back to httpx for simple page operations.

Install Playwright:
    pip install playwright
    playwright install chromium

Without Playwright: fetch_page, click_link, fill_form still work via httpx.
With Playwright:    full browser automation, screenshots, JS-heavy sites.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PLUGIN_META = {
    "name":               "computer_skill",
    "display_name":       "Personal Computer",
    "description":        "Browser automation, screenshots, form filling, and web interaction",
    "version":            "1.0.0",
    "category":           "automation",
    "tags":               ["browser", "automation", "screenshot", "web"],
    "enabled_by_default": True,
    "requires":           [],  # playwright is optional — graceful fallback
}

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
_SCREENSHOT_DIR = Path("memory/screenshots")
_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def browser_open(url: str) -> str:
    """
    Open a URL in a headless browser and return the page text content.
    Uses Playwright if installed, otherwise plain httpx fetch.
    url: the full URL to open (must start with http/https).
    """
    if not url.startswith(("http://", "https://")):
        return f"Invalid URL: {url!r} — must start with http:// or https://"

    if _has_playwright():
        return await _playwright_get_text(url)
    return await _httpx_get_text(url)


async def browser_screenshot(url: str, filename: str = "") -> str:
    """
    Take a screenshot of a webpage. Requires Playwright.
    url: page to screenshot. filename: optional output filename (saved to memory/screenshots/).
    Returns the file path or instructions to install Playwright.
    """
    if not _has_playwright():
        return ("Playwright not installed. Run: pip install playwright && playwright install chromium\n"
                "Then use browser_screenshot again.")

    if not url.startswith(("http://", "https://")):
        return f"Invalid URL: {url!r}"

    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in (filename or url.split("//")[-1][:40]))
    out_path = _SCREENSHOT_DIR / f"{safe}.png"

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_extra_http_headers({"User-Agent": _UA})
            await page.goto(url, timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=10000)
            await page.screenshot(path=str(out_path), full_page=False)
            await browser.close()
        return f"Screenshot saved: {out_path}"
    except Exception as exc:
        return f"Screenshot failed: {exc}"


async def browser_fill_and_submit(url: str, fields: str, submit_selector: str = "") -> str:
    """
    Fill a web form and optionally submit it. Requires Playwright.
    url: form URL. fields: JSON string like '{"#email": "me@example.com", "#name": "XClaw"}'.
    submit_selector: CSS selector for submit button (optional).
    """
    if not _has_playwright():
        return "Playwright not installed. Run: pip install playwright && playwright install chromium"

    import json
    try:
        form_fields = json.loads(fields)
    except Exception:
        return f"fields must be valid JSON: {fields!r}"

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=15000)
            for selector, value in form_fields.items():
                await page.fill(selector, str(value))
            if submit_selector:
                await page.click(submit_selector)
                await page.wait_for_load_state("networkidle", timeout=8000)
            result_url = page.url
            result_text = await page.inner_text("body")
            await browser.close()
        return f"Form submitted. Result URL: {result_url}\n\nPage content:\n{result_text[:800]}"
    except Exception as exc:
        return f"Form fill failed: {exc}"


async def browser_extract_links(url: str) -> str:
    """Extract all hyperlinks from a webpage."""
    html = await _httpx_get_html(url)
    if html.startswith("Error"):
        return html
    import re
    links = re.findall(r'href=["\']([^"\']+)["\']', html)
    # Normalise relative links
    from urllib.parse import urljoin
    full_links = list(dict.fromkeys(
        urljoin(url, l) for l in links
        if not l.startswith(("#", "javascript:", "mailto:"))
    ))[:30]
    return "\n".join(full_links) if full_links else "No links found."


async def browser_get_title(url: str) -> str:
    """Get the title and meta description of a webpage."""
    html = await _httpx_get_html(url)
    if html.startswith("Error"):
        return html
    import re
    title_m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
    desc_m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', html, re.I)
    title = title_m.group(1).strip() if title_m else "No title"
    desc = desc_m.group(1).strip() if desc_m else "No description"
    return f"Title: {title}\nDescription: {desc}\nURL: {url}"


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _playwright_get_text(url: str) -> str:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_extra_http_headers({"User-Agent": _UA})
            await page.goto(url, timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=8000)
            text = await page.inner_text("body")
            await browser.close()
        import re
        return re.sub(r"\s+", " ", text).strip()[:4000]
    except Exception as exc:
        return f"Browser error: {exc}"


async def _httpx_get_html(url: str) -> str:
    try:
        async with httpx.AsyncClient(headers={"User-Agent": _UA}, timeout=12, follow_redirects=True) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as exc:
        return f"Error fetching {url}: {exc}"


async def _httpx_get_text(url: str) -> str:
    html = await _httpx_get_html(url)
    if html.startswith("Error"):
        return html
    try:
        from bs4 import BeautifulSoup
        import re
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        body = soup.find("main") or soup.find("article") or soup.find("body") or soup
        return re.sub(r"\s+", " ", body.get_text(separator=" ", strip=True))[:4000]
    except ImportError:
        import re
        return re.sub(r"<[^>]+>", " ", html)[:4000]
