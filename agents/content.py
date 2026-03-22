"""
XClaw Content Agent — writing, formatting, and publishing.

Supported actions:
  - "write"      → writes content for params["topic"], params.get("format", "article")
  - "format"     → reformats params["text"] into params.get("target_format", "markdown")
  - "summarise"  → condenses params["text"]
  - "draft_email"→ drafts an email from params["brief"]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter

logger = logging.getLogger(__name__)

_WRITE_PROMPT = """\
You are a professional content writer. Write a {format} about the following topic.
Be clear, engaging, and well-structured. Use markdown.

TOPIC: {topic}
"""

_FORMAT_PROMPT = """\
Reformat the following text into {target_format}. Preserve all key information.

TEXT:
{text}
"""

_EMAIL_PROMPT = """\
Draft a professional email based on the following brief.
Include: Subject line, greeting, body, and sign-off.

BRIEF: {brief}
"""


class ContentAgent:
    name = "content"

    def __init__(self, llm: "LLMRouter") -> None:
        self._llm = llm

    async def run(self, action: str, params: dict, session_id: str) -> str:
        a = action.lower()

        if "write" in a or "create" in a or "draft" in a and "email" not in a:
            return await self._write(
                params.get("topic", action),
                params.get("format", "article"),
                session_id,
            )

        if "email" in a:
            return await self._draft_email(params.get("brief", action), session_id)

        if "format" in a or "reformat" in a:
            return await self._format(
                params.get("text", ""),
                params.get("target_format", "markdown"),
                session_id,
            )

        # Default: write
        return await self._write(params.get("topic", action), "piece", session_id)

    async def _write(self, topic: str, fmt: str, session_id: str) -> str:
        logger.info("[content] write: %s (%s)", topic, fmt)
        prompt = _WRITE_PROMPT.format(topic=topic, format=fmt)
        return await self._llm.complete(prompt, session_id=session_id)

    async def _format(self, text: str, target_format: str, session_id: str) -> str:
        if not text:
            return "No text provided to format."
        prompt = _FORMAT_PROMPT.format(text=text[:4000], target_format=target_format)
        return await self._llm.complete(prompt, session_id=session_id)

    async def _draft_email(self, brief: str, session_id: str) -> str:
        logger.info("[content] draft_email: %s", brief[:80])
        prompt = _EMAIL_PROMPT.format(brief=brief)
        return await self._llm.complete(prompt, session_id=session_id)
