"""
XClaw Commander — v3.

Routes every request to the appropriate execution engine:
  • AgentLoop (v3) — ReAct tool-calling loop. Used for all complex requests.
  • Wave executor  (v2 fallback) — parallel plan waves. Used when the user
    explicitly types a structured plan command (/plan ...) or as a fallback
    if the AgentLoop is not configured.

Flow (v3 default):
  1. Gateway sends Request
  2. Commander stores the message in memory
  3. Runs AgentLoop: LLM reasons + calls tools iteratively
  4. Returns final result. No approval gate for v3 agentic mode.

Approval gate (still available):
  If Navigator types "/plan <intent>", Commander enters the v2 plan → approve
  → parallel wave execution flow.

Special commands (handled before routing):
  /plan <text>   — force structured plan mode (shows steps, asks for approval)
  /schedule ...  — register a recurring background task
  /tasks         — show task list
  /history       — show recent executions
  /kb <query>    — search knowledge base
  /sources       — list KB sources
  /metrics       — show telemetry snapshot
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from core.gateway import Request, Response
from core.memory import Memory

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.agent_loop import AgentLoop
    from core.knowledge_base import KnowledgeBase
    from core.router import Router
    from core.scheduler import Scheduler
    from core.telemetry import Telemetry

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict], Awaitable[None]]


# ── v2 Plan structures (kept for /plan mode) ───────────────────────────────

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


_PLAN_PROMPT = """\
You are XClaw, an AI executive assistant.

CONVERSATION HISTORY:
{history}

NAVIGATOR'S REQUEST:
"{intent}"

Break this into concrete steps for specialist agents.
Available agents: research, content, leads, tasks, markets, code

Return ONLY valid JSON (no prose):
{{
  "summary": "<one-line summary>",
  "estimated_seconds": <int>,
  "steps": [
    {{
      "id": 1,
      "agent": "<agent_name>",
      "action": "<what this step does>",
      "params": {{}},
      "depends_on": [],
      "description": "<display label>"
    }}
  ]
}}
"""

_EDIT_KEYWORDS = frozenset({"change", "edit", "modify", "update", "replace", "step"})


# ── Commander ──────────────────────────────────────────────────────────────


class Commander:
    """
    Top-level request router.

    In v3, most requests go directly to the AgentLoop.
    Slash commands are intercepted and handled inline.
    The /plan command activates the v2 approval-gate workflow.
    """

    def __init__(
        self,
        llm: "LLMRouter",
        router: "Router",
        memory: Memory,
        agent_loop: "AgentLoop | None" = None,
        kb: "KnowledgeBase | None" = None,
        scheduler: "Scheduler | None" = None,
        telemetry: "Telemetry | None" = None,
        progress_hub=None,
    ) -> None:
        self.llm = llm
        self.router = router
        self.memory = memory
        self._agent_loop = agent_loop
        self._kb = kb
        self._scheduler = scheduler
        self._telemetry = telemetry
        self._hub = progress_hub
        self._pending_plans: dict[str, Plan] = {}

    # ------------------------------------------------------------------
    # Public entry point (called by Gateway)
    # ------------------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        session = request.session_id
        text = request.text.strip()

        # Store user message in history
        self.memory.add_message(session, "user", text)

        # ── Check for pending plan approval ───────────────────────────
        if session in self._pending_plans:
            return await self._handle_plan_approval(session, text)

        # ── Slash commands ────────────────────────────────────────────
        if text.startswith("/"):
            return await self._handle_command(text, session)

        # ── v3 Agentic mode (default) ─────────────────────────────────
        if self._agent_loop:
            trace_id = uuid.uuid4().hex[:12]
            await self._emit(session, {"type": "agent_mode", "trace_id": trace_id})
            try:
                result = await self._agent_loop.run(text, session_id=session, trace_id=trace_id)
            except Exception as exc:
                logger.exception("[commander] AgentLoop failed")
                result = f"Something went wrong: {exc}"
            return Response(text=result)

        # ── v2 fallback (no AgentLoop configured) ─────────────────────
        plan = await self._build_plan(text, session)
        self._pending_plans[session] = plan
        return self._present_plan(plan)

    # ------------------------------------------------------------------
    # Slash command dispatch
    # ------------------------------------------------------------------

    async def _handle_command(self, text: str, session: str) -> Response:
        cmd, _, args = text.partition(" ")
        cmd = cmd.lower()

        if cmd == "/plan":
            intent = args.strip() or "help me plan something"
            plan = await self._build_plan(intent, session)
            self._pending_plans[session] = plan
            return self._present_plan(plan)

        if cmd == "/tasks":
            tasks = self.memory.get_tasks(session)
            if not tasks:
                return Response(text="No tasks.")
            lines = [f"[{t['status']}] (id={t['id']}) {t['title']}" for t in tasks]
            return Response(text="\n".join(lines))

        if cmd == "/history":
            execs = self.memory.get_executions(session, limit=5)
            if not execs:
                return Response(text="No history.")
            lines = [f"• {e['executed_at'][:16]}  {e['summary']}" for e in execs]
            return Response(text="Recent executions:\n" + "\n".join(lines))

        if cmd == "/kb":
            if not self._kb:
                return Response(text="Knowledge base not configured.")
            result = self._kb.search_formatted(args) if args else self._kb.list_sources().__str__()
            return Response(text=result)

        if cmd == "/sources":
            if not self._kb:
                return Response(text="Knowledge base not configured.")
            sources = self._kb.list_sources()
            if not sources:
                return Response(text="Knowledge base is empty.")
            lines = [f"• {s['source']} ({s['chunks']} chunks)" for s in sources]
            return Response(text="\n".join(lines))

        if cmd == "/schedule":
            # /schedule <interval> <prompt>
            # e.g. /schedule 1h check BTC price and alert me
            parts = args.split(" ", 1)
            if len(parts) < 2 or not self._scheduler:
                return Response(text="Usage: /schedule <interval> <prompt>\nExample: /schedule 1h Check BTC price")
            interval, prompt = parts[0], parts[1]
            try:
                task_id = self._scheduler.add_task(session, prompt, interval)
                return Response(text=f"Scheduled (id={task_id}): '{prompt}' every {interval}")
            except ValueError as exc:
                return Response(text=f"Invalid interval: {exc}")

        if cmd == "/scheduled":
            if not self._scheduler:
                return Response(text="Scheduler not configured.")
            tasks = self._scheduler.list_tasks(session)
            if not tasks:
                return Response(text="No scheduled tasks.")
            lines = [f"[{t['id']}] every {t['interval_str']}: {t['prompt'][:60]}" for t in tasks]
            return Response(text="\n".join(lines))

        if cmd == "/metrics":
            if not self._telemetry:
                return Response(text="Telemetry not configured.")
            snap = self._telemetry.snapshot()
            lines = [
                f"Requests: {snap['requests_total']}",
                f"Avg latency: {snap['latency']['avg_ms']:.0f}ms  P95: {snap['latency']['p95_ms']:.0f}ms",
                f"Tool calls: {sum(snap['tool_calls'].values())} total",
                f"Providers: {', '.join(self.llm.available_providers())}",
            ]
            top_tools = sorted(snap["tool_calls"].items(), key=lambda x: -x[1])[:5]
            if top_tools:
                lines.append("Top tools: " + ", ".join(f"{k}×{v}" for k, v in top_tools))
            return Response(text="\n".join(lines))

        if cmd in {"/help", "/?"}:
            return Response(text=_HELP_TEXT)

        # Unknown command — route to AgentLoop as natural language
        if self._agent_loop:
            result = await self._agent_loop.run(text, session_id=session)
            return Response(text=result)
        return Response(text=f"Unknown command: {cmd}. Type /help for commands.")

    # ------------------------------------------------------------------
    # v2 Plan mode (activated by /plan)
    # ------------------------------------------------------------------

    async def _build_plan(self, intent: str, session_id: str) -> Plan:
        history = self.memory.format_history_for_prompt(session_id, limit=4)
        prompt = _PLAN_PROMPT.format(intent=intent, history=history)
        raw = await self.llm.complete(prompt, session_id=session_id)
        raw = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        try:
            data = json.loads(raw)
            steps = [PlanStep(
                step_id=int(s.get("id", i + 1)),
                agent=s.get("agent", "research"),
                action=s.get("action", ""),
                params=s.get("params", {}),
                depends_on=[int(d) for d in s.get("depends_on", [])],
                description=s.get("description", s.get("action", "")),
            ) for i, s in enumerate(data.get("steps", []))]
            return Plan(steps=steps, summary=data.get("summary", intent),
                        estimated_seconds=int(data.get("estimated_seconds", 60)))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Plan parse failed: %s", exc)
            return Plan(
                steps=[PlanStep(step_id=1, agent="research", action=intent, params={"query": intent})],
                summary=intent, estimated_seconds=60,
            )

    def _present_plan(self, plan: Plan) -> Response:
        waves = self._build_waves(plan.steps)
        lines: list[str] = []
        for wave in waves:
            for step in wave:
                par = " ⟳" if len(wave) > 1 else ""
                lines.append(f"  {step.step_id}. [{step.agent}]{par} {step.description or step.action}")
        mins, secs = divmod(plan.estimated_seconds, 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        text = (
            f"Plan:\n\n{chr(10).join(lines)}\n\n"
            f"Estimated: ~{time_str}\n\n"
            f"✅ yes — run   ❌ no — cancel   ✏️ 'change step N to …' — edit\n\n"
            f"*Tip: Skip planning with regular messages — they go straight to the agentic loop.*"
        )
        return Response(text=text, requires_approval=True, plan={"summary": plan.summary})

    async def _handle_plan_approval(self, session: str, text: str) -> Response:
        plan = self._pending_plans[session]
        lower = text.lower()

        if any(kw in lower for kw in _EDIT_KEYWORDS):
            edited = await self._edit_plan(plan, text, session)
            if edited:
                self._pending_plans[session] = edited
                return self._present_plan(edited)

        if lower in {"yes", "y", "approve", "✅", "go", "ok", "run"}:
            self._pending_plans.pop(session)
            return await self._execute_waves(plan, session)

        if lower in {"no", "n", "cancel", "❌", "stop", "abort"}:
            self._pending_plans.pop(session)
            return Response(text="Cancelled.")

        return self._present_plan(plan)

    async def _edit_plan(self, plan: Plan, instruction: str, session_id: str) -> Plan | None:
        existing = json.dumps({"steps": [
            {"id": s.step_id, "agent": s.agent, "action": s.action,
             "params": s.params, "depends_on": s.depends_on} for s in plan.steps
        ]}, indent=2)
        prompt = f"Update this plan:\n{existing}\n\nInstruction: {instruction}\n\nReturn updated plan JSON only."
        try:
            raw = await self.llm.complete(prompt, session_id=session_id)
            raw = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
            data = json.loads(raw)
            steps = [PlanStep(step_id=int(s.get("id", i + 1)), agent=s["agent"],
                              action=s["action"], params=s.get("params", {}),
                              depends_on=[int(d) for d in s.get("depends_on", [])],
                              description=s.get("description", s["action"]))
                     for i, s in enumerate(data.get("steps", []))]
            return Plan(steps=steps, summary=plan.summary, estimated_seconds=plan.estimated_seconds)
        except Exception as exc:
            logger.warning("Plan edit failed: %s", exc)
            return None

    def _build_waves(self, steps: list[PlanStep]) -> list[list[PlanStep]]:
        completed: set[int] = set()
        remaining = list(steps)
        waves: list[list[PlanStep]] = []
        while remaining:
            ready = [s for s in remaining if all(d in completed for d in s.depends_on)]
            if not ready:
                ready = [remaining[0]]
            waves.append(ready)
            for s in ready:
                completed.add(s.step_id)
                remaining.remove(s)
        return waves

    async def _execute_waves(self, plan: Plan, session_id: str) -> Response:
        import asyncio
        waves = self._build_waves(plan.steps)
        results: dict[int, str] = {}
        all_results: list[str] = []

        await self._emit(session_id, {"type": "plan_start", "summary": plan.summary, "waves": len(waves)})

        for wn, wave in enumerate(waves, 1):
            await self._emit(session_id, {
                "type": "wave_start", "wave": wn, "total_waves": len(waves),
                "steps": [{"id": s.step_id, "agent": s.agent, "action": s.description} for s in wave],
                "parallel": len(wave) > 1,
            })

            coros = [self._run_step(s, results, session_id) for s in wave]
            outputs = await asyncio.gather(*coros, return_exceptions=True)

            for step, out in zip(wave, outputs):
                txt = f"⚠️ {out}" if isinstance(out, Exception) else out
                results[step.step_id] = txt
                all_results.append(f"**Step {step.step_id} [{step.agent}]:** {step.description}\n{txt}")
                await self._emit(session_id, {"type": "step_done", "step_id": step.step_id,
                                              "agent": step.agent, "preview": txt[:200]})

        combined = "\n\n---\n\n".join(all_results)
        self.memory.save_execution(session_id, plan.summary, all_results)
        self.memory.add_message(session_id, "xclaw", combined[:600])
        await self._emit(session_id, {"type": "done"})
        return Response(text=f"Done.\n\n{combined}")

    async def _run_step(self, step: PlanStep, results: dict, session_id: str) -> str:
        params = dict(step.params)
        if step.depends_on:
            ctx = "\n\n".join(f"[Step {d}]:\n{results[d][:800]}" for d in step.depends_on if d in results)
            if ctx:
                params["prior_context"] = ctx
        return await self.router.dispatch(step.agent, step.action, params, session_id)

    async def _emit(self, session_id: str, event: dict) -> None:
        if self._hub:
            await self._hub.emit(session_id, event)


# ── Progress Hub (unchanged from v2) ──────────────────────────────────────


import asyncio as _asyncio


class ProgressHub:
    def __init__(self) -> None:
        self._queues: dict[str, _asyncio.Queue] = {}

    def subscribe(self, session_id: str) -> _asyncio.Queue:
        q: _asyncio.Queue = _asyncio.Queue()
        self._queues[session_id] = q
        return q

    def unsubscribe(self, session_id: str) -> None:
        self._queues.pop(session_id, None)

    async def emit(self, session_id: str, event: dict) -> None:
        q = self._queues.get(session_id)
        if q:
            await q.put(event)


# ── Help text ──────────────────────────────────────────────────────────────

_HELP_TEXT = """
**XClaw v3 Commands**

Regular messages → go directly to the agentic AI loop (tool-calling)

*Slash commands:*
  /plan <intent>         Force structured plan mode (shows steps, asks approval)
  /schedule <N> <prompt> Schedule a recurring task (30m, 2h, daily, daily@09:00)
  /scheduled             List your scheduled tasks
  /tasks                 Show your task list
  /history               Show recent executions
  /kb <query>            Search your knowledge base
  /sources               List knowledge base documents
  /metrics               Show performance metrics
  /help                  Show this message

*Examples:*
  Research Harver Space competitors and write a comparison table
  /schedule 1h Get BTC price and alert me if it drops below $90000
  /plan Write a market entry strategy for Harver Space in the US
""".strip()
