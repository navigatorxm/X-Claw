"""
XClaw Router — dispatches plan steps to the correct specialist agent.

Agents register themselves at startup.  The Router finds the right agent
by name and calls its `run(action, params, session_id)` coroutine.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@runtime_checkable
class Agent(Protocol):
    """Every XClaw agent must implement this protocol."""

    name: str  # e.g. "research", "content"

    async def run(self, action: str, params: dict, session_id: str) -> str:
        """Execute the requested action and return a plain-text result."""
        ...


class Router:
    """
    Maintains a registry of agents and dispatches tasks to them.

    Usage:
        router = Router()
        router.register(ResearchAgent())
        result = await router.dispatch("research", "find competitors", {...}, "sid")
    """

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        if not isinstance(agent, Agent):
            raise TypeError(f"{agent!r} does not implement the Agent protocol")
        self._agents[agent.name] = agent
        logger.info("Agent registered: %s", agent.name)

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    async def dispatch(self, agent_name: str, action: str, params: dict, session_id: str) -> str:
        agent = self._agents.get(agent_name)
        if agent is None:
            available = ", ".join(self._agents) or "none"
            raise ValueError(f"Unknown agent '{agent_name}'. Available: {available}")
        logger.debug("Dispatching to '%s': %s", agent_name, action)
        return await agent.run(action, params, session_id)
