"""
XClaw Gateway — single entry point for all Navigator interfaces.

Every inbound message (Telegram, Web, CLI) is normalized into a
standard Request and handed to the Commander.  The Commander
returns a Response that the gateway forwards back to the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable


class Channel(str, Enum):
    TELEGRAM = "telegram"
    WEB = "web"
    CLI = "cli"


@dataclass
class Request:
    """Normalized inbound message from Navigator."""
    text: str
    channel: Channel
    session_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    """Outbound reply to Navigator."""
    text: str
    requires_approval: bool = False
    plan: dict | None = None
    attachments: list[dict] = field(default_factory=list)


# Type alias for any async handler that takes a Request and returns a Response
Handler = Callable[[Request], Awaitable[Response]]


class Gateway:
    """
    Routes incoming requests from any interface to the Commander.

    Usage:
        gateway = Gateway(commander_handler)
        response = await gateway.handle("do something", Channel.CLI, "session-1")
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler

    async def handle(
        self,
        text: str,
        channel: Channel,
        session_id: str,
        metadata: dict | None = None,
    ) -> Response:
        request = Request(
            text=text,
            channel=channel,
            session_id=session_id,
            metadata=metadata or {},
        )
        return await self._handler(request)
