"""
XClaw Research Agent — web search, content fetch, and summarisation.

Supported actions (passed as `action` string):
  - "search"        → runs a web search for params["query"]
  - "fetch"         → fetches and extracts text from params["url"]
  - "summarise"     → summarises params["text"] to params.get("max_words", 300) words
  - anything else   → treated as a free-form research query
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter

logger = logging.getLogger(__name__)

_SUMMARISE_PROMPT = """\
Summarise the following text in {max_words} words or fewer.
Be factual, structured, and use bullet points where helpful.

TEXT:
{text}
"""

_RESEARCH_PROMPT = """\
You are a research assistant. Answer the following research request concisely
and factually, using markdown formatting.

REQUEST: {query}
"""


class ResearchAgent:
    name = "research"

    def __init__(self, llm: "LLMRouter") -> None:
        self._llm = llm

    async def run(self, action: str, params: dict, session_id: str) -> str:
        action_lower = action.lower()

        if action_lower == "search" or "search" in action_lower:
            return await self._search(params.get("query", action), session_id)

        if action_lower == "fetch" or "fetch" in action_lower:
            return await self._fetch(params.get("url", ""), session_id)

        if action_lower in {"summarise", "summarize"}:
            return await self._summarise(
                params.get("text", ""),
                params.get("max_words", 300),
                session_id,
            )

        # Generic research fallback
        return await self._research(params.get("query", action), session_id)

    # ------------------------------------------------------------------
    # Sub-actions
    # ------------------------------------------------------------------

    async def _search(self, query: str, session_id: str) -> str:
        """Delegate web search to the LLM (which can call search tools if available)."""
        logger.info("[research] search: %s", query)
        prompt = _RESEARCH_PROMPT.format(query=query)
        return await self._llm.complete(prompt, session_id=session_id)

    async def _fetch(self, url: str, session_id: str) -> str:
        """Fetch a URL and return extracted text (best-effort)."""
        if not url:
            return "No URL provided."
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                raw = resp.read(50_000).decode("utf-8", errors="replace")
            # Strip tags naively for now; a real implementation would use BeautifulSoup
            import re
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()[:3000]
            return await self._summarise(text, 400, session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[research] fetch failed: %s", exc)
            return f"Could not fetch {url}: {exc}"

    async def _summarise(self, text: str, max_words: int, session_id: str) -> str:
        if not text:
            return "Nothing to summarise."
        prompt = _SUMMARISE_PROMPT.format(max_words=max_words, text=text[:6000])
        return await self._llm.complete(prompt, session_id=session_id)

    async def _research(self, query: str, session_id: str) -> str:
        prompt = _RESEARCH_PROMPT.format(query=query)
        return await self._llm.complete(prompt, session_id=session_id)
