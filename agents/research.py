"""
XClaw Research Agent — real web search, concurrent content fetching, summarisation.

v2 upgrades:
  • DuckDuckGo HTML search  — no API key required, real live results
  • Concurrent URL fetching — fetches multiple pages simultaneously with httpx
  • BeautifulSoup parsing   — extracts main content, strips boilerplate
  • Result caching          — identical queries skip network round-trips
  • prior_context injection — uses results from upstream plan steps

Supported actions:
  "search"     → params["query"] — live DDG search + summarise top results
  "fetch"      → params["url"]   — fetch + extract a specific URL
  "summarise"  → params["text"]  — condense provided text
  anything else → treated as a research query
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import httpx

from agents.base import BaseAgent

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.memory import Memory

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_DDG_URL = "https://html.duckduckgo.com/html/"
_FETCH_CONCURRENCY = 3
_MAX_PAGE_CHARS = 4000

_SUMMARISE_PROMPT = """\
Summarise the following content in {max_words} words or fewer.
Be factual and structured. Use bullet points where helpful.

{text}
"""

_RESEARCH_PROMPT = """\
You are a research assistant. Answer this research request using the provided sources.
Be factual, well-structured, and use markdown. Cite specifics.

REQUEST: {query}

SOURCES:
{sources}

PRIOR CONTEXT FROM EARLIER STEPS:
{prior_context}
"""

_FALLBACK_PROMPT = """\
You are a research assistant. Answer the following as accurately as possible
using your knowledge. Note if you are uncertain.

REQUEST: {query}
"""


class ResearchAgent(BaseAgent):
    name = "research"
    timeout_seconds = 90.0
    retry_attempts = 2

    def __init__(self, llm: "LLMRouter", memory: "Memory | None" = None) -> None:
        self._llm = llm
        self._memory = memory

    # ------------------------------------------------------------------
    # BaseAgent implementation
    # ------------------------------------------------------------------

    async def _run(self, action: str, params: dict, session_id: str) -> str:
        a = action.lower()
        prior = params.get("prior_context", "")

        if "fetch" in a:
            return await self._fetch_url(params.get("url", ""), session_id)

        if a in {"summarise", "summarize"} or "summarise" in a or "summarize" in a:
            text = params.get("text", prior or action)
            return await self._summarise(text, params.get("max_words", 300), session_id)

        # Default: search + synthesise
        query = params.get("query", action)
        return await self._search_and_synthesise(query, prior, session_id)

    # ------------------------------------------------------------------
    # DuckDuckGo search
    # ------------------------------------------------------------------

    async def _search_and_synthesise(self, query: str, prior_context: str, session_id: str) -> str:
        """Search DDG, fetch top pages concurrently, synthesise with LLM."""
        logger.info("[research] search: %s", query)

        # Check cache first
        if self._memory:
            cache_key = self._memory.cache_key("research_search", query)
            if cached := self._memory.cache_get(cache_key):
                logger.info("[research] cache hit: %s", query[:50])
                return cached

        links = await self._ddg_links(query, max_results=5)
        if not links:
            logger.warning("[research] no DDG results, falling back to LLM knowledge")
            return await self._llm.complete(_FALLBACK_PROMPT.format(query=query), session_id=session_id)

        # Fetch pages concurrently (up to _FETCH_CONCURRENCY at once)
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
        async def fetch_one(url: str) -> str:
            async with sem:
                return await self._fetch_url(url, session_id)

        pages = await asyncio.gather(*[fetch_one(url) for url in links], return_exceptions=True)
        sources = []
        for url, page in zip(links, pages):
            if isinstance(page, Exception) or not page:
                continue
            sources.append(f"URL: {url}\n{page[:_MAX_PAGE_CHARS]}")

        if not sources:
            return await self._llm.complete(_FALLBACK_PROMPT.format(query=query), session_id=session_id)

        sources_text = "\n\n---\n\n".join(sources[:3])
        prompt = _RESEARCH_PROMPT.format(
            query=query,
            sources=sources_text,
            prior_context=prior_context or "(none)",
        )
        result = await self._llm.complete(prompt, session_id=session_id)

        # Cache for 5 minutes
        if self._memory:
            self._memory.cache_set(cache_key, result, session_id=session_id, ttl=300)

        return result

    async def _ddg_links(self, query: str, max_results: int = 5) -> list[str]:
        """Scrape DuckDuckGo HTML results page for URLs."""
        try:
            async with httpx.AsyncClient(headers={"User-Agent": _UA}, timeout=10, follow_redirects=True) as client:
                resp = await client.get(_DDG_URL, params={"q": query, "kl": "us-en"})
                resp.raise_for_status()
                html = resp.text

            # Try BeautifulSoup first, fall back to regex
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                links = []
                for a in soup.select("a.result__url"):
                    href = a.get("href", "")
                    if href.startswith("http"):
                        links.append(href)
                # Also try result__a links with uddg param
                if not links:
                    for a in soup.select("a.result__a"):
                        href = a.get("href", "")
                        # DDG wraps URLs: /l/?uddg=https%3A...
                        m = re.search(r"uddg=([^&]+)", href)
                        if m:
                            from urllib.parse import unquote
                            links.append(unquote(m.group(1)))
                return links[:max_results]
            except ImportError:
                # Regex fallback
                urls = re.findall(r'uddg=([^&"]+)', html)
                from urllib.parse import unquote
                return [unquote(u) for u in urls[:max_results] if u.startswith("http")]

        except Exception as exc:  # noqa: BLE001
            logger.warning("[research] DDG search failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # URL fetching
    # ------------------------------------------------------------------

    async def _fetch_url(self, url: str, session_id: str) -> str:
        if not url:
            return "No URL provided."
        try:
            async with httpx.AsyncClient(headers={"User-Agent": _UA}, timeout=10, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
            return self._extract_text(html)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[research] fetch %s failed: %s", url[:60], exc)
            return ""

    @staticmethod
    def _extract_text(html: str) -> str:
        """Extract readable text from HTML, preferring BeautifulSoup."""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            # Remove noisy elements
            for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
                tag.decompose()
            # Prefer article/main content
            main = soup.find("article") or soup.find("main") or soup.find("body") or soup
            text = main.get_text(separator=" ", strip=True)
        except ImportError:
            text = re.sub(r"<[^>]+>", " ", html)

        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text[:_MAX_PAGE_CHARS]

    # ------------------------------------------------------------------
    # Summarisation
    # ------------------------------------------------------------------

    async def _summarise(self, text: str, max_words: int, session_id: str) -> str:
        if not text.strip():
            return "Nothing to summarise."
        prompt = _SUMMARISE_PROMPT.format(max_words=max_words, text=text[:6000])
        return await self._llm.complete(prompt, session_id=session_id)
