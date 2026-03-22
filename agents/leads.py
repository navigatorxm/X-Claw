"""
XClaw Leads Agent — find, qualify, and draft outreach for prospects.

Supported actions:
  - "find"       → find leads matching params["criteria"]
  - "qualify"    → qualify a lead from params["profile"]
  - "outreach"   → draft outreach message for params["lead"] + params.get("context","")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter

logger = logging.getLogger(__name__)

_FIND_PROMPT = """\
You are a business development expert. Generate a structured list of 10 potential
leads that match the following criteria. For each lead include:
- Company name
- Why they match
- Likely decision-maker title
- Suggested first contact channel

CRITERIA: {criteria}
"""

_QUALIFY_PROMPT = """\
Evaluate this lead profile and score it 1-10 on:
- Budget fit
- Authority (decision-maker access)
- Need alignment
- Timing

Provide a short justification and recommended next action.

PROFILE:
{profile}
"""

_OUTREACH_PROMPT = """\
Write a concise, personalized outreach message (under 150 words) for the
following lead. Avoid generic phrases. Reference specifics from the context.

LEAD: {lead}
CONTEXT: {context}
"""


class LeadsAgent:
    name = "leads"

    def __init__(self, llm: "LLMRouter") -> None:
        self._llm = llm

    async def run(self, action: str, params: dict, session_id: str) -> str:
        a = action.lower()

        if "find" in a or "search" in a or "discover" in a:
            return await self._find(params.get("criteria", action), session_id)

        if "qualify" in a or "score" in a:
            return await self._qualify(params.get("profile", action), session_id)

        if "outreach" in a or "message" in a or "email" in a:
            return await self._outreach(
                params.get("lead", ""),
                params.get("context", ""),
                session_id,
            )

        return await self._find(params.get("criteria", action), session_id)

    async def _find(self, criteria: str, session_id: str) -> str:
        logger.info("[leads] find: %s", criteria)
        return await self._llm.complete(_FIND_PROMPT.format(criteria=criteria), session_id=session_id)

    async def _qualify(self, profile: str, session_id: str) -> str:
        logger.info("[leads] qualify")
        return await self._llm.complete(_QUALIFY_PROMPT.format(profile=profile), session_id=session_id)

    async def _outreach(self, lead: str, context: str, session_id: str) -> str:
        logger.info("[leads] outreach: %s", lead[:60])
        prompt = _OUTREACH_PROMPT.format(lead=lead, context=context or "None provided")
        return await self._llm.complete(prompt, session_id=session_id)
