"""
XClaw BaseAgent — shared resilience layer for all agents.

Every agent subclasses BaseAgent and implements `_run()`.
BaseAgent wraps it with:
  - Retry with exponential backoff (configurable per agent)
  - Hard timeout (configurable per agent)
  - Automatic error encapsulation (never bubbles a crash to the Commander)
  - Execution timing logged at INFO
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class BaseAgent:
    """
    Abstract base for all XClaw agents.

    Subclass and implement `_run()`.  The public `run()` method adds:
      - timeout enforcement (self.timeout_seconds)
      - retry with backoff   (self.retry_attempts, self.retry_backoff)
      - structured logging
    """

    name: str = "base"
    timeout_seconds: float = 60.0
    retry_attempts: int = 2
    retry_backoff: float = 1.5      # each retry waits backoff^(attempt-1) seconds

    async def run(self, action: str, params: dict, session_id: str) -> str:
        """Public entrypoint. Wraps _run() with timeout + retry."""
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._run_with_retry(action, params, session_id),
                timeout=self.timeout_seconds,
            )
            elapsed = time.monotonic() - t0
            logger.info("[%s] completed in %.2fs", self.name, elapsed)
            return result
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            logger.error("[%s] timed out after %.0fs", self.name, elapsed)
            return f"Agent '{self.name}' timed out after {self.timeout_seconds:.0f}s. Try a narrower request."
        except Exception as exc:
            logger.exception("[%s] unhandled error", self.name)
            return f"Agent '{self.name}' encountered an error: {exc}"

    async def _run_with_retry(self, action: str, params: dict, session_id: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return await self._run(action, params, session_id)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self.retry_attempts:
                    wait = self.retry_backoff ** (attempt - 1)
                    logger.warning(
                        "[%s] attempt %d/%d failed (%.1fs retry): %s",
                        self.name, attempt, self.retry_attempts, wait, exc,
                    )
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    @abstractmethod
    async def _run(self, action: str, params: dict, session_id: str) -> str:
        """Implement the agent's core logic here."""
        ...
