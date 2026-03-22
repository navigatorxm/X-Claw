"""
XClaw LLM Router — v3.

New capabilities:
  • complete_with_tools()  — native OpenAI-format tool/function calling
  • complete_stream()      — async SSE token-by-token streaming
  • Simulated tool calling — prompt-based fallback for providers without native support
  • Circuit breaker        — per-provider, auto-resets after 60s
  • Retry with backoff     — 3 attempts per provider before failover
  • Token usage tracking   — logged + reported to Telemetry if provided

Provider priority: OVH → Groq → Gemini → OpenAI (brain/config.yaml)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator

import httpx
import yaml

if TYPE_CHECKING:
    from core.telemetry import Telemetry

logger = logging.getLogger(__name__)
_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# ── Data models ────────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str   # raw JSON string


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"    # "stop" | "tool_calls" | "length"
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def text(self) -> str:
        return self.content or ""


# ── Circuit Breaker ────────────────────────────────────────────────────────


class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_after: float = 60.0) -> None:
        self.threshold = threshold
        self.reset_after = reset_after
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._failures >= self.threshold:
            if self._opened_at and (time.monotonic() - self._opened_at) > self.reset_after:
                self._failures = 0
                self._opened_at = None
                return False
            return True
        return False

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold and not self._opened_at:
            self._opened_at = time.monotonic()
            logger.warning("[circuit] opened for provider")

    def record_success(self) -> None:
        if self._failures:
            logger.info("[circuit] provider recovered")
        self._failures = 0
        self._opened_at = None


# ── Provider ───────────────────────────────────────────────────────────────


class LLMProvider:
    MAX_RETRIES = 3
    RETRY_BACKOFF = 2.0

    def __init__(self, cfg: dict) -> None:
        self.provider = cfg["provider"]
        self.model = cfg["model"]
        self.api_key = cfg.get("api_key", "")
        self.timeout = cfg.get("timeout_seconds", 30)
        self.max_tokens = cfg.get("max_tokens", 2048)
        self.temperature = cfg.get("temperature", 0.3)
        self.supports_tool_calling = cfg.get("supports_tool_calling", True)
        self.supports_streaming = cfg.get("supports_streaming", True)
        raw_url = cfg.get("base_url") or self._default_base_url()
        self.base_url = raw_url.rstrip("/")
        self._breaker = CircuitBreaker()

    def _default_base_url(self) -> str:
        return {
            "groq": "https://api.groq.com/openai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "openai": "https://api.openai.com/v1",
        }.get(self.provider, "")

    def is_available(self) -> bool:
        return bool(self.api_key and self.base_url) and not self._breaker.is_open

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _base_payload(self, messages: list[dict], **kwargs: Any) -> dict:
        return {
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }

    # ── Standard completion ────────────────────────────────────────────

    async def complete(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        payload = self._base_payload(messages, **kwargs)
        return await self._post_with_retry(payload)

    # ── Tool calling ───────────────────────────────────────────────────

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        **kwargs: Any,
    ) -> LLMResponse:
        if not self.supports_tool_calling:
            raise NotImplementedError(f"{self.provider} does not support native tool calling")

        payload = self._base_payload(messages, **kwargs)
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        return await self._post_with_retry(payload)

    # ── Streaming ─────────────────────────────────────────────────────

    async def stream(self, messages: list[dict], **kwargs: Any) -> AsyncGenerator[str, None]:
        """Yield text tokens as they arrive from the provider."""
        if not self.supports_streaming:
            # Fallback: complete normally and yield the whole response
            resp = await self.complete(messages, **kwargs)
            yield resp.text
            return

        payload = self._base_payload(messages, **kwargs)
        payload["stream"] = True

        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout * 2) as client:
            async with client.stream("POST", url, json=payload, headers=self._headers()) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                        delta = chunk["choices"][0].get("delta", {})
                        if text := delta.get("content"):
                            yield text
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    # ── Internal HTTP ──────────────────────────────────────────────────

    async def _post_with_retry(self, payload: dict) -> LLMResponse:
        url = f"{self.base_url}/chat/completions"
        last_exc: Exception | None = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload, headers=self._headers())
                    resp.raise_for_status()
                    self._breaker.record_success()
                    return self._parse_response(resp.json())

                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code < 500:
                        self._breaker.record_failure()
                        raise
                    last_exc = exc

                except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                    last_exc = exc

                if attempt < self.MAX_RETRIES:
                    wait = self.RETRY_BACKOFF ** (attempt - 1)
                    logger.warning("[%s] attempt %d/%d failed, retry in %.1fs: %s",
                                   self.provider, attempt, self.MAX_RETRIES, wait, last_exc)
                    await asyncio.sleep(wait)

        self._breaker.record_failure()
        raise last_exc or RuntimeError(f"{self.provider}: unknown failure")

    @staticmethod
    def _parse_response(data: dict) -> LLMResponse:
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")
        usage = data.get("usage", {})

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            tool_calls.append(ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=tc["function"].get("arguments", "{}"),
            ))

        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else finish_reason,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )


# ── Config helpers ─────────────────────────────────────────────────────────


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


# ── Router ─────────────────────────────────────────────────────────────────


class LLMRouter:
    """
    Multi-provider LLM client with cascading fallback.

    Primary API:
        text = await router.complete("prompt")
        response = await router.complete_with_tools(messages, tools)
        async for token in router.stream("prompt"):
            ...
    """

    def __init__(
        self,
        config_path: str | Path = _CONFIG_PATH,
        telemetry: "Telemetry | None" = None,
    ) -> None:
        raw = yaml.safe_load(Path(config_path).read_text())
        cfg = _expand_env(raw)

        self._system_prompt: str = cfg.get("system_prompt", "You are XClaw.")
        primary = LLMProvider(cfg["primary"])
        fallbacks = [LLMProvider(fb) for fb in cfg.get("fallbacks", [])]
        self._providers: list[LLMProvider] = [primary, *fallbacks]
        self._telemetry = telemetry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(self, prompt: str, session_id: str = "", **kwargs: Any) -> str:
        """Single-turn text completion. Returns assistant text."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]
        resp = await self._dispatch(messages, **kwargs)
        return resp.text

    async def chat(self, messages: list[dict], session_id: str = "", **kwargs: Any) -> str:
        """Multi-turn text completion. Returns assistant text."""
        resp = await self._dispatch(messages, **kwargs)
        return resp.text

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        session_id: str = "",
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Tool-calling completion. Returns LLMResponse with possible tool_calls.

        If the primary provider doesn't support native tool calling, falls back
        to prompt-based simulated tool calling (works with any LLM).
        """
        last_error: Exception | None = None

        for provider in self._providers:
            if not provider.is_available():
                continue
            try:
                logger.info("[llm] tool-call → %s", provider.provider)
                if provider.supports_tool_calling:
                    resp = await provider.complete_with_tools(messages, tools, **kwargs)
                else:
                    resp = await self._simulated_tool_call(provider, messages, tools, **kwargs)
                self._record_usage(provider.provider, resp)
                return resp
            except NotImplementedError:
                # Fall through to simulated
                resp = await self._simulated_tool_call(provider, messages, tools, **kwargs)
                self._record_usage(provider.provider, resp)
                return resp
            except Exception as exc:
                logger.warning("[llm] %s tool-call failed: %s", provider.provider, exc)
                last_error = exc

        raise RuntimeError(f"All providers failed for tool calling. Last: {last_error}")

    async def stream(self, prompt: str, session_id: str = "", **kwargs: Any) -> AsyncGenerator[str, None]:
        """Stream tokens from the first available provider."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]
        for provider in self._providers:
            if not provider.is_available():
                continue
            try:
                logger.info("[llm] streaming → %s", provider.provider)
                async for token in provider.stream(messages, **kwargs):
                    yield token
                return
            except Exception as exc:
                logger.warning("[llm] %s stream failed: %s", provider.provider, exc)
        # Final fallback: complete() and yield whole response
        text = await self.complete(prompt, session_id=session_id, **kwargs)
        yield text

    # ------------------------------------------------------------------
    # Simulated tool calling (prompt-based, works with any LLM)
    # ------------------------------------------------------------------

    _SIMULATED_PREFIX = """\
You have access to the following tools. To call a tool, respond with EXACTLY:
TOOL_CALL: <tool_name>
ARGS: <json_args>

Then stop. I will execute the tool and show you the result.
When you have your final answer, respond normally without TOOL_CALL.

Available tools:
{tool_list}

"""

    async def _simulated_tool_call(
        self,
        provider: LLMProvider,
        messages: list[dict],
        tools: list[dict],
        **kwargs: Any,
    ) -> LLMResponse:
        """Inject tool descriptions into the system prompt and parse TOOL_CALL from response."""
        tool_descriptions = "\n".join(
            f"- {t['function']['name']}: {t['function']['description']}"
            for t in tools
        )
        # Prepend tool info to system message
        sim_messages = list(messages)
        if sim_messages and sim_messages[0]["role"] == "system":
            sim_messages[0] = {
                "role": "system",
                "content": self._SIMULATED_PREFIX.format(tool_list=tool_descriptions) + sim_messages[0]["content"],
            }
        else:
            sim_messages.insert(0, {
                "role": "system",
                "content": self._SIMULATED_PREFIX.format(tool_list=tool_descriptions),
            })

        resp = await provider.complete(sim_messages, **kwargs)
        content = resp.text

        # Parse TOOL_CALL from response
        m = re.search(r"TOOL_CALL:\s*(\w+)\s*\nARGS:\s*(\{.*?\})", content, re.DOTALL)
        if m:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="sim_0", name=m.group(1), arguments=m.group(2))],
                finish_reason="tool_calls",
            )
        return resp   # No tool call detected → normal response

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        last_error: Exception | None = None
        for provider in self._providers:
            if not provider.is_available():
                continue
            try:
                logger.info("[llm] → %s (%s)", provider.provider, provider.model)
                resp = await provider.complete(messages, **kwargs)
                self._record_usage(provider.provider, resp)
                logger.info("[llm] ✓ %s (%d+%d tokens)",
                            provider.provider, resp.prompt_tokens, resp.completion_tokens)
                return resp
            except Exception as exc:
                logger.warning("[llm] ✗ %s: %s", provider.provider, exc)
                last_error = exc

        raise RuntimeError(
            f"All LLM providers exhausted. Last: {last_error}\n"
            "Set at least one API key in .env"
        )

    def _record_usage(self, provider: str, resp: LLMResponse) -> None:
        if self._telemetry and (resp.prompt_tokens or resp.completion_tokens):
            self._telemetry.record_tokens(provider, resp.prompt_tokens, resp.completion_tokens)

    def available_providers(self) -> list[str]:
        return [p.provider for p in self._providers if p.is_available()]

    def provider_status(self) -> list[dict]:
        return [
            {
                "provider": p.provider,
                "model": p.model,
                "available": p.is_available(),
                "circuit_open": p._breaker.is_open,
                "failures": p._breaker._failures,
                "tool_calling": p.supports_tool_calling,
                "streaming": p.supports_streaming,
            }
            for p in self._providers
        ]
