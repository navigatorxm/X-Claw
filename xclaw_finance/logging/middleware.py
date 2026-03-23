"""
FastAPI middleware for request tracking and structured logging.

Generates unique request IDs and logs request/response events.
"""

import uuid
from datetime import datetime
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .structured_logger import get_logger

logger = get_logger(__name__)


def generate_request_id() -> str:
    """Generate a unique request ID."""
    return f"req_{uuid.uuid4().hex[:12]}"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that logs all requests and responses with request ID correlation.

    Attaches request_id to request.state for propagation to all handlers.
    Logs request details, latency, and response status.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and log details."""
        # Generate request ID
        request_id = generate_request_id()
        request.state.request_id = request_id

        # Extract relevant request info
        method = request.method
        path = request.url.path
        agent_id = request.headers.get("X-Agent-ID", "unknown")

        # Log incoming request
        logger.debug(
            f"Incoming request",
            request_id=request_id,
            agent_id=agent_id if agent_id != "unknown" else None,
            method=method,
            path=path,
            query_string=request.url.query if request.url.query else None,
        )

        # Process request
        start_time = datetime.utcnow()
        try:
            response = await call_next(request)
        except Exception as exc:
            # Log request error
            duration_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            logger.error(
                f"Request exception",
                request_id=request_id,
                agent_id=agent_id if agent_id != "unknown" else None,
                method=method,
                path=path,
                latency_ms=duration_ms,
                error=str(exc),
                exception=exc,
            )
            raise

        # Calculate latency
        duration_ms = (datetime.utcnow() - start_time).total_seconds() * 1000

        # Log response
        status_code = response.status_code
        status_str = "success" if status_code < 400 else "error" if status_code >= 500 else "client_error"

        logger.info(
            f"Response sent",
            request_id=request_id,
            agent_id=agent_id if agent_id != "unknown" else None,
            method=method,
            path=path,
            status=status_str,
            status_code=status_code,
            latency_ms=round(duration_ms, 2),
        )

        return response


class RequestIdDependency:
    """Dependency for injecting request_id into route handlers."""

    def __call__(self, request: Request) -> str:
        """Get request_id from request.state."""
        return getattr(request.state, "request_id", "unknown")
