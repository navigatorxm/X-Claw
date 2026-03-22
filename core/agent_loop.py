"""
XClaw AgentLoop — the v3 execution engine.

Implements the ReAct (Reason + Act) pattern:
  1. LLM reasons about the intent
  2. LLM calls a tool (or multiple tools in parallel)
  3. Tool results are fed back to the LLM
  4. LLM reasons again with full context
  5. Repeat until done (finish_reason == "stop") or max_iterations reached

Key features:
  • Parallel tool execution — multiple tool calls from one LLM response run concurrently
  • Context compression — when conversation grows large, older tool results are summarised
  • Trace integration — every LLM call and tool invocation recorded in Telemetry
  • Progress streaming — optional ProgressHub for real-time UI updates
  • Self-correction — if a tool returns an error, the LLM can try a different approach

Usage:
    loop = AgentLoop(llm=llm_router, tools=registry, memory=memory, telemetry=tel)
    result = await loop.run("Research XYZ and write a report", session_id="s1")
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter, LLMResponse
    from core.memory import Memory
    from core.telemetry import ExecutionTrace, Telemetry
    from core.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict], Awaitable[None]]

# ── Constants ──────────────────────────────────────────────────────────────

_MAX_ITERATIONS = 20
_CONTEXT_COMPRESS_THRESHOLD = 12   # compress after this many messages
_MAX_TOOL_RESULT_CHARS = 3000      # truncate tool results longer than this
_DATE_PLACEHOLDER = "{date}"

_SYSTEM_SUFFIX = """
You have access to real tools. Use them to gather live information.
Think step by step. When you have a complete answer, respond with your final
synthesis — do NOT call any more tools.
"""


class AgentLoop:
    """
    ReAct agent loop with tool calling.

    The loop alternates between:
      - LLM reasoning (with full conversation context)
      - Tool execution (parallel if multiple calls in one response)

    It terminates when:
      - The LLM responds without requesting a tool call (finish_reason="stop")
      - max_iterations is reached (forces a final synthesis)
      - An unrecoverable error occurs
    """

    def __init__(
        self,
        llm: "LLMRouter",
        tools: "ToolRegistry",
        memory: "Memory",
        telemetry: "Telemetry | None" = None,
        progress_hub=None,
        max_iterations: int = _MAX_ITERATIONS,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._memory = memory
        self._telemetry = telemetry
        self._hub = progress_hub
        self._max_iter = max_iterations

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        intent: str,
        session_id: str,
        trace_id: str | None = None,
    ) -> str:
        """
        Execute the intent through the ReAct loop.

        Returns the final text response to Navigator.
        """
        trace_id = trace_id or uuid.uuid4().hex[:12]
        trace = self._telemetry.start_trace(trace_id, session_id, intent) if self._telemetry else None

        try:
            result = await self._loop(intent, session_id, trace)
        except Exception as exc:
            logger.exception("[loop] unhandled error in session %s", session_id)
            if trace:
                self._telemetry.finish_trace(trace_id, success=False)  # type: ignore[union-attr]
            return f"XClaw encountered an error: {exc}"

        if trace:
            self._telemetry.finish_trace(trace_id, success=True)  # type: ignore[union-attr]

        # Persist result in memory
        self._memory.add_message(session_id, "xclaw", result[:600])
        self._memory.save_execution(session_id, intent[:120], [result])
        return result

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    async def _loop(
        self,
        intent: str,
        session_id: str,
        trace: "ExecutionTrace | None",
    ) -> str:
        # Build initial message list
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        system = self._llm._system_prompt.replace(_DATE_PLACEHOLDER, date_str) + _SYSTEM_SUFFIX
        history = self._memory.get_recent_messages(session_id, limit=6)
        messages: list[dict] = [{"role": "system", "content": system}]

        # Inject conversation history
        for msg in history:
            role = "user" if msg["role"] == "user" else "assistant"
            messages.append({"role": role, "content": msg["content"]})

        messages.append({"role": "user", "content": intent})

        tools_schema = self._tools.as_openai_tools()
        iteration = 0

        await self._emit(session_id, {
            "type": "agent_start",
            "intent": intent,
            "tools": self._tools.tool_names(),
        })

        while iteration < self._max_iter:
            iteration += 1
            logger.info("[loop:%s] iteration %d/%d", session_id[:8], iteration, self._max_iter)

            # ── LLM call ──────────────────────────────────────────────
            if trace:
                span = trace.add_span(f"llm_iter_{iteration}", "llm")

            try:
                response = await self._llm.complete_with_tools(
                    messages, tools=tools_schema, session_id=session_id
                )
            except Exception as exc:
                logger.error("[loop] LLM call failed: %s", exc)
                if trace:
                    span.finish(error=str(exc))  # type: ignore[possibly-undefined]
                return f"LLM failed after {iteration} iterations: {exc}"

            if trace:
                span.finish()  # type: ignore[possibly-undefined]
                trace.iterations = iteration
                trace.total_tokens += response.prompt_tokens + response.completion_tokens

            # ── No tool calls → done ──────────────────────────────────
            if not response.has_tool_calls:
                logger.info("[loop:%s] done at iteration %d", session_id[:8], iteration)
                await self._emit(session_id, {"type": "agent_done", "iterations": iteration})
                return response.text or "(no response)"

            # ── Execute tool calls ────────────────────────────────────
            calls = response.tool_calls
            logger.info("[loop:%s] %d tool call(s): %s", session_id[:8], len(calls),
                        ", ".join(c.name for c in calls))

            await self._emit(session_id, {
                "type": "tool_calls",
                "tools": [c.name for c in calls],
                "iteration": iteration,
            })

            # Add assistant message (with tool_calls) to context
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in calls
                ],
            })

            # Run all tool calls in parallel
            if trace:
                tspan = trace.add_span("tool_batch", "tool", count=len(calls))

            tool_responses = await self._tools.call_many(calls, session_id=session_id)

            if trace:
                tspan.finish()  # type: ignore[possibly-undefined]
                trace.tool_calls += len(calls)
                for c in calls:
                    if self._telemetry:
                        self._telemetry.record_tool_call(c.name)

            # Add tool results to context
            for tr in tool_responses:
                content = tr.content
                if len(content) > _MAX_TOOL_RESULT_CHARS:
                    content = content[:_MAX_TOOL_RESULT_CHARS] + f"\n[... truncated to {_MAX_TOOL_RESULT_CHARS} chars]"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_call_id,
                    "content": content,
                })

                await self._emit(session_id, {
                    "type": "tool_done",
                    "tool": tr.name,
                    "preview": content[:200],
                })

            # Compress context if it's getting long
            if len(messages) > _CONTEXT_COMPRESS_THRESHOLD + 2:
                messages = await self._compress_context(messages, session_id)

        # ── Max iterations hit → force synthesis ──────────────────────
        logger.warning("[loop:%s] max iterations (%d) reached, forcing synthesis", session_id[:8], self._max_iter)
        messages.append({
            "role": "user",
            "content": "You have gathered enough information. Now provide your final, complete answer to the original request. Do not call any more tools.",
        })
        try:
            final = await self._llm.chat(messages, session_id=session_id)
            await self._emit(session_id, {"type": "agent_done", "iterations": iteration, "forced": True})
            return final
        except Exception as exc:
            return f"Reached {self._max_iter} iterations. Summary of what was found before the limit."

    # ------------------------------------------------------------------
    # Context compression
    # ------------------------------------------------------------------

    async def _compress_context(self, messages: list[dict], session_id: str) -> list[dict]:
        """
        Summarise the middle of the conversation to keep context manageable.
        Keeps: system message, last 4 messages, compresses the rest.
        """
        system = [m for m in messages if m["role"] == "system"]
        tail = messages[-4:]
        middle = messages[len(system):-4]

        if not middle:
            return messages

        # Build summary of middle messages
        middle_text = "\n".join(
            f"[{m['role']}]: {str(m.get('content', ''))[:300]}"
            for m in middle
        )
        try:
            summary = await self._llm.complete(
                f"Summarise these agent steps concisely (under 300 words):\n{middle_text}",
                session_id=session_id,
            )
        except Exception:
            summary = f"[{len(middle)} prior messages compressed]"

        compressed = system + [
            {"role": "user", "content": f"[Context summary from prior steps:]\n{summary}"},
        ] + tail

        logger.debug("[loop] compressed %d messages to 1 summary", len(middle))
        return compressed

    # ------------------------------------------------------------------
    # Progress streaming
    # ------------------------------------------------------------------

    async def _emit(self, session_id: str, event: dict) -> None:
        if self._hub:
            await self._hub.emit(session_id, event)
