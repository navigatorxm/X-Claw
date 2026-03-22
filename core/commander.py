"""
XClaw Commander — the brain of the Intent → Plan → Approve → Execute loop.

Flow:
    1. Receive a Request from the Gateway.
    2. Ask the LLM Brain to parse intent and build an execution plan.
    3. Return the plan to Navigator for approval.
    4. On approval, delegate each step to the Router.
    5. Collect results and return a final Response.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.gateway import Request, Response
from core.memory import Memory

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.router import Router

logger = logging.getLogger(__name__)


@dataclass
class PlanStep:
    agent: str          # e.g. "research", "content", "markets"
    action: str         # human-readable description
    params: dict = field(default_factory=dict)


@dataclass
class Plan:
    steps: list[PlanStep]
    summary: str
    estimated_seconds: int = 0


PLAN_PROMPT = """\
You are XClaw, an AI executive assistant.
Navigator has sent you this request:

"{intent}"

Your job:
1. Identify the intent.
2. Break it into concrete steps that can be delegated to specialist agents.
   Available agents: research, content, leads, tasks, markets, code
3. Return ONLY valid JSON in this exact schema (no prose):

{{
  "summary": "<one-line summary>",
  "estimated_seconds": <int>,
  "steps": [
    {{
      "agent": "<agent_name>",
      "action": "<what this step does>",
      "params": {{<key: value pairs for the agent>}}
    }}
  ]
}}
"""


class Commander:
    """
    Orchestrates the full request lifecycle.

    Attributes:
        llm: LLMRouter used for planning.
        router: Router used for agent dispatch.
        memory: Memory store for session context.
        pending_plans: In-flight plans awaiting approval, keyed by session_id.
    """

    def __init__(self, llm: "LLMRouter", router: "Router", memory: Memory) -> None:
        self.llm = llm
        self.router = router
        self.memory = memory
        self._pending: dict[str, Plan] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        """Entry point called by the Gateway."""
        session = request.session_id
        text = request.text.strip()

        # Check if Navigator is approving / cancelling a pending plan
        if session in self._pending:
            return await self._handle_approval(session, text)

        # New intent — generate a plan and ask for approval
        plan = await self._build_plan(text, session)
        self._pending[session] = plan
        return self._present_plan(plan)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _build_plan(self, intent: str, session_id: str) -> Plan:
        """Ask the LLM to turn Navigator's intent into a structured plan."""
        prompt = PLAN_PROMPT.format(intent=intent)
        raw = await self.llm.complete(prompt, session_id=session_id)

        try:
            data = json.loads(raw)
            steps = [PlanStep(**s) for s in data.get("steps", [])]
            return Plan(
                steps=steps,
                summary=data.get("summary", intent),
                estimated_seconds=data.get("estimated_seconds", 0),
            )
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Plan parse failed (%s), using fallback single-step plan.", exc)
            return Plan(
                steps=[PlanStep(agent="research", action=intent, params={"query": intent})],
                summary=intent,
                estimated_seconds=60,
            )

    def _present_plan(self, plan: Plan) -> Response:
        """Format the plan for Navigator to review."""
        step_lines = "\n".join(
            f"  {i + 1}. [{s.agent}] {s.action}"
            for i, s in enumerate(plan.steps)
        )
        minutes = plan.estimated_seconds // 60
        seconds = plan.estimated_seconds % 60
        time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

        text = (
            f"Here's my plan:\n\n"
            f"{step_lines}\n\n"
            f"Estimated time: ~{time_str}\n"
            f"Agents involved: {', '.join(s.agent for s in plan.steps)}\n\n"
            f"✅ Reply 'yes' to approve   ❌ Reply 'no' to cancel"
        )
        return Response(text=text, requires_approval=True, plan={"summary": plan.summary})

    async def _handle_approval(self, session_id: str, text: str) -> Response:
        """Process Navigator's yes/no response to a pending plan."""
        plan = self._pending.pop(session_id)

        if text.lower() in {"yes", "y", "approve", "✅", "go", "ok"}:
            return await self._execute_plan(plan, session_id)

        return Response(text="Cancelled. What else would you like to do?")

    async def _execute_plan(self, plan: Plan, session_id: str) -> Response:
        """Dispatch each step to the Router and aggregate results."""
        results: list[str] = []
        for i, step in enumerate(plan.steps, 1):
            logger.info("[%s] Executing step %d/%d: %s → %s", session_id, i, len(plan.steps), step.agent, step.action)
            try:
                result = await self.router.dispatch(step.agent, step.action, step.params, session_id)
                results.append(f"**Step {i} ({step.agent}):**\n{result}")
            except Exception as exc:  # noqa: BLE001
                logger.error("Step %d failed: %s", i, exc)
                results.append(f"**Step {i} ({step.agent}):** ⚠️ Failed — {exc}")

        self.memory.save_execution(session_id, plan.summary, results)
        combined = "\n\n".join(results)
        return Response(text=f"Done. Here's what I found:\n\n{combined}")
