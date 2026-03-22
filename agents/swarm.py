"""
XClaw Agent Swarm — parallel multi-agent execution.

The swarm decomposes a complex task into sub-tasks, dispatches each
to a specialised mini-agent running concurrently, then synthesises
the results into a single coherent output.

Architecture:
    SwarmOrchestrator
        ├── decompose(task) → [SubTask, ...]
        ├── dispatch(sub_tasks) → asyncio.gather(...)  ← all parallel
        └── synthesise(results) → final answer

Built-in swarm worker types:
    researcher   — web search + summarise
    coder        — code generation / execution
    analyst      — data analysis / comparison
    writer       — drafting / editing text
    planner      — task decomposition / scheduling
    fact_checker — cross-verify claims

Usage (from Commander/AgentLoop):
    orchestrator = SwarmOrchestrator(llm, tools, progress_hub)
    result = await orchestrator.run(task, session_id)

Tool-callable:
    swarm_task(task, workers) — exposed to LLM via ToolRegistry
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.commander import ProgressHub
    from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── Data structures ───────────────────────────────────────────────────────────

WORKER_TYPES = ["researcher", "coder", "analyst", "writer", "planner", "fact_checker"]

@dataclass
class SubTask:
    id: int
    worker: str           # one of WORKER_TYPES
    description: str
    context: str = ""     # shared context from orchestrator
    result: str = ""
    error: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "worker": self.worker,
            "description": self.description,
            "result": self.result[:500] if self.result else "",
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms),
        }


@dataclass
class SwarmResult:
    task: str
    sub_tasks: list[SubTask] = field(default_factory=list)
    synthesis: str = ""
    total_elapsed_ms: float = 0.0

    def to_markdown(self) -> str:
        lines = [f"## Swarm Result\n**Task:** {self.task}\n"]
        for st in self.sub_tasks:
            icon = "✓" if not st.error else "✗"
            lines.append(f"### {icon} [{st.worker.upper()}] {st.description}")
            if st.error:
                lines.append(f"*Error: {st.error}*")
            else:
                lines.append(st.result[:600] + ("…" if len(st.result) > 600 else ""))
            lines.append("")
        if self.synthesis:
            lines.append("---\n## Synthesis\n" + self.synthesis)
        lines.append(f"\n*Total time: {self.total_elapsed_ms/1000:.1f}s | {len(self.sub_tasks)} agents*")
        return "\n".join(lines)


# ── Worker implementations ────────────────────────────────────────────────────

class SwarmWorker:
    """A lightweight async worker that executes one sub-task."""

    def __init__(self, llm: "LLMRouter", tools: "ToolRegistry | None" = None) -> None:
        self._llm = llm
        self._tools = tools

    async def execute(self, sub_task: SubTask, session_id: str) -> None:
        t0 = time.monotonic()
        try:
            prompt = self._build_prompt(sub_task)
            result = await self._llm.complete(prompt, session_id=session_id)
            sub_task.result = result.strip()
        except Exception as exc:
            sub_task.error = str(exc)
            logger.warning("[swarm] worker %s failed: %s", sub_task.worker, exc)
        finally:
            sub_task.elapsed_ms = (time.monotonic() - t0) * 1000

    def _build_prompt(self, st: SubTask) -> str:
        role_prompts = {
            "researcher": (
                "You are a research specialist. Your task: research the topic thoroughly and "
                "provide a concise, factual summary with key findings."
            ),
            "coder": (
                "You are a coding specialist. Your task: write clean, working code that solves "
                "the problem. Include brief explanation."
            ),
            "analyst": (
                "You are a data analyst. Your task: analyse the information and provide "
                "insights, patterns, and conclusions."
            ),
            "writer": (
                "You are a writing specialist. Your task: produce well-structured, clear "
                "written content for the given objective."
            ),
            "planner": (
                "You are a strategic planner. Your task: create a concrete, actionable plan "
                "with clear steps, owners, and timelines."
            ),
            "fact_checker": (
                "You are a fact-checker. Your task: verify the claims, identify any "
                "inaccuracies, and provide corrections with sources."
            ),
        }
        role = role_prompts.get(st.worker, "You are a helpful AI assistant.")
        ctx = f"\n\nShared context:\n{st.context}" if st.context else ""
        return f"{role}\n\nTask: {st.description}{ctx}\n\nProvide a focused, concise response."


# ── Orchestrator ──────────────────────────────────────────────────────────────

class SwarmOrchestrator:
    """
    Decomposes a task into parallel sub-tasks, runs them concurrently,
    then synthesises results via LLM.
    """

    MAX_WORKERS = 6
    DECOMPOSE_TIMEOUT = 30.0
    WORKER_TIMEOUT = 60.0
    SYNTHESISE_TIMEOUT = 45.0

    def __init__(
        self,
        llm: "LLMRouter",
        tools: "ToolRegistry | None" = None,
        progress_hub: "ProgressHub | None" = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._hub = progress_hub

    async def run(self, task: str, session_id: str, max_workers: int = 4) -> str:
        """Full swarm pipeline: decompose → dispatch → synthesise."""
        t0 = time.monotonic()
        max_workers = min(max_workers, self.MAX_WORKERS)

        self._emit(session_id, "swarm_start", {"task": task[:100], "max_workers": max_workers})
        logger.info("[swarm] starting for task: %.80s", task)

        # 1. Decompose
        sub_tasks = await self._decompose(task, session_id, max_workers)
        if not sub_tasks:
            return "Swarm could not decompose the task. Please try a more specific request."

        self._emit(session_id, "swarm_decomposed", {
            "count": len(sub_tasks),
            "workers": [st.worker for st in sub_tasks],
        })
        logger.info("[swarm] decomposed into %d sub-tasks", len(sub_tasks))

        # 2. Dispatch (parallel)
        worker = SwarmWorker(self._llm, self._tools)
        await asyncio.gather(*[
            asyncio.wait_for(worker.execute(st, session_id), timeout=self.WORKER_TIMEOUT)
            for st in sub_tasks
        ], return_exceptions=True)

        self._emit(session_id, "swarm_workers_done", {
            "results": [st.to_dict() for st in sub_tasks],
        })

        # 3. Synthesise
        synthesis = await self._synthesise(task, sub_tasks, session_id)

        result = SwarmResult(
            task=task,
            sub_tasks=sub_tasks,
            synthesis=synthesis,
            total_elapsed_ms=(time.monotonic() - t0) * 1000,
        )

        self._emit(session_id, "swarm_done", {
            "elapsed_ms": round(result.total_elapsed_ms),
            "agents": len(sub_tasks),
        })
        logger.info("[swarm] done in %.2fs", result.total_elapsed_ms / 1000)

        return result.to_markdown()

    async def _decompose(self, task: str, session_id: str, max_workers: int) -> list[SubTask]:
        prompt = f"""You are a task orchestrator. Break the following task into {max_workers} or fewer parallel sub-tasks.
Each sub-task should be assigned to ONE of these worker types: {', '.join(WORKER_TYPES)}.

Task: {task}

Respond with ONLY a JSON array. Each item must have:
  "worker": one of {WORKER_TYPES}
  "description": concise sub-task (max 100 chars)

Example:
[
  {{"worker": "researcher", "description": "Research current AI coding tools landscape"}},
  {{"worker": "analyst", "description": "Compare top 5 tools by features and pricing"}},
  {{"worker": "writer", "description": "Draft a 300-word executive summary of findings"}}
]

JSON array only, no other text:"""

        try:
            raw = await asyncio.wait_for(
                self._llm.complete(prompt, session_id=session_id),
                timeout=self.DECOMPOSE_TIMEOUT,
            )
            # Extract JSON from response
            import json, re
            m = re.search(r'\[[\s\S]+\]', raw)
            if not m:
                raise ValueError("no JSON array in response")
            items = json.loads(m.group())
            sub_tasks = []
            for i, item in enumerate(items[:self.MAX_WORKERS]):
                worker = item.get("worker", "researcher")
                if worker not in WORKER_TYPES:
                    worker = "researcher"
                sub_tasks.append(SubTask(
                    id=i + 1,
                    worker=worker,
                    description=str(item.get("description", ""))[:200],
                    context=f"Parent task: {task[:200]}",
                ))
            return sub_tasks
        except Exception as exc:
            logger.warning("[swarm] decompose failed (%s), using fallback", exc)
            # Fallback: create 2 generic sub-tasks
            return [
                SubTask(1, "researcher", f"Research and gather information about: {task[:120]}", context=task[:200]),
                SubTask(2, "writer", f"Synthesise and present findings about: {task[:120]}", context=task[:200]),
            ]

    async def _synthesise(self, task: str, sub_tasks: list[SubTask], session_id: str) -> str:
        completed = [st for st in sub_tasks if not st.error]
        if not completed:
            return "All sub-tasks failed. Please retry or simplify the task."

        parts = "\n\n".join(
            f"[{st.worker.upper()}] {st.description}:\n{st.result[:800]}"
            for st in completed
        )
        prompt = f"""You are a synthesis expert. Multiple AI agents worked on this task in parallel.

Original task: {task}

Agent outputs:
{parts}

Write a clear, cohesive synthesis that:
1. Integrates all agent findings
2. Resolves any contradictions
3. Delivers a direct, actionable answer to the original task

Be concise and structured:"""

        try:
            return await asyncio.wait_for(
                self._llm.complete(prompt, session_id=session_id),
                timeout=self.SYNTHESISE_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("[swarm] synthesis failed: %s", exc)
            return "\n\n".join(f"**{st.worker}:** {st.result[:400]}" for st in completed)

    def _emit(self, session_id: str, event_type: str, data: dict) -> None:
        if self._hub:
            try:
                self._hub.emit(session_id, {"type": event_type, **data})
            except Exception:
                pass


# ── Tool-callable interface ───────────────────────────────────────────────────

def make_swarm_tool(llm: "LLMRouter", tools: "ToolRegistry | None" = None,
                    hub: "ProgressHub | None" = None):
    """
    Returns an async function that the ToolRegistry can register.
    Call make_swarm_tool() at startup and register the result.
    """
    orchestrator = SwarmOrchestrator(llm=llm, tools=tools, progress_hub=hub)

    async def swarm_task(task: str, workers: int = 3) -> str:
        """
        Dispatch a swarm of parallel AI agents to complete a complex task.
        Use this when a task can be broken into independent sub-tasks that
        can run simultaneously (research + analysis + writing, etc.).
        task: the full task description.
        workers: number of parallel agents (2-6, default 3).
        """
        import re as _re
        session_id = f"swarm-{abs(hash(task)) % 100000}"
        workers = max(2, min(6, workers))
        return await orchestrator.run(task, session_id=session_id, max_workers=workers)

    return swarm_task
