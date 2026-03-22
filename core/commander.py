"""
XClaw Commander — Intent → Plan → Approve → Execute loop.

v2 upgrades:
  • Parallel wave execution  — independent steps run concurrently via asyncio.gather()
  • Dependency graph         — steps declare depends_on; Commander resolves execution order
  • Result injection         — dependent steps receive prior step outputs as context
  • Conversation history     — last N turns injected into planning prompt for context-awareness
  • Progress callbacks       — optional async callback for streaming step updates to UI
  • Plan editing             — Navigator can say "change step 2 to X" before approving

Flow:
    1. Gateway sends Request
    2. Commander injects conversation history, asks LLM for a dependency-aware Plan
    3. Plan is presented to Navigator for approval
    4. On approval, steps are executed in topological waves (parallel where possible)
    5. Dependent steps receive prior results as injected context
    6. Final result is stored in memory and returned
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from core.gateway import Request, Response
from core.memory import Memory

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.router import Router

logger = logging.getLogger(__name__)

# Type alias for streaming progress callbacks
ProgressCallback = Callable[[str, dict], Awaitable[None]]


# ── Plan data model ────────────────────────────────────────────────────────


@dataclass
class PlanStep:
    step_id: int
    agent: str
    action: str
    params: dict = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)
    description: str = ""


@dataclass
class Plan:
    steps: list[PlanStep]
    summary: str
    estimated_seconds: int = 0


# ── Prompts ────────────────────────────────────────────────────────────────

_PLAN_PROMPT = """\
You are XClaw, an AI executive assistant for Navigator.

CONVERSATION HISTORY (most recent last):
{history}

NAVIGATOR'S NEW REQUEST:
"{intent}"

Your job:
1. Understand the intent, taking prior conversation into account.
2. Break it into concrete steps delegated to specialist agents.
3. Mark which steps can run IN PARALLEL by leaving depends_on empty [].
   Steps that need a prior step's output should list that step's id in depends_on.

Available agents: research, content, leads, tasks, markets, code

Return ONLY valid JSON (no prose, no markdown fences):
{{
  "summary": "<one-line summary>",
  "estimated_seconds": <int>,
  "steps": [
    {{
      "id": 1,
      "agent": "<agent_name>",
      "action": "<what this step does>",
      "params": {{<key: value>}},
      "depends_on": [],
      "description": "<one-line for display>"
    }}
  ]
}}
"""

_EDIT_KEYWORDS = frozenset({"change", "edit", "modify", "update", "replace", "step", "instead"})


# ── Commander ─────────────────────────────────────────────────────────────


class Commander:
    """
    Orchestrates the full request lifecycle with parallel execution.
    """

    def __init__(
        self,
        llm: "LLMRouter",
        router: "Router",
        memory: Memory,
        progress_hub: "ProgressHub | None" = None,
    ) -> None:
        self.llm = llm
        self.router = router
        self.memory = memory
        self._hub = progress_hub
        self._pending: dict[str, Plan] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        session = request.session_id
        text = request.text.strip()

        if session in self._pending:
            return await self._handle_approval(session, text)

        plan = await self._build_plan(text, session)
        self._pending[session] = plan
        # Record Navigator's message in history
        self.memory.add_message(session, "user", text)
        return self._present_plan(plan)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def _build_plan(self, intent: str, session_id: str) -> Plan:
        history = self.memory.format_history_for_prompt(session_id, limit=6)
        prompt = _PLAN_PROMPT.format(intent=intent, history=history)
        raw = await self.llm.complete(prompt, session_id=session_id)

        # Strip markdown fences if the LLM added them anyway
        raw = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()

        try:
            data = json.loads(raw)
            steps = []
            for s in data.get("steps", []):
                steps.append(PlanStep(
                    step_id=int(s.get("id", len(steps) + 1)),
                    agent=s.get("agent", "research"),
                    action=s.get("action", ""),
                    params=s.get("params", {}),
                    depends_on=[int(d) for d in s.get("depends_on", [])],
                    description=s.get("description", s.get("action", "")),
                ))
            return Plan(
                steps=steps,
                summary=data.get("summary", intent),
                estimated_seconds=int(data.get("estimated_seconds", 60)),
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Plan parse failed (%s), falling back to single step.", exc)
            return Plan(
                steps=[PlanStep(step_id=1, agent="research", action=intent, params={"query": intent})],
                summary=intent,
                estimated_seconds=60,
            )

    def _present_plan(self, plan: Plan) -> Response:
        waves = self._build_waves(plan.steps)
        lines: list[str] = []
        for i, wave in enumerate(waves, 1):
            parallel = len(wave) > 1
            for step in wave:
                parallel_tag = " ⟳ parallel" if parallel else ""
                lines.append(f"  {step.step_id}. [{step.agent}]{parallel_tag} {step.description or step.action}")

        minutes, seconds = divmod(plan.estimated_seconds, 60)
        time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        agents_used = ", ".join(dict.fromkeys(s.agent for s in plan.steps))

        text = (
            f"Here's my plan:\n\n"
            f"{chr(10).join(lines)}\n\n"
            f"Estimated time: ~{time_str}  |  Agents: {agents_used}\n\n"
            f"✅ yes — approve   ❌ no — cancel   ✏️ 'change step N to …' — edit"
        )
        return Response(text=text, requires_approval=True, plan={"summary": plan.summary})

    # ------------------------------------------------------------------
    # Approval handling
    # ------------------------------------------------------------------

    async def _handle_approval(self, session_id: str, text: str) -> Response:
        plan = self._pending[session_id]
        lower = text.lower()

        # Plan edit request
        if any(kw in lower for kw in _EDIT_KEYWORDS):
            edited = await self._try_edit_plan(plan, text, session_id)
            if edited:
                self._pending[session_id] = edited
                return self._present_plan(edited)

        # Approval
        if lower in {"yes", "y", "approve", "✅", "go", "ok", "run", "execute"}:
            self._pending.pop(session_id)
            return await self._execute_plan(plan, session_id)

        # Cancel
        if lower in {"no", "n", "cancel", "❌", "stop", "abort"}:
            self._pending.pop(session_id)
            return Response(text="Cancelled. What else would you like to do?")

        # Ambiguous — re-present the plan
        return self._present_plan(plan)

    async def _try_edit_plan(self, plan: Plan, instruction: str, session_id: str) -> Plan | None:
        """Ask the LLM to apply a human edit instruction to the existing plan."""
        existing_json = json.dumps(
            {"steps": [{"id": s.step_id, "agent": s.agent, "action": s.action,
                        "params": s.params, "depends_on": s.depends_on} for s in plan.steps]},
            indent=2,
        )
        prompt = (
            f"Here is an existing plan in JSON:\n{existing_json}\n\n"
            f"Navigator wants to change it: \"{instruction}\"\n\n"
            f"Return the updated plan JSON using the same schema. "
            f"Only change what was requested. Return ONLY valid JSON."
        )
        try:
            raw = await self.llm.complete(prompt, session_id=session_id)
            raw = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
            data = json.loads(raw)
            steps = [
                PlanStep(
                    step_id=int(s.get("id", i + 1)),
                    agent=s["agent"],
                    action=s["action"],
                    params=s.get("params", {}),
                    depends_on=[int(d) for d in s.get("depends_on", [])],
                    description=s.get("description", s["action"]),
                )
                for i, s in enumerate(data.get("steps", []))
            ]
            return Plan(steps=steps, summary=plan.summary, estimated_seconds=plan.estimated_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Plan edit failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Parallel execution engine
    # ------------------------------------------------------------------

    def _build_waves(self, steps: list[PlanStep]) -> list[list[PlanStep]]:
        """
        Topological sort into execution waves.
        Steps in the same wave have no mutual dependencies → run in parallel.
        """
        completed: set[int] = set()
        remaining = list(steps)
        waves: list[list[PlanStep]] = []

        while remaining:
            ready = [s for s in remaining if all(d in completed for d in s.depends_on)]
            if not ready:
                # Circular or missing dep — take the first remaining step to avoid deadlock
                ready = [remaining[0]]
                logger.warning(
                    "Dependency resolution fallback for step %d — running sequentially",
                    remaining[0].step_id,
                )
            waves.append(ready)
            for s in ready:
                completed.add(s.step_id)
                remaining.remove(s)

        return waves

    async def _execute_plan(self, plan: Plan, session_id: str) -> Response:
        waves = self._build_waves(plan.steps)
        results: dict[int, str] = {}   # step_id → result text
        all_results: list[str] = []

        total_waves = len(waves)
        await self._emit(session_id, {"type": "plan_start", "summary": plan.summary, "waves": total_waves})

        for wave_num, wave in enumerate(waves, 1):
            is_parallel = len(wave) > 1
            logger.info(
                "[%s] Wave %d/%d — %d step(s)%s: %s",
                session_id, wave_num, total_waves, len(wave),
                " (parallel)" if is_parallel else "",
                ", ".join(f"{s.step_id}:{s.agent}" for s in wave),
            )
            await self._emit(session_id, {
                "type": "wave_start",
                "wave": wave_num,
                "total_waves": total_waves,
                "steps": [{"id": s.step_id, "agent": s.agent, "action": s.description or s.action} for s in wave],
                "parallel": is_parallel,
            })

            # Build coroutines for all steps in this wave
            coros = [self._run_step(s, results, session_id) for s in wave]
            wave_outputs = await asyncio.gather(*coros, return_exceptions=True)

            for step, output in zip(wave, wave_outputs):
                if isinstance(output, Exception):
                    result_text = f"⚠️ Step failed: {output}"
                    logger.error("Step %d (%s) raised: %s", step.step_id, step.agent, output)
                else:
                    result_text = output
                results[step.step_id] = result_text
                all_results.append(f"**Step {step.step_id} [{step.agent}] — {step.description or step.action}:**\n{result_text}")

                await self._emit(session_id, {
                    "type": "step_done",
                    "step_id": step.step_id,
                    "agent": step.agent,
                    "preview": result_text[:200],
                })

        combined = "\n\n---\n\n".join(all_results)
        self.memory.save_execution(session_id, plan.summary, all_results)
        self.memory.add_message(session_id, "xclaw", combined[:600])

        await self._emit(session_id, {"type": "done"})
        return Response(text=f"Done.\n\n{combined}")

    async def _run_step(self, step: PlanStep, results: dict[int, str], session_id: str) -> str:
        """Run a single step, injecting prior step results as context."""
        # Inject dependent results into params so the agent can reference them
        enriched_params = dict(step.params)
        if step.depends_on:
            context_parts = []
            for dep_id in step.depends_on:
                if dep_id in results:
                    context_parts.append(f"[Step {dep_id} result]:\n{results[dep_id][:1000]}")
            if context_parts:
                enriched_params["prior_context"] = "\n\n".join(context_parts)

        return await self.router.dispatch(step.agent, step.action, enriched_params, session_id)

    # ------------------------------------------------------------------
    # Progress streaming
    # ------------------------------------------------------------------

    async def _emit(self, session_id: str, event: dict) -> None:
        if self._hub:
            await self._hub.emit(session_id, event)


# ── Progress Hub ───────────────────────────────────────────────────────────


class ProgressHub:
    """
    Per-session asyncio queues for streaming plan execution progress to UIs.

    Usage:
        hub = ProgressHub()
        q = hub.subscribe("session-123")
        # in another coroutine:
        event = await q.get()   # {"type": "step_done", ...}
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[session_id] = q
        return q

    def unsubscribe(self, session_id: str) -> None:
        self._queues.pop(session_id, None)

    async def emit(self, session_id: str, event: dict) -> None:
        q = self._queues.get(session_id)
        if q:
            await q.put(event)
