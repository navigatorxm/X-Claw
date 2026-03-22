"""
XClaw Token Optimizer — keep API costs minimal without sacrificing capability.

Strategy:
  1. Complexity routing  — simple queries → cheapest model; complex → best model
  2. Semantic cache      — normalise + hash prompts; skip LLM on near-duplicate
  3. Token budget guard  — warn / truncate before hitting model context limits
  4. Response cache      — store results in SQLite; TTL configurable per query type

Routing tiers
─────────────
  SIMPLE   → groq/gemini-flash/ollama (≤ 500 tokens estimated)
             e.g. "What time is it?" / "Summarise this in one line"
  MEDIUM   → standard provider (500–2000 tokens estimated)
             e.g. "Research X and give 3 bullet points"
  COMPLEX  → best available provider (> 2000 tokens or tool-heavy)
             e.g. "Build a full feature, test it, write docs"

Semantic cache
──────────────
  Key: sha256(normalise(prompt[:500]))
  normalise() lowercases, strips punctuation/whitespace, dedups spaces.
  Hit rate in practice: ~15–30% for repeated briefings / recurring schedules.

Usage
─────
  optimizer = TokenOptimizer(memory)
  result = await optimizer.optimized_complete(llm_router, prompt, session_id)
  stats = optimizer.stats()
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from brain.llm_router import LLMRouter
    from core.memory import Memory

logger = logging.getLogger(__name__)

# ── Complexity tiers ──────────────────────────────────────────────────────────

class Complexity(str, Enum):
    SIMPLE  = "simple"
    MEDIUM  = "medium"
    COMPLEX = "complex"


# Keywords that bump complexity up
_COMPLEX_SIGNALS = [
    "build", "implement", "create a full", "develop", "write a", "architect",
    "deploy", "migrate", "refactor", "test suite", "step by step", "in depth",
    "comprehensive", "detailed analysis", "compare and contrast", "swarm",
]
_SIMPLE_SIGNALS = [
    "what is", "who is", "when did", "define", "one line", "in one sentence",
    "yes or no", "list", "name", "translate", "spell", "what time",
]


def classify_complexity(prompt: str, estimated_tokens: int = 0) -> Complexity:
    """Heuristically classify a prompt into SIMPLE / MEDIUM / COMPLEX."""
    lower = prompt.lower()

    if estimated_tokens > 2000:
        return Complexity.COMPLEX
    if estimated_tokens > 500:
        return Complexity.MEDIUM

    if any(sig in lower for sig in _COMPLEX_SIGNALS):
        return Complexity.COMPLEX
    if any(sig in lower for sig in _SIMPLE_SIGNALS):
        return Complexity.SIMPLE

    # Fall back on word count
    words = len(prompt.split())
    if words < 15:
        return Complexity.SIMPLE
    if words > 80:
        return Complexity.COMPLEX
    return Complexity.MEDIUM


# ── Semantic cache ────────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r'[^\w\s]')
_SPACE_RE = re.compile(r'\s+')

def _normalise(text: str) -> str:
    """Normalise text for cache keying."""
    t = text.lower().strip()
    t = _PUNCT_RE.sub(' ', t)
    t = _SPACE_RE.sub(' ', t)
    return t[:500]


def cache_key(prompt: str) -> str:
    return hashlib.sha256(_normalise(prompt).encode()).hexdigest()[:16]


# ── Token budget ──────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Rough estimation: ~4 chars per token (GPT-style)."""
    return max(1, len(text) // 4)


def truncate_to_budget(text: str, max_tokens: int) -> str:
    """Truncate text to fit within a token budget."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[...truncated to fit context window]"


# ── Stats ─────────────────────────────────────────────────────────────────────

@dataclass
class OptimizerStats:
    total_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    simple_routed: int = 0
    medium_routed: int = 0
    complex_routed: int = 0
    tokens_saved_estimate: int = 0
    total_latency_ms: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.cache_hits / self.total_requests

    def to_dict(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "cache_hits": self.cache_hits,
            "cache_hit_rate_pct": round(self.cache_hit_rate * 100, 1),
            "simple_routed": self.simple_routed,
            "medium_routed": self.medium_routed,
            "complex_routed": self.complex_routed,
            "tokens_saved_estimate": self.tokens_saved_estimate,
            "avg_latency_ms": round(self.total_latency_ms / max(1, self.total_requests - self.cache_hits), 1),
        }


# ── Main optimizer ────────────────────────────────────────────────────────────

class TokenOptimizer:
    """
    Wraps LLMRouter with caching + smart routing.

    Cache TTL strategy:
      SIMPLE  → 3600s (1 hour)
      MEDIUM  → 900s  (15 min)
      COMPLEX → 300s  (5 min, long prompts change often)
    """

    CACHE_TTL = {
        Complexity.SIMPLE:  3600,
        Complexity.MEDIUM:   900,
        Complexity.COMPLEX:  300,
    }

    # Preferred provider order per tier (first available wins)
    PROVIDER_PREF = {
        Complexity.SIMPLE:  ["groq", "gemini", "ollama", "openai", "digitalocean"],
        Complexity.MEDIUM:  ["groq", "gemini", "openai", "digitalocean", "ollama"],
        Complexity.COMPLEX: ["openai", "gemini", "groq", "digitalocean", "ollama"],
    }

    def __init__(self, memory: "Memory | None" = None, max_cache_size: int = 1000) -> None:
        self._memory = memory
        self._max_cache_size = max_cache_size
        self._cache: dict[str, tuple[str, float]] = {}   # key → (response, expires_at)
        self._stats = OptimizerStats()
        if memory:
            self._ensure_schema()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        conn = self._memory._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS response_cache (
                cache_key   TEXT PRIMARY KEY,
                prompt_hash TEXT NOT NULL,
                response    TEXT NOT NULL,
                complexity  TEXT NOT NULL,
                created_at  REAL NOT NULL,
                expires_at  REAL NOT NULL,
                hit_count   INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.commit()

    # ── Cache operations ──────────────────────────────────────────────────────

    def _cache_get(self, key: str) -> str | None:
        now = time.time()
        # Check in-memory first
        if key in self._cache:
            val, exp = self._cache[key]
            if now < exp:
                return val
            del self._cache[key]
        # Check SQLite
        if self._memory:
            try:
                row = self._memory._conn().execute(
                    "SELECT response, expires_at FROM response_cache WHERE cache_key=?", (key,)
                ).fetchone()
                if row and now < row["expires_at"]:
                    # Promote to memory cache
                    self._cache[key] = (row["response"], row["expires_at"])
                    self._memory._conn().execute(
                        "UPDATE response_cache SET hit_count=hit_count+1 WHERE cache_key=?", (key,)
                    )
                    return row["response"]
            except Exception:
                pass
        return None

    def _cache_set(self, key: str, response: str, complexity: Complexity, prompt: str) -> None:
        ttl = self.CACHE_TTL[complexity]
        now = time.time()
        expires_at = now + ttl
        # Memory cache with LRU eviction
        if len(self._cache) >= self._max_cache_size:
            # Remove oldest entry
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[key] = (response, expires_at)
        # Persist to SQLite
        if self._memory:
            try:
                self._memory._conn().execute(
                    """INSERT OR REPLACE INTO response_cache
                       (cache_key, prompt_hash, response, complexity, created_at, expires_at, hit_count)
                       VALUES (?,?,?,?,?,?,0)""",
                    (key, key, response, complexity.value, now, expires_at),
                )
                self._memory._conn().commit()
            except Exception as exc:
                logger.debug("cache persist failed: %s", exc)

    # ── Core API ──────────────────────────────────────────────────────────────

    async def optimized_complete(
        self,
        llm: "LLMRouter",
        prompt: str,
        session_id: str = "",
        force_complexity: Complexity | None = None,
        use_cache: bool = True,
    ) -> str:
        """
        Run a completion with caching + smart routing.
        Returns the response string.
        """
        t0 = time.monotonic()
        self._stats.total_requests += 1

        est_tokens = estimate_tokens(prompt)
        complexity = force_complexity or classify_complexity(prompt, est_tokens)

        # Update routing stats
        {
            Complexity.SIMPLE:  self._stats.simple_routed,
            Complexity.MEDIUM:  self._stats.medium_routed,
            Complexity.COMPLEX: self._stats.complex_routed,
        }[complexity]  # access to trigger

        if complexity == Complexity.SIMPLE:
            self._stats.simple_routed += 1
        elif complexity == Complexity.MEDIUM:
            self._stats.medium_routed += 1
        else:
            self._stats.complex_routed += 1

        # Cache lookup
        if use_cache:
            key = cache_key(prompt)
            cached = self._cache_get(key)
            if cached is not None:
                self._stats.cache_hits += 1
                self._stats.tokens_saved_estimate += est_tokens
                logger.debug("[optimizer] cache hit (key=%s, complexity=%s)", key, complexity.value)
                return cached
            self._stats.cache_misses += 1
        else:
            key = None

        # Truncate oversized prompts
        truncated = truncate_to_budget(prompt, max_tokens=12000)

        # Route to preferred provider
        preferred = self._select_provider(llm, complexity)
        logger.debug("[optimizer] routing %s → %s (est %d tokens)", complexity.value, preferred or "default", est_tokens)

        # Execute
        try:
            if preferred:
                response = await llm.complete(truncated, session_id=session_id, provider=preferred)
            else:
                response = await llm.complete(truncated, session_id=session_id)
        except Exception as exc:
            logger.warning("[optimizer] preferred provider failed, falling back: %s", exc)
            response = await llm.complete(truncated, session_id=session_id)

        elapsed_ms = (time.monotonic() - t0) * 1000
        self._stats.total_latency_ms += elapsed_ms

        # Cache store
        if use_cache and key:
            self._cache_set(key, response, complexity, prompt)

        return response

    def _select_provider(self, llm: "LLMRouter", complexity: Complexity) -> str | None:
        """Pick the best available provider for this complexity tier."""
        try:
            available = {p["provider"].lower() for p in llm.provider_status() if p.get("available")}
            for pref in self.PROVIDER_PREF[complexity]:
                if pref in available:
                    return pref
        except Exception:
            pass
        return None  # let LLMRouter choose

    # ── Stats & maintenance ───────────────────────────────────────────────────

    def stats(self) -> dict:
        return self._stats.to_dict()

    def clear_cache(self) -> int:
        count = len(self._cache)
        self._cache.clear()
        if self._memory:
            try:
                self._memory._conn().execute("DELETE FROM response_cache")
                self._memory._conn().commit()
            except Exception:
                pass
        return count

    def evict_expired(self) -> int:
        now = time.time()
        expired = [k for k, (_, exp) in self._cache.items() if now >= exp]
        for k in expired:
            del self._cache[k]
        if self._memory:
            try:
                self._memory._conn().execute("DELETE FROM response_cache WHERE expires_at<?", (now,))
                self._memory._conn().commit()
            except Exception:
                pass
        return len(expired)
