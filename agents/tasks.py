"""
XClaw Tasks Agent — plan, track, and remind Navigator about work items.

Supported actions:
  - "add"     → add a task from params["title"]
  - "list"    → list tasks, optionally filtered by params.get("status")
  - "done"    → mark task params["task_id"] as done
  - "plan"    → generate a project plan from params["goal"]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agents.base import BaseAgent

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.memory import Memory

logger = logging.getLogger(__name__)

_PLAN_PROMPT = """\
Break the following goal into a clear, actionable project plan with milestones
and specific tasks. Format as a numbered list grouped by milestone.

GOAL: {goal}
"""


class TasksAgent(BaseAgent):
    name = "tasks"
    timeout_seconds = 30.0

    def __init__(self, llm: "LLMRouter", memory: "Memory") -> None:
        self._llm = llm
        self._memory = memory

    async def _run(self, action: str, params: dict, session_id: str) -> str:
        a = action.lower()

        if "add" in a or "create" in a or "new" in a:
            return self._add(params.get("title", action), session_id)

        if "list" in a or "show" in a or "get" in a:
            return self._list(session_id, params.get("status"))

        if "done" in a or "complete" in a or "finish" in a:
            task_id = params.get("task_id")
            if task_id:
                return self._mark_done(int(task_id))
            return "Please provide a task_id to mark as done."

        if "plan" in a or "break" in a or "roadmap" in a:
            return await self._plan(params.get("goal", action), session_id)

        return self._list(session_id)

    def _add(self, title: str, session_id: str) -> str:
        task_id = self._memory.add_task(session_id, title)
        return f"Task added (id={task_id}): {title}"

    def _list(self, session_id: str, status: str | None = None) -> str:
        tasks = self._memory.get_tasks(session_id, status)
        if not tasks:
            return "No tasks found."
        lines = [f"- [{t['status']}] (id={t['id']}) {t['title']}" for t in tasks]
        return "\n".join(lines)

    def _mark_done(self, task_id: int) -> str:
        self._memory.update_task_status(task_id, "done")
        return f"Task {task_id} marked as done."

    async def _plan(self, goal: str, session_id: str) -> str:
        logger.info("[tasks] plan: %s", goal)
        return await self._llm.complete(_PLAN_PROMPT.format(goal=goal), session_id=session_id)
