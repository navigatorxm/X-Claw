"""
XClaw Router — dispatches plan steps to the correct specialist agent.

v2 upgrades:
  • Execution timing logged per dispatch
  • dispatch_many() for parallel batch dispatch (used by Commander internally)
  • Middleware hooks (before/after dispatch) for observability
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Middleware: async fn(agent_name, action, params, session_id) → modifies nothing, observes
DispatchMiddleware = Callable[[str, str, dict, str], Awaitable[None]]


@runtime_checkable
class Agent(Protocol):
    """Every XClaw agent must implement this protocol."""

    name: str

    async def run(self, action: str, params: dict, session_id: str) -> str:
        ...


class Router:
    """
    Agent registry with timing, middleware, and parallel batch dispatch.

    Usage:
        router = Router()
        router.register(ResearchAgent())
        result  = await router.dispatch("research", "find competitors", {}, "sid")
        results = await router.dispatch_many([("research", "q1", {}, "sid"),
                                              ("content",  "write", {}, "sid")])
    """

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._middleware: list[DispatchMiddleware] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, agent: Agent) -> None:
        if not isinstance(agent, Agent):
            raise TypeError(f"{agent!r} does not implement the Agent protocol")
        self._agents[agent.name] = agent
        logger.info("Agent registered: %s", agent.name)

    def add_middleware(self, fn: DispatchMiddleware) -> None:
        """Add a coroutine that is called before every dispatch (for logging, metrics, etc.)."""
        self._middleware.append(fn)

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        agent_name: str,
        action: str,
        params: dict,
        session_id: str,
    ) -> str:
        agent = self._agents.get(agent_name)
        if agent is None:
            available = ", ".join(self._agents) or "none"
            raise ValueError(f"Unknown agent '{agent_name}'. Available: {available}")

        # Run middleware hooks
        for mw in self._middleware:
            try:
                await mw(agent_name, action, params, session_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Middleware error (non-fatal): %s", exc)

        t0 = time.monotonic()
        result = await agent.run(action, params, session_id)
        elapsed = time.monotonic() - t0
        logger.info("[router] %s completed in %.2fs", agent_name, elapsed)
        return result

    async def dispatch_many(
        self,
        tasks: list[tuple[str, str, dict, str]],
    ) -> list[str]:
        """
        Dispatch multiple tasks concurrently.

        Args:
            tasks: List of (agent_name, action, params, session_id) tuples.

        Returns:
            List of results in the same order as the input tasks.
            Failed tasks return an error string rather than raising.
        """
        coros = [self.dispatch(agent, action, params, sid) for agent, action, params, sid in tasks]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)
        results: list[str] = []
        for task, outcome in zip(tasks, outcomes):
            if isinstance(outcome, Exception):
                logger.error("dispatch_many: %s/%s failed: %s", task[0], task[1][:40], outcome)
                results.append(f"⚠️ Failed: {outcome}")
            else:
                results.append(outcome)
        return results
