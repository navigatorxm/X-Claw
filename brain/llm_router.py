"""
XClaw LLM Router — reliable multi-provider LLM client.

Improvements over v1:
  • httpx AsyncClient  — true async, connection pooling, proper timeouts
  • Circuit breaker    — skips providers with repeated failures (auto-resets after 60s)
  • Per-provider retry — up to 3 attempts with exponential backoff before failing over
  • Conversation msgs  — accepts full message list for context-aware completions
  • Token budget log   — logs usage from response if the provider returns it

Priority: OVH → Groq → Gemini → OpenAI (configured in brain/config.yaml)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# ── Circuit Breaker ────────────────────────────────────────────────────────


class CircuitBreaker:
    """
    Opens after `threshold` consecutive failures.
    Auto-resets after `reset_after` seconds, allowing one probe attempt.
    """

    def __init__(self, threshold: int = 3, reset_after: float = 60.0) -> None:
        self.threshold = threshold
        self.reset_after = reset_after
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._failures >= self.threshold:
            if self._opened_at and (time.monotonic() - self._opened_at) > self.reset_after:
                # Half-open: reset so next call gets through as a probe
                self._failures = 0
                self._opened_at = None
                return False
            return True
        return False

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            logger.warning("[circuit] opened — provider will be skipped for %.0fs", self.reset_after)

    def record_success(self) -> None:
        if self._failures:
            logger.info("[circuit] closed — provider recovered")
        self._failures = 0
        self._opened_at = None


# ── Provider ───────────────────────────────────────────────────────────────


class LLMProvider:
    """One configured LLM endpoint with its own circuit breaker and retry logic."""

    MAX_RETRIES = 3
    RETRY_BACKOFF = 2.0  # seconds; doubles each attempt

    def __init__(self, cfg: dict) -> None:
        self.provider = cfg["provider"]
        self.model = cfg["model"]
        self.api_key = cfg.get("api_key", "")
        self.timeout = cfg.get("timeout_seconds", 30)
        self.max_tokens = cfg.get("max_tokens", 2048)
        self.temperature = cfg.get("temperature", 0.3)
        self.base_url = (cfg.get("base_url") or self._default_base_url()).rstrip("/")
        self._breaker = CircuitBreaker()

    def _default_base_url(self) -> str:
        return {
            "groq": "https://api.groq.com/openai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "openai": "https://api.openai.com/v1",
        }.get(self.provider, "")

    def is_available(self) -> bool:
        return bool(self.api_key and self.base_url) and not self._breaker.is_open

    async def complete(self, messages: list[dict], **kwargs: Any) -> str:
        """Call the endpoint, retry on transient errors, update the circuit breaker."""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    # Log token usage if available
                    if usage := data.get("usage"):
                        logger.debug(
                            "[%s] tokens — prompt:%d completion:%d",
                            self.provider,
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                        )
                    self._breaker.record_success()
                    return data["choices"][0]["message"]["content"]

                except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                    last_exc = exc
                    if attempt < self.MAX_RETRIES:
                        wait = self.RETRY_BACKOFF ** (attempt - 1)
                        logger.warning(
                            "[%s] transient error (attempt %d/%d), retry in %.1fs: %s",
                            self.provider, attempt, self.MAX_RETRIES, wait, exc,
                        )
                        await asyncio.sleep(wait)

                except httpx.HTTPStatusError as exc:
                    # 4xx = client error, not worth retrying
                    if exc.response.status_code < 500:
                        self._breaker.record_failure()
                        raise
                    last_exc = exc
                    if attempt < self.MAX_RETRIES:
                        wait = self.RETRY_BACKOFF ** (attempt - 1)
                        logger.warning("[%s] HTTP %d, retry in %.1fs", self.provider, exc.response.status_code, wait)
                        await asyncio.sleep(wait)

        self._breaker.record_failure()
        raise last_exc or RuntimeError(f"{self.provider}: unknown failure")


# ── Config helpers ─────────────────────────────────────────────────────────


def _expand_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), value)


def _resolve(cfg: Any) -> Any:
    if isinstance(cfg, dict):
        return {k: _resolve(v) for k, v in cfg.items()}
    if isinstance(cfg, list):
        return [_resolve(v) for v in cfg]
    if isinstance(cfg, str):
        return _expand_env(cfg)
    return cfg


# ── Router ─────────────────────────────────────────────────────────────────


class LLMRouter:
    """
    Cascading LLM router: primary → fallback1 → fallback2 → …

    Usage:
        router = LLMRouter()
        text = await router.complete("Summarise this…")
        text = await router.chat([{"role":"user","content":"…"}])
    """

    def __init__(self, config_path: str | Path = _CONFIG_PATH) -> None:
        raw = yaml.safe_load(Path(config_path).read_text())
        cfg = _resolve(raw)

        self._system_prompt: str = cfg.get("system_prompt", "You are XClaw.")
        primary = LLMProvider(cfg["primary"])
        fallbacks = [LLMProvider(fb) for fb in cfg.get("fallbacks", [])]
        self._providers: list[LLMProvider] = [primary, *fallbacks]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(self, prompt: str, session_id: str = "", **kwargs: Any) -> str:
        """Single-turn: send `prompt` as a user message, return assistant text."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]
        return await self.chat(messages, session_id=session_id, **kwargs)

    async def chat(
        self,
        messages: list[dict],
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        """
        Multi-turn: send a full message list, return assistant text.
        Tries each provider in order; raises RuntimeError if all fail.
        """
        last_error: Exception | None = None

        for provider in self._providers:
            if not provider.is_available():
                reason = "circuit open" if provider._breaker.is_open else "not configured"
                logger.debug("Skipping %s (%s)", provider.provider, reason)
                continue

            try:
                logger.info("[llm] → %s (%s)", provider.provider, provider.model)
                result = await provider.complete(messages, **kwargs)
                logger.info("[llm] ✓ %s", provider.provider)
                return result
            except Exception as exc:  # noqa: BLE001
                logger.warning("[llm] ✗ %s: %s", provider.provider, exc)
                last_error = exc

        raise RuntimeError(
            f"All LLM providers exhausted. Last error: {last_error}\n"
            f"Check your API keys in .env — at least one provider must be configured."
        )

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
            }
            for p in self._providers
        ]
