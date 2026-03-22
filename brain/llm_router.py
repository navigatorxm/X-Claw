"""
XClaw LLM Router — tries the primary LLM provider, falls back to alternatives.

Priority order (configured in brain/config.yaml):
  1. OVH AI Endpoints  (local, private, free)
  2. Groq              (fast cloud)
  3. Gemini            (Google cloud)
  4. OpenAI            (last resort)

All providers expose the same OpenAI-compatible chat completion API, so a
single HTTP client handles all of them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _expand_env(value: str) -> str:
    """Replace ${VAR} tokens with environment variable values."""
    import re
    return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), value)


def _resolve(cfg: dict) -> dict:
    """Recursively expand env vars in a config dict."""
    out = {}
    for k, v in cfg.items():
        if isinstance(v, str):
            out[k] = _expand_env(v)
        elif isinstance(v, dict):
            out[k] = _resolve(v)
        else:
            out[k] = v
    return out


class LLMProvider:
    """A single configured LLM endpoint."""

    def __init__(self, cfg: dict) -> None:
        self.provider = cfg["provider"]
        self.model = cfg["model"]
        self.api_key = cfg.get("api_key", "")
        self.timeout = cfg.get("timeout_seconds", 30)
        self.max_tokens = cfg.get("max_tokens", 2048)
        self.temperature = cfg.get("temperature", 0.3)

        # OVH uses a custom base URL; others use provider defaults
        if cfg.get("base_url"):
            self.base_url = cfg["base_url"].rstrip("/")
        else:
            self.base_url = self._default_base_url()

    def _default_base_url(self) -> str:
        defaults = {
            "groq": "https://api.groq.com/openai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
            "openai": "https://api.openai.com/v1",
            "ovh": "",
        }
        return defaults.get(self.provider, "https://api.openai.com/v1")

    def is_available(self) -> bool:
        return bool(self.api_key and self.base_url)

    async def complete(self, messages: list[dict], **kwargs: Any) -> str:
        """
        Call the chat completions endpoint and return the assistant message text.
        Uses only the stdlib (urllib) to avoid mandatory dependencies.
        """
        import urllib.request

        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
        }).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )

        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None,
            lambda: self._sync_request(req),
        )
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"]

    def _sync_request(self, req: Any) -> bytes:
        import urllib.request
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return resp.read()


class LLMRouter:
    """
    Tries the primary provider first; on failure cascades through fallbacks.

    Args:
        config_path: Path to brain/config.yaml (defaults to the bundled one).
    """

    def __init__(self, config_path: str | Path = _CONFIG_PATH) -> None:
        raw = yaml.safe_load(Path(config_path).read_text())
        resolved = _resolve(raw)

        self._system_prompt: str = resolved.get("system_prompt", "You are XClaw.")
        self._primary = LLMProvider(resolved["primary"])
        self._fallbacks = [LLMProvider(fb) for fb in resolved.get("fallbacks", [])]
        self._providers: list[LLMProvider] = [self._primary, *self._fallbacks]

    async def complete(self, prompt: str, session_id: str = "", **kwargs: Any) -> str:
        """
        Send `prompt` to the first available provider that succeeds.

        Returns the assistant's text response.
        Raises RuntimeError if every provider fails.
        """
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]

        last_error: Exception | None = None
        for provider in self._providers:
            if not provider.is_available():
                logger.debug("Skipping %s (no key/url configured)", provider.provider)
                continue
            try:
                logger.info("[llm] trying %s (%s)", provider.provider, provider.model)
                result = await provider.complete(messages, **kwargs)
                logger.info("[llm] success via %s", provider.provider)
                return result
            except Exception as exc:  # noqa: BLE001
                logger.warning("[llm] %s failed: %s", provider.provider, exc)
                last_error = exc

        raise RuntimeError(
            f"All LLM providers failed. Last error: {last_error}"
        )

    def available_providers(self) -> list[str]:
        return [p.provider for p in self._providers if p.is_available()]
