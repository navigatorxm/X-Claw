"""
XClaw Skill: Deep Research
Structured, multi-source research with automatic citation tracking.
"""

from __future__ import annotations
import asyncio
import re
import httpx

PLUGIN_META = {
    "name":               "research_skill",
    "display_name":       "Deep Research",
    "description":        "Multi-source web research with citations, fact-checking and summaries",
    "version":            "1.0.0",
    "category":           "research",
    "tags":               ["research", "web", "citations"],
    "enabled_by_default": True,
    "requires":           [],
}

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
_DDG = "https://html.duckduckgo.com/html/"


async def research_topic(topic: str, depth: int = 3) -> str:
    """
    Research a topic in depth: searches multiple sources, fetches content, returns structured summary with citations.
    depth: 1=quick(3 sources), 2=standard(5 sources), 3=deep(8 sources).
    """
    depth = max(1, min(depth, 3))
    limit = {1: 3, 2: 5, 3: 8}[depth]

    # 1. Search DuckDuckGo
    sources = await _ddg_search(topic, limit)
    if not sources:
        return f"No results found for: {topic!r}"

    # 2. Fetch content concurrently (cap at 4 parallel)
    sem = asyncio.Semaphore(4)
    async def fetch(url: str) -> tuple[str, str]:
        async with sem:
            return url, await _fetch_text(url)

    results = await asyncio.gather(*[fetch(s["url"]) for s in sources if s.get("url")],
                                    return_exceptions=True)

    # 3. Build structured output
    lines = [f"## Research: {topic}\n"]
    citations = []
    for i, res in enumerate(results, 1):
        if isinstance(res, Exception):
            continue
        url, text = res
        if not text or len(text) < 100:
            continue
        title = sources[i-1].get("title", url)
        snippet = text[:600].replace("\n", " ").strip()
        lines.append(f"**[{i}] {title}**\n{snippet}…\n")
        citations.append(f"[{i}] {url}")

    lines.append("\n### Sources\n" + "\n".join(citations))
    return "\n".join(lines)


async def find_sources(query: str, limit: int = 10) -> str:
    """Find URLs and titles of web sources for a query. Returns a numbered list."""
    sources = await _ddg_search(query, limit)
    if not sources:
        return "No sources found."
    return "\n".join(f"[{i+1}] {s['title']}\n    {s['url']}" for i, s in enumerate(sources))


async def summarize_url(url: str, focus: str = "") -> str:
    """Fetch a URL and return a summary of its content. focus: optional topic to focus on."""
    text = await _fetch_text(url)
    if not text:
        return f"Could not fetch content from {url}"
    if focus:
        # Return context around focus keyword
        idx = text.lower().find(focus.lower())
        if idx >= 0:
            text = text[max(0, idx-200):idx+1500]
    return text[:2000]


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _ddg_search(query: str, limit: int) -> list[dict]:
    try:
        async with httpx.AsyncClient(headers={"User-Agent": _UA}, timeout=10, follow_redirects=True) as client:
            resp = await client.get(_DDG, params={"q": query, "kl": "us-en"})
            resp.raise_for_status()
            html = resp.text
    except Exception:
        return []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for r in soup.select(".result")[:limit]:
            title_el = r.select_one(".result__title")
            href_el = r.select_one("a.result__a")
            snippet_el = r.select_one(".result__snippet")
            if not title_el or not href_el:
                continue
            raw_href = href_el.get("href", "")
            m = re.search(r"uddg=([^&]+)", raw_href)
            from urllib.parse import unquote
            url = unquote(m.group(1)) if m else raw_href
            if url.startswith("http"):
                out.append({"title": title_el.get_text(strip=True), "url": url,
                             "snippet": snippet_el.get_text(strip=True) if snippet_el else ""})
        return out
    except ImportError:
        return []


async def _fetch_text(url: str) -> str:
    try:
        async with httpx.AsyncClient(headers={"User-Agent": _UA}, timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            body = soup.find("article") or soup.find("main") or soup.find("body") or soup
            return re.sub(r"\s+", " ", body.get_text(separator=" ", strip=True))
        except ImportError:
            return re.sub(r"<[^>]+>", " ", html)
    except Exception:
        return ""
