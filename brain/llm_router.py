"""
XClaw LLM Router — v3.1 with smart cost-aware routing.

Routing strategy
────────────────
  1. Classify request complexity: CHEAP → STANDARD → PREMIUM
  2. Pick the first available provider in the matching tier
  3. If that tier has no available providers, escalate to next tier
  4. For tool-calling: prefer providers with native support; fall back to
     simulated (prompt-injection) tool calling as last resort
  5. Circuit breaker per provider — bad providers temporarily excluded

Tier selection rules
────────────────────
  CHEAP    ≤ 30 words, no complex keywords
  STANDARD 31–120 words, or research/analyse keywords
  PREMIUM  > 120 words, or build/implement/architect keywords

Override:
  await router.complete("...", tier="premium")   # force tier
  await router.complete_with_tools(..., tier="standard")

DigitalOcean GenAI
──────────────────
  Fully supported. Set DO_AI_ENDPOINT + DO_API_KEY + DO_AI_MODEL in .env.
  DO is treated as a standard OpenAI-compatible API — same code path.

Providers currently configured (config.yaml):
  cheap    → groq/llama-3.1-8b-instant, gemini-flash, DO llama3-8b, ollama
  standard → groq/llama-3.3-70b, gemini-flash, ovh, openai-mini, DO llama3-70b
  premium  → gemini-1.5-pro, openai-gpt-4o, DO mistral
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

# ── Routing tiers ───────────────────────────────────────────────────────────

CHEAP    = "cheap"
STANDARD = "standard"
PREMIUM  = "premium"
_TIERS   = (CHEAP, STANDARD, PREMIUM)


# ── Data models ─────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str   # raw JSON string


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider_used: str = ""
    tier_used: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def text(self) -> str:
        return self.content or ""


# ── Circuit Breaker ─────────────────────────────────────────────────────────

class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_after: float = 60.0) -> None:
        self.threshold   = threshold
        self.reset_after = reset_after
        self._failures   = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._failures >= self.threshold:
            if self._opened_at and (time.monotonic() - self._opened_at) > self.reset_after:
                self._failures  = 0
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
        self._failures  = 0
        self._opened_at = None


# ── Provider ────────────────────────────────────────────────────────────────

class LLMProvider:
    MAX_RETRIES  = 3
    RETRY_BACKOFF = 2.0

    def __init__(self, cfg: dict) -> None:
        self.name                 = cfg.get("name", cfg["provider"])
        self.provider             = cfg["provider"]
        self.model                = cfg["model"]
        self.api_key              = cfg.get("api_key", "")
        self.timeout              = cfg.get("timeout_seconds", 30)
        self.max_tokens           = cfg.get("max_tokens", 2048)
        self.temperature          = cfg.get("temperature", 0.3)
        self.supports_tool_calling = cfg.get("supports_tool_calling", True)
        self.supports_streaming   = cfg.get("supports_streaming", True)
        self.tier                 = cfg.get("tier", STANDARD)
        self.cost_per_1m          = cfg.get("cost_per_1m_tokens", 0.0)
        raw_url = cfg.get("base_url") or self._default_base_url()
        self.base_url = raw_url.rstrip("/") if raw_url else ""
        self._breaker = CircuitBreaker()

    def _default_base_url(self) -> str:
        return {
            "groq":   "https://api.groq.com/openai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "openai": "https://api.openai.com/v1",
        }.get(self.provider, "")

    def is_available(self) -> bool:
        return bool(self.api_key and self.base_url) and not self._breaker.is_open

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _base_payload(self, messages: list[dict], **kwargs: Any) -> dict:
        return {
            "model":       self.model,
            "messages":    messages,
            "max_tokens":  kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }

    async def complete(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        return await self._post_with_retry(self._base_payload(messages, **kwargs))

    async def complete_with_tools(self, messages: list[dict], tools: list[dict], **kwargs: Any) -> LLMResponse:
        if not self.supports_tool_calling:
            raise NotImplementedError(f"{self.provider}/{self.model} does not support native tool calling")
        payload = self._base_payload(messages, **kwargs)
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"
        return await self._post_with_retry(payload)

    async def stream(self, messages: list[dict], **kwargs: Any) -> AsyncGenerator[str, None]:
        if not self.supports_streaming:
            resp = await self.complete(messages, **kwargs)
            yield resp.text
            return
        payload = {**self._base_payload(messages, **kwargs), "stream": True}
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
                        if text := chunk["choices"][0].get("delta", {}).get("content"):
                            yield text
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def _post_with_retry(self, payload: dict) -> LLMResponse:
        url = f"{self.base_url}/chat/completions"
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload, headers=self._headers())
                    resp.raise_for_status()
                    self._breaker.record_success()
                    result = self._parse_response(resp.json())
                    result.provider_used = self.name
                    result.tier_used     = self.tier
                    return result
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code < 500:
                        self._breaker.record_failure()
                        raise
                    last_exc = exc
                except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                    last_exc = exc
                if attempt < self.MAX_RETRIES:
                    wait = self.RETRY_BACKOFF ** (attempt - 1)
                    logger.warning("[%s] attempt %d/%d failed (retry in %.1fs): %s",
                                   self.name, attempt, self.MAX_RETRIES, wait, last_exc)
                    await asyncio.sleep(wait)
        self._breaker.record_failure()
        raise last_exc or RuntimeError(f"{self.name}: unknown failure")

    @staticmethod
    def _parse_response(data: dict) -> LLMResponse:
        choice  = data["choices"][0]
        message = choice["message"]
        usage   = data.get("usage", {})
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            tool_calls.append(ToolCall(
                id        = tc["id"],
                name      = tc["function"]["name"],
                arguments = tc["function"].get("arguments", "{}"),
            ))
        return LLMResponse(
            content           = message.get("content"),
            tool_calls        = tool_calls,
            finish_reason     = "tool_calls" if tool_calls else choice.get("finish_reason", "stop"),
            prompt_tokens     = usage.get("prompt_tokens", 0),
            completion_tokens = usage.get("completion_tokens", 0),
        )


# ── Complexity classifier ────────────────────────────────────────────────────

def _classify_tier(prompt: str, cfg_routing: dict) -> str:
    """
    Return CHEAP / STANDARD / PREMIUM based on prompt characteristics.
    Uses thresholds + keyword signals from config.yaml routing section.
    """
    words              = len(prompt.split())
    lower              = prompt.lower()
    cheap_max          = cfg_routing.get("cheap_max_words", 30)
    standard_max       = cfg_routing.get("standard_max_words", 120)
    premium_keywords   = cfg_routing.get("premium_keywords", [])
    standard_keywords  = cfg_routing.get("standard_keywords", [])

    # Keyword signals override word-count
    if any(kw in lower for kw in premium_keywords):
        return PREMIUM
    if any(kw in lower for kw in standard_keywords):
        return STANDARD

    if words <= cheap_max:
        return CHEAP
    if words <= standard_max:
        return STANDARD
    return PREMIUM


# ── Config helpers ───────────────────────────────────────────────────────────

def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        # Handle ${VAR:-default} syntax
        def _repl(m: re.Match) -> str:
            parts = m.group(1).split(":-", 1)
            return os.getenv(parts[0], parts[1] if len(parts) > 1 else "")
        return re.sub(r"\$\{([^}]+)\}", _repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


# ── Simulated tool-calling ───────────────────────────────────────────────────

_SIM_PREFIX = """\
You have access to the following tools. To call a tool respond with EXACTLY:
TOOL_CALL: <tool_name>
ARGS: <json_args>

Then stop. I will execute the tool and return the result.
When you have your final answer, respond normally (no TOOL_CALL).

Available tools:
{tool_list}

"""

def _inject_tools(messages: list[dict], tools: list[dict]) -> list[dict]:
    tool_descriptions = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in tools
    )
    prefix = _SIM_PREFIX.format(tool_list=tool_descriptions)
    msgs = list(messages)
    if msgs and msgs[0]["role"] == "system":
        msgs[0] = {"role": "system", "content": prefix + msgs[0]["content"]}
    else:
        msgs.insert(0, {"role": "system", "content": prefix})
    return msgs

def _parse_simulated(resp: LLMResponse) -> LLMResponse:
    """Extract TOOL_CALL / ARGS from simulated tool-call response."""
    m = re.search(r"TOOL_CALL:\s*(\w+)\s*\nARGS:\s*(\{.*?\})", resp.text, re.DOTALL)
    if m:
        return LLMResponse(
            content       = None,
            tool_calls    = [ToolCall(id="sim_0", name=m.group(1), arguments=m.group(2))],
            finish_reason = "tool_calls",
        )
    return resp


# ── Router ───────────────────────────────────────────────────────────────────

class LLMRouter:
    """
    Multi-provider LLM router with automatic cost-aware tier selection.

    Usage:
        text = await router.complete("prompt")          # auto-tier
        text = await router.complete("prompt", tier="premium")  # forced
        resp = await router.complete_with_tools(msgs, tools)    # auto-tier
        async for tok in router.stream("prompt"): ...
    """

    def __init__(
        self,
        config_path: str | Path = _CONFIG_PATH,
        telemetry: "Telemetry | None" = None,
    ) -> None:
        raw = yaml.safe_load(Path(config_path).read_text())
        cfg = _expand_env(raw)

        self._system_prompt: str  = cfg.get("system_prompt", "You are XClaw.")
        self._routing_cfg: dict   = cfg.get("routing", {})
        self._loop_cfg: dict      = cfg.get("agent_loop", {})
        self._default_tier: str   = self._loop_cfg.get("default_tier", STANDARD)
        self._tool_tier: str      = self._loop_cfg.get("force_tool_calling_tier", STANDARD)
        self._telemetry           = telemetry

        # Build provider map: name → LLMProvider
        provider_map: dict[str, LLMProvider] = {}
        for p_cfg in cfg.get("providers", []):
            try:
                p = LLMProvider(p_cfg)
                provider_map[p.name] = p
            except Exception as exc:
                logger.warning("[router] skipping provider %s: %s", p_cfg.get("name", "?"), exc)

        # Build tier lists in declared priority order
        tier_order_cfg: dict[str, list[str]] = cfg.get("tier_order", {})
        self._tier_providers: dict[str, list[LLMProvider]] = {t: [] for t in _TIERS}
        for tier, names in tier_order_cfg.items():
            for name in names:
                if name in provider_map:
                    self._tier_providers[tier].append(provider_map[name])
                else:
                    logger.debug("[router] tier_order references unknown provider: %s", name)

        # Legacy: if no tier config, fall back to flat list (backwards compat)
        if not any(self._tier_providers.values()):
            primary   = LLMProvider(cfg["primary"])
            fallbacks = [LLMProvider(fb) for fb in cfg.get("fallbacks", [])]
            all_p = [primary, *fallbacks]
            for p in all_p:
                self._tier_providers[p.tier].append(p)

        total = sum(len(v) for v in self._tier_providers.values())
        logger.info("[router] loaded %d providers across %d tiers", total, len(_TIERS))

    # ── Public API ────────────────────────────────────────────────────────────

    async def complete(self, prompt: str, session_id: str = "", tier: str | None = None, **kwargs: Any) -> str:
        """Single-turn completion. Auto-selects cheapest capable tier unless overridden."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": prompt},
        ]
        resolved_tier = tier or _classify_tier(prompt, self._routing_cfg)
        resp = await self._dispatch(messages, tier=resolved_tier, **kwargs)
        self._record_usage(resp)
        logger.info("[router] complete via %s (tier=%s, %d+%d tok)",
                    resp.provider_used, resp.tier_used, resp.prompt_tokens, resp.completion_tokens)
        return resp.text

    async def chat(self, messages: list[dict], session_id: str = "", tier: str | None = None, **kwargs: Any) -> str:
        """Multi-turn completion."""
        resolved_tier = tier or self._default_tier
        resp = await self._dispatch(messages, tier=resolved_tier, **kwargs)
        self._record_usage(resp)
        return resp.text

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        session_id: str = "",
        tier: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Tool-calling completion. Prefers native tool calling; falls back to
        simulated (prompt-injection) for providers that don't support it.
        Forces at least `force_tool_calling_tier` to get a capable model.
        """
        # Ensure minimum tier for tool-heavy requests
        tier_order = {CHEAP: 0, STANDARD: 1, PREMIUM: 2}
        resolved_tier = tier or self._tool_tier
        if tier_order.get(resolved_tier, 0) < tier_order.get(self._tool_tier, 0):
            resolved_tier = self._tool_tier

        # Try tiers from resolved upward
        tiers_to_try = [t for t in _TIERS if tier_order[t] >= tier_order[resolved_tier]]

        for t in tiers_to_try:
            for provider in self._tier_providers.get(t, []):
                if not provider.is_available():
                    continue
                try:
                    if provider.supports_tool_calling:
                        logger.info("[router] tool-call via %s (tier=%s)", provider.name, t)
                        resp = await provider.complete_with_tools(messages, tools, **kwargs)
                    else:
                        logger.info("[router] simulated tool-call via %s (tier=%s)", provider.name, t)
                        sim_msgs = _inject_tools(messages, tools)
                        resp = await provider.complete(sim_msgs, **kwargs)
                        resp = _parse_simulated(resp)
                    resp.tier_used = t
                    self._record_usage(resp)
                    return resp
                except Exception as exc:
                    logger.warning("[router] %s tool-call failed: %s", provider.name, exc)

        raise RuntimeError(
            "All providers failed for tool calling. "
            "Set at least one API key (GROQ_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY / DO_API_KEY) in .env"
        )

    async def stream(self, prompt: str, session_id: str = "", tier: str | None = None, **kwargs: Any) -> AsyncGenerator[str, None]:
        """Stream tokens from cheapest available provider for the given tier."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": prompt},
        ]
        resolved_tier = tier or _classify_tier(prompt, self._routing_cfg)
        tiers_to_try  = [t for t in _TIERS if _TIERS.index(t) >= _TIERS.index(resolved_tier)]

        for t in tiers_to_try:
            for provider in self._tier_providers.get(t, []):
                if not provider.is_available():
                    continue
                try:
                    logger.info("[router] streaming via %s (tier=%s)", provider.name, t)
                    async for token in provider.stream(messages, **kwargs):
                        yield token
                    return
                except Exception as exc:
                    logger.warning("[router] %s stream failed: %s", provider.name, exc)

        # Ultimate fallback: complete() and yield whole response
        text = await self.complete(prompt, session_id=session_id, tier=resolved_tier, **kwargs)
        yield text

    # ── Internal dispatch ────────────────────────────────────────────────────

    async def _dispatch(self, messages: list[dict], tier: str = STANDARD, **kwargs: Any) -> LLMResponse:
        """Try providers in tier order, escalating if needed."""
        tier_order   = {CHEAP: 0, STANDARD: 1, PREMIUM: 2}
        tiers_to_try = [t for t in _TIERS if tier_order[t] >= tier_order.get(tier, 0)]

        last_error: Exception | None = None
        for t in tiers_to_try:
            for provider in self._tier_providers.get(t, []):
                if not provider.is_available():
                    continue
                try:
                    logger.info("[router] → %s/%s (tier=%s, cost=$%.3f/1M)",
                                provider.provider, provider.model, t, provider.cost_per_1m)
                    resp = await provider.complete(messages, **kwargs)
                    resp.tier_used = t
                    return resp
                except Exception as exc:
                    logger.warning("[router] ✗ %s: %s", provider.name, exc)
                    last_error = exc

        raise RuntimeError(
            f"All LLM providers exhausted (tried tiers {tiers_to_try}). Last error: {last_error}\n"
            "Set at least one API key in .env or install Ollama for local inference."
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _record_usage(self, resp: LLMResponse) -> None:
        if self._telemetry and (resp.prompt_tokens or resp.completion_tokens):
            self._telemetry.record_tokens(resp.provider_used, resp.prompt_tokens, resp.completion_tokens)

    def available_providers(self) -> list[str]:
        seen, out = set(), []
        for providers in self._tier_providers.values():
            for p in providers:
                if p.is_available() and p.provider not in seen:
                    seen.add(p.provider)
                    out.append(p.provider)
        return out

    def provider_status(self) -> list[dict]:
        seen, out = set(), []
        for tier in _TIERS:
            for p in self._tier_providers.get(tier, []):
                if p.name in seen:
                    continue
                seen.add(p.name)
                out.append({
                    "provider":      p.provider,
                    "name":          p.name,
                    "model":         p.model,
                    "tier":          p.tier,
                    "available":     p.is_available(),
                    "circuit_open":  p._breaker.is_open,
                    "failures":      p._breaker._failures,
                    "tool_calling":  p.supports_tool_calling,
                    "streaming":     p.supports_streaming,
                    "cost_per_1m":   p.cost_per_1m,
                })
        return out

    def routing_summary(self) -> dict:
        """Return a human-readable summary of active routing config."""
        summary = {}
        for tier in _TIERS:
            available = [
                f"{p.provider}/{p.model} (${p.cost_per_1m}/1M)"
                for p in self._tier_providers.get(tier, [])
                if p.is_available()
            ]
            summary[tier] = available or ["none available"]
        return summary
