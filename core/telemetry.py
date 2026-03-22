"""
XClaw Telemetry — lightweight execution tracing and metrics.

Tracks per-request traces, tool call counts, LLM token usage,
latency distributions, and error rates. No external dependencies.

Exposed via:
  GET /metrics  → JSON snapshot
  GET /traces   → recent execution traces
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TraceSpan:
    """A single timed operation within an execution trace."""
    name: str
    kind: str           # "llm", "tool", "agent", "request"
    started_at: float   # monotonic
    ended_at: float = 0.0
    metadata: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000

    def finish(self, error: str | None = None) -> None:
        self.ended_at = time.monotonic()
        self.error = error


@dataclass
class ExecutionTrace:
    """Full trace of one request through XClaw."""
    trace_id: str
    session_id: str
    intent: str
    started_at: str
    spans: list[TraceSpan] = field(default_factory=list)
    total_tokens: int = 0
    tool_calls: int = 0
    iterations: int = 0
    success: bool = True
    ended_at: str | None = None

    def add_span(self, name: str, kind: str, **metadata: Any) -> TraceSpan:
        span = TraceSpan(name=name, kind=kind, started_at=time.monotonic(), metadata=metadata)
        self.spans.append(span)
        return span

    def finish(self, success: bool = True) -> None:
        self.success = success
        self.ended_at = _now()

    def summary(self) -> dict:
        total_ms = sum(s.duration_ms for s in self.spans)
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "intent": self.intent[:80],
            "started_at": self.started_at,
            "duration_ms": round(total_ms),
            "tool_calls": self.tool_calls,
            "iterations": self.iterations,
            "total_tokens": self.total_tokens,
            "success": self.success,
            "spans": len(self.spans),
        }


class Telemetry:
    """
    In-process telemetry store.

    Metrics retained:
      - Rolling 1000 request latencies
      - Per-tool call counts
      - Per-provider token usage
      - Error counts by type
      - Last 50 execution traces
    """

    MAX_TRACES = 50
    MAX_LATENCIES = 1000

    def __init__(self) -> None:
        self._requests = 0
        self._errors: defaultdict[str, int] = defaultdict(int)
        self._tool_calls: defaultdict[str, int] = defaultdict(int)
        self._latencies: deque[float] = deque(maxlen=self.MAX_LATENCIES)   # ms
        self._tokens: defaultdict[str, int] = defaultdict(int)
        self._traces: deque[ExecutionTrace] = deque(maxlen=self.MAX_TRACES)
        self._active_traces: dict[str, ExecutionTrace] = {}   # trace_id → trace
        self._start_time = time.monotonic()

    # ------------------------------------------------------------------
    # Trace lifecycle
    # ------------------------------------------------------------------

    def start_trace(self, trace_id: str, session_id: str, intent: str) -> ExecutionTrace:
        trace = ExecutionTrace(
            trace_id=trace_id,
            session_id=session_id,
            intent=intent,
            started_at=_now(),
        )
        self._active_traces[trace_id] = trace
        self._requests += 1
        return trace

    def finish_trace(self, trace_id: str, success: bool = True) -> ExecutionTrace | None:
        trace = self._active_traces.pop(trace_id, None)
        if trace:
            trace.finish(success)
            total_ms = sum(s.duration_ms for s in trace.spans)
            self._latencies.append(total_ms)
            self._traces.append(trace)
        return trace

    def get_trace(self, trace_id: str) -> ExecutionTrace | None:
        return self._active_traces.get(trace_id) or next(
            (t for t in reversed(self._traces) if t.trace_id == trace_id), None
        )

    # ------------------------------------------------------------------
    # Recording events
    # ------------------------------------------------------------------

    def record_tool_call(self, tool_name: str) -> None:
        self._tool_calls[tool_name] += 1

    def record_tokens(self, provider: str, prompt: int, completion: int) -> None:
        self._tokens[f"{provider}.prompt"] += prompt
        self._tokens[f"{provider}.completion"] += completion

    def record_error(self, error_type: str) -> None:
        self._errors[error_type] += 1

    # ------------------------------------------------------------------
    # Metrics snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        lats = list(self._latencies)
        avg_ms = sum(lats) / len(lats) if lats else 0
        p95_ms = sorted(lats)[int(len(lats) * 0.95)] if lats else 0

        uptime_s = time.monotonic() - self._start_time

        return {
            "uptime_seconds": round(uptime_s),
            "requests_total": self._requests,
            "errors": dict(self._errors),
            "latency": {
                "avg_ms": round(avg_ms, 1),
                "p95_ms": round(p95_ms, 1),
                "samples": len(lats),
            },
            "tool_calls": dict(self._tool_calls),
            "token_usage": dict(self._tokens),
            "active_traces": len(self._active_traces),
        }

    def recent_traces(self, limit: int = 20) -> list[dict]:
        return [t.summary() for t in list(self._traces)[-limit:]]

    # ------------------------------------------------------------------
    # Context manager helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def span(self, trace: ExecutionTrace | None, name: str, kind: str, **meta: Any):
        """Async context manager that times a code block and records a span."""
        if trace is None:
            yield None
            return
        s = trace.add_span(name, kind, **meta)
        try:
            yield s
        except Exception as exc:
            s.finish(error=str(exc))
            raise
        else:
            s.finish()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
