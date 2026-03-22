"""
XClaw CLI Interface — interactive terminal session with the Gateway.

Usage:
    python -m interface.cli
    python main.py --interface cli

Commands:
    /quit, /exit, /q   — exit
    /history           — show recent executions
    /tasks             — list tasks for this session
    /agents            — list registered agents
    /help              — show help
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.gateway import Gateway
    from core.memory import Memory
    from core.router import Router

logger = logging.getLogger(__name__)

_BANNER = """
╔══════════════════════════════════════════╗
║          XClaw — NavOS Terminal          ║
║   Type your request. /help for commands  ║
╚══════════════════════════════════════════╝
"""

_HELP = """
Commands:
  /quit /exit /q   — exit XClaw
  /history         — show recent executions
  /tasks           — list tasks for this session
  /agents          — list registered agents
  /help            — show this message

Anything else is sent to XClaw as a request.
"""


class CLIInterface:
    def __init__(self, gateway: "Gateway", memory: "Memory", router: "Router") -> None:
        self._gateway = gateway
        self._memory = memory
        self._router = router
        self._session_id = f"cli-{uuid.uuid4().hex[:8]}"

    async def run(self) -> None:
        print(_BANNER)
        print(f"Session: {self._session_id}\n")

        while True:
            try:
                text = input("Navigator › ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break

            if not text:
                continue

            if text.lower() in {"/quit", "/exit", "/q"}:
                print("Goodbye.")
                break

            if text == "/help":
                print(_HELP)
                continue

            if text == "/agents":
                agents = self._router.list_agents()
                print("Registered agents:", ", ".join(agents) or "none")
                continue

            if text == "/tasks":
                tasks = self._memory.get_tasks(self._session_id)
                if not tasks:
                    print("No tasks.")
                else:
                    for t in tasks:
                        print(f"  [{t['status']}] (id={t['id']}) {t['title']}")
                continue

            if text == "/history":
                execs = self._memory.get_executions(self._session_id, limit=5)
                if not execs:
                    print("No history.")
                else:
                    for e in execs:
                        print(f"  {e['executed_at']}  {e['summary']}")
                continue

            # Regular request
            try:
                response = await self._gateway.handle(text, "cli", self._session_id)
                print(f"\nXClaw › {response.text}\n")
            except Exception as exc:  # noqa: BLE001
                logger.error("Request failed: %s", exc)
                print(f"Error: {exc}\n")


def run_cli(gateway: "Gateway", memory: "Memory", router: "Router") -> None:
    cli = CLIInterface(gateway, memory, router)
    asyncio.run(cli.run())
