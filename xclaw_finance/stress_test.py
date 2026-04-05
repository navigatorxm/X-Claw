"""
X-Claw Production-Grade Stress Testing Harness
===============================================

Simulates high-concurrency API usage to detect failures, race conditions,
and incorrect behavior under load.

Usage:
    python stress_test.py [config.json]

If no config file is provided, looks for stress_test_config.json in the
current directory.

Exit codes:
    0 — PASSED (error_rate < 1% AND success_rate > 60%)
    1 — FAILED
    2 — Configuration / setup error
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("xclaw.stress")


# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

@dataclass
class TestParams:
    """Nested test parameters with sensible defaults."""
    # Endpoint selection
    execute_ratio: float = 0.7
    approve_ratio: float = 0.3

    # Assets
    assets: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    asset_weights: list[float] = field(default_factory=lambda: [0.5, 0.3, 0.2])

    # Amount tiers: [min, max]
    amount_small: list[float] = field(default_factory=lambda: [0.001, 0.01])
    amount_medium: list[float] = field(default_factory=lambda: [0.01, 0.1])
    amount_large: list[float] = field(default_factory=lambda: [0.1, 1.0])
    amount_extreme: list[float] = field(default_factory=lambda: [1.0, 10.0])

    # Amount tier probabilities (must sum to 1.0)
    prob_small: float = 0.40
    prob_medium: float = 0.40
    prob_large: float = 0.15
    prob_extreme: float = 0.05

    # Timing
    timing_jitter_ms: int = 500

    # HTTP
    request_timeout_s: int = 30
    max_retries: int = 3

    # Approval
    approval_approve_ratio: float = 0.8
    # Refresh pending approvals list every N requests per worker
    pending_refresh_interval: int = 5

    # Output
    log_failures: bool = True
    failure_log_file: str = "stress_test_failures.jsonl"
    report_file: str = "stress_test_report.json"


@dataclass
class LoadTestConfig:
    """Top-level test configuration."""
    base_url: str
    api_key: str
    agent_id: str
    wallet_id: str
    total_requests: int
    concurrency: int
    test_params: TestParams = field(default_factory=TestParams)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}


class ConfigLoader:
    """Load and validate LoadTestConfig from a JSON file."""

    _REQUIRED = {"base_url", "api_key", "agent_id", "wallet_id", "total_requests", "concurrency"}

    @staticmethod
    def load(filepath: str) -> LoadTestConfig:
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raise SystemExit(f"[config] File not found: {filepath}")
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[config] JSON parse error in {filepath}: {exc}")

        missing = ConfigLoader._REQUIRED - raw.keys()
        if missing:
            raise SystemExit(f"[config] Missing required fields: {sorted(missing)}")

        if not isinstance(raw["total_requests"], int) or raw["total_requests"] <= 0:
            raise SystemExit("[config] total_requests must be a positive integer")
        if not isinstance(raw["concurrency"], int) or raw["concurrency"] <= 0:
            raise SystemExit("[config] concurrency must be a positive integer")
        if not raw["base_url"].startswith(("http://", "https://")):
            raise SystemExit("[config] base_url must start with http:// or https://")
        if not raw["api_key"].startswith("xclaw_"):
            raise SystemExit("[config] api_key must start with 'xclaw_'")

        # Build TestParams from nested "test_params" key, applying defaults
        tp_raw = raw.get("test_params", {})
        tp = TestParams(
            execute_ratio=tp_raw.get("execute_ratio", 0.7),
            approve_ratio=tp_raw.get("approve_ratio", 0.3),
            assets=tp_raw.get("assets", ["BTC", "ETH", "SOL"]),
            asset_weights=tp_raw.get("asset_weights", [0.5, 0.3, 0.2]),
            timing_jitter_ms=tp_raw.get("timing_jitter_ms", 500),
            request_timeout_s=tp_raw.get("request_timeout_s", 30),
            max_retries=tp_raw.get("max_retries", 3),
            approval_approve_ratio=tp_raw.get("approval_approve_ratio", 0.8),
            log_failures=tp_raw.get("log_failures", True),
            failure_log_file=tp_raw.get("failure_log_file", "stress_test_failures.jsonl"),
            report_file=tp_raw.get("report_file", "stress_test_report.json"),
        )
        # Amount ranges
        ranges = tp_raw.get("amount_ranges", {})
        if ranges.get("small"):
            tp.amount_small = ranges["small"]
        if ranges.get("medium"):
            tp.amount_medium = ranges["medium"]
        if ranges.get("large"):
            tp.amount_large = ranges["large"]
        if ranges.get("extreme"):
            tp.amount_extreme = ranges["extreme"]
        # Amount probabilities
        probs = tp_raw.get("amount_probabilities", {})
        if probs.get("small") is not None:
            tp.prob_small = probs["small"]
        if probs.get("medium") is not None:
            tp.prob_medium = probs["medium"]
        if probs.get("large") is not None:
            tp.prob_large = probs["large"]
        if probs.get("extreme") is not None:
            tp.prob_extreme = probs["extreme"]

        return LoadTestConfig(
            base_url=raw["base_url"].rstrip("/"),
            api_key=raw["api_key"],
            agent_id=raw["agent_id"],
            wallet_id=raw["wallet_id"],
            total_requests=raw["total_requests"],
            concurrency=raw["concurrency"],
            test_params=tp,
        )


# ---------------------------------------------------------------------------
# 2. Metrics
# ---------------------------------------------------------------------------

@dataclass
class _EndpointMetrics:
    requests: int = 0
    success: int = 0
    denied: int = 0
    pending: int = 0
    error: int = 0
    latencies_ms: list = field(default_factory=list)

    def avg_latency(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return sum(self.latencies_ms) / len(self.latencies_ms)


class MetricsCollector:
    """Thread-safe aggregation of request outcomes and latency data."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.success_count = 0
        self.denied_count = 0
        self.pending_count = 0
        self.error_count = 0
        self.latencies_ms: list[float] = []
        self.status_distribution: Counter = Counter()
        self._endpoints: dict[str, _EndpointMetrics] = {}

    # ------------------------------------------------------------------ write
    def _ep(self, endpoint: str) -> _EndpointMetrics:
        if endpoint not in self._endpoints:
            self._endpoints[endpoint] = _EndpointMetrics()
        return self._endpoints[endpoint]

    def record_success(self, endpoint: str, latency_ms: float, status_code: int) -> None:
        with self._lock:
            self.success_count += 1
            self.latencies_ms.append(latency_ms)
            self.status_distribution[status_code] += 1
            ep = self._ep(endpoint)
            ep.requests += 1
            ep.success += 1
            ep.latencies_ms.append(latency_ms)

    def record_denied(self, endpoint: str, latency_ms: float) -> None:
        with self._lock:
            self.denied_count += 1
            self.latencies_ms.append(latency_ms)
            self.status_distribution[403] += 1
            ep = self._ep(endpoint)
            ep.requests += 1
            ep.denied += 1
            ep.latencies_ms.append(latency_ms)

    def record_pending(self, endpoint: str, latency_ms: float) -> None:
        with self._lock:
            self.pending_count += 1
            self.latencies_ms.append(latency_ms)
            self.status_distribution[200] += 1
            ep = self._ep(endpoint)
            ep.requests += 1
            ep.pending += 1
            ep.latencies_ms.append(latency_ms)

    def record_error(self, endpoint: str, latency_ms: float, status_code: int) -> None:
        with self._lock:
            self.error_count += 1
            self.latencies_ms.append(latency_ms)
            self.status_distribution[status_code] += 1
            ep = self._ep(endpoint)
            ep.requests += 1
            ep.error += 1
            ep.latencies_ms.append(latency_ms)

    # ------------------------------------------------------------------ read
    @property
    def total(self) -> int:
        return self.success_count + self.denied_count + self.pending_count + self.error_count

    def compute_latency_stats(self) -> dict[str, float]:
        data = sorted(self.latencies_ms)
        if not data:
            return {"min": 0, "max": 0, "avg": 0, "median": 0, "p95": 0, "p99": 0}
        n = len(data)

        def percentile(p: float) -> float:
            idx = math.ceil(p / 100 * n) - 1
            return round(data[max(0, min(idx, n - 1))], 2)

        return {
            "min": round(data[0], 2),
            "max": round(data[-1], 2),
            "avg": round(sum(data) / n, 2),
            "median": round(data[n // 2], 2),
            "p95": percentile(95),
            "p99": percentile(99),
        }

    def per_endpoint_dict(self) -> dict[str, dict]:
        result = {}
        for ep_name, ep in self._endpoints.items():
            result[ep_name] = {
                "requests": ep.requests,
                "success": ep.success,
                "denied": ep.denied,
                "pending": ep.pending,
                "error": ep.error,
                "avg_latency_ms": round(ep.avg_latency(), 2),
            }
        return result

    def rates(self, total: Optional[int] = None) -> dict[str, float]:
        n = total or self.total
        if n == 0:
            return {"success_rate": 0, "denial_rate": 0, "pending_rate": 0, "error_rate": 0}
        return {
            "success_rate": round(self.success_count / n, 4),
            "denial_rate": round(self.denied_count / n, 4),
            "pending_rate": round(self.pending_count / n, 4),
            "error_rate": round(self.error_count / n, 4),
        }


# ---------------------------------------------------------------------------
# 3. Failure Logger
# ---------------------------------------------------------------------------

@dataclass
class FailureRecord:
    timestamp: str
    endpoint: str
    method: str
    status_code: int
    payload: dict
    response_body: str
    error_message: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "endpoint": self.endpoint,
            "method": self.method,
            "status_code": self.status_code,
            "payload": self.payload,
            "response_body": self.response_body[:1024],  # cap at 1 KB
            "error_message": self.error_message,
        }


class FailureLogger:
    """Log failed requests with full context; optionally persist to .jsonl file."""

    def __init__(self, filepath: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._buffer: list[FailureRecord] = []
        self._filepath = filepath
        self._error_counts: Counter = Counter()

    def log(
        self,
        endpoint: str,
        method: str,
        status_code: int,
        payload: dict,
        response_body: str,
        error_message: str,
    ) -> None:
        record = FailureRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            payload=payload,
            response_body=response_body,
            error_message=error_message,
        )
        with self._lock:
            self._buffer.append(record)
            self._error_counts[error_message] += 1
            if self._filepath:
                try:
                    with open(self._filepath, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record.to_dict()) + "\n")
                except OSError as exc:
                    log.warning("Could not write failure log: %s", exc)

    def get_failures(self) -> list[FailureRecord]:
        with self._lock:
            return list(self._buffer)

    def top_errors(self, n: int = 10) -> list[dict]:
        with self._lock:
            return [
                {"error": msg, "count": cnt}
                for msg, cnt in self._error_counts.most_common(n)
            ]

    def summary(self) -> str:
        failures = self.get_failures()
        if not failures:
            return "No failures recorded."
        lines = [f"  [{f.status_code}] {f.endpoint} — {f.error_message}" for f in failures[:20]]
        if len(failures) > 20:
            lines.append(f"  ... and {len(failures) - 20} more (see {self._filepath})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Request Factory
# ---------------------------------------------------------------------------

class RequestFactory:
    """Generate randomized trade and approval request payloads."""

    # Amount tier names in selection order
    _TIERS = ("small", "medium", "large", "extreme")

    def __init__(self, cfg: LoadTestConfig) -> None:
        self._cfg = cfg
        tp = cfg.test_params
        self._assets = tp.assets
        self._asset_weights = tp.asset_weights
        self._ranges: dict[str, list[float]] = {
            "small": tp.amount_small,
            "medium": tp.amount_medium,
            "large": tp.amount_large,
            "extreme": tp.amount_extreme,
        }
        self._tier_weights = [tp.prob_small, tp.prob_medium, tp.prob_large, tp.prob_extreme]

    def random_execute(self) -> dict:
        """Return a randomized /execute request body."""
        asset = random.choices(self._assets, weights=self._asset_weights, k=1)[0]
        tier = random.choices(self._TIERS, weights=self._tier_weights, k=1)[0]
        lo, hi = self._ranges[tier]
        amount = round(random.uniform(lo, hi), 6)
        side = random.choice(("buy", "sell"))
        return {
            "agent_id": self._cfg.agent_id,
            "wallet_id": self._cfg.wallet_id,
            "side": side,
            "asset": asset,
            "amount": str(amount),
            "quote": "USDT",
            "order_type": "market",
        }

    def random_approve(self, request_id: str) -> dict:
        """Return a randomized /approve request body for the given request_id."""
        approve_ratio = self._cfg.test_params.approval_approve_ratio
        decision = "approve" if random.random() < approve_ratio else "reject"
        return {
            "request_id": request_id,
            "decision": decision,
            "note": f"stress-test-{decision}",
            "execute_immediately": True,
        }

    def jitter(self) -> float:
        """Return a random delay in seconds (0 to timing_jitter_ms ms)."""
        max_ms = self._cfg.test_params.timing_jitter_ms
        return random.random() * max_ms / 1000.0


# ---------------------------------------------------------------------------
# 5. HTTP Client
# ---------------------------------------------------------------------------

class RequestResult:
    """Outcome of a single HTTP request."""
    __slots__ = ("endpoint", "method", "status_code", "body", "latency_ms", "error")

    def __init__(
        self,
        endpoint: str,
        method: str,
        status_code: int,
        body: Any,
        latency_ms: float,
        error: str = "",
    ) -> None:
        self.endpoint = endpoint
        self.method = method
        self.status_code = status_code
        self.body = body
        self.latency_ms = latency_ms
        self.error = error

    @property
    def is_ok(self) -> bool:
        return self.status_code == 200 and not self.error

    @property
    def is_transient_error(self) -> bool:
        return self.status_code in (500, 502, 503)


class HttpClient:
    """Async HTTP client wrapping httpx with retries and timeout handling."""

    _TRANSIENT_CODES = {500, 502, 503}
    _RETRY_DELAYS = (1.0, 2.0, 4.0)

    def __init__(self, cfg: LoadTestConfig) -> None:
        self._base = cfg.base_url
        self._headers = cfg.headers
        self._timeout = cfg.test_params.request_timeout_s
        self._max_retries = cfg.test_params.max_retries
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "HttpClient":
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=self._headers,
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def _post(self, path: str, payload: dict) -> RequestResult:
        assert self._client is not None, "HttpClient not entered as async context manager"
        attempt = 0
        while True:
            t0 = time.monotonic()
            status = -1
            body: Any = {}
            error = ""
            try:
                resp = await self._client.post(path, json=payload)
                latency_ms = (time.monotonic() - t0) * 1000
                status = resp.status_code
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
            except httpx.TimeoutException as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                error = f"timeout: {exc}"
            except httpx.RequestError as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                error = f"request error: {exc}"

            # Retry logic for transient failures
            if (status in self._TRANSIENT_CODES or (error and "timeout" in error)) and attempt < self._max_retries:
                delay = self._RETRY_DELAYS[min(attempt, len(self._RETRY_DELAYS) - 1)]
                log.debug("Retry %d/%d for %s after %.1fs (status=%s err=%s)",
                          attempt + 1, self._max_retries, path, delay, status, error or "-")
                await asyncio.sleep(delay)
                attempt += 1
                continue

            return RequestResult(
                endpoint=path,
                method="POST",
                status_code=status,
                body=body,
                latency_ms=latency_ms,
                error=error,
            )

    async def _get(self, path: str) -> RequestResult:
        assert self._client is not None
        t0 = time.monotonic()
        status = -1
        body: Any = {}
        error = ""
        try:
            resp = await self._client.get(path)
            latency_ms = (time.monotonic() - t0) * 1000
            status = resp.status_code
            try:
                body = resp.json()
            except Exception:
                body = resp.text
        except httpx.TimeoutException as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            error = f"timeout: {exc}"
        except httpx.RequestError as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            error = f"request error: {exc}"
        return RequestResult(
            endpoint=path,
            method="GET",
            status_code=status,
            body=body,
            latency_ms=latency_ms,
            error=error,
        )

    async def execute_trade(self, payload: dict) -> RequestResult:
        return await self._post("/execute", payload)

    async def approve_request(self, payload: dict) -> RequestResult:
        return await self._post("/approve", payload)

    async def get_pending_approvals(self) -> RequestResult:
        return await self._get("/approve/pending")

    async def get_agent_me(self) -> RequestResult:
        return await self._get("/auth/agents/me")


# ---------------------------------------------------------------------------
# 6. Report Generator
# ---------------------------------------------------------------------------

class ReportGenerator:
    """Build JSON and human-readable reports from collected metrics."""

    def __init__(
        self,
        cfg: LoadTestConfig,
        metrics: MetricsCollector,
        failures: FailureLogger,
        start_time: float,
        end_time: float,
    ) -> None:
        self._cfg = cfg
        self._metrics = metrics
        self._failures = failures
        self._start = start_time
        self._end = end_time

    # ------------------------------------------------------------------ verdict
    def _verdict(self, rates: dict) -> str:
        if rates["error_rate"] < 0.01 and rates["success_rate"] >= 0.60:
            return "PASSED"
        return "FAILED"

    # ------------------------------------------------------------------ JSON
    def build_json(self) -> dict:
        duration = self._end - self._start
        tp = self._cfg.test_params
        completed = self._metrics.total
        rates = self._metrics.rates(completed)

        report = {
            "metadata": {
                "timestamp_start": datetime.fromtimestamp(self._start, tz=timezone.utc).isoformat(),
                "timestamp_end": datetime.fromtimestamp(self._end, tz=timezone.utc).isoformat(),
                "duration_seconds": round(duration, 2),
                "base_url": self._cfg.base_url,
                "agent_id": self._cfg.agent_id,
                "wallet_id": self._cfg.wallet_id,
            },
            "parameters": {
                "total_requests_target": self._cfg.total_requests,
                "concurrency": self._cfg.concurrency,
                "execute_ratio": tp.execute_ratio,
                "approve_ratio": tp.approve_ratio,
                "assets": tp.assets,
                "timing_jitter_ms": tp.timing_jitter_ms,
            },
            "summary": {
                "requests_completed": completed,
                "success_count": self._metrics.success_count,
                "denied_count": self._metrics.denied_count,
                "pending_count": self._metrics.pending_count,
                "error_count": self._metrics.error_count,
            },
            "rates": rates,
            "latency_ms": self._metrics.compute_latency_stats(),
            "status_distribution": dict(self._metrics.status_distribution),
            "per_endpoint": self._metrics.per_endpoint_dict(),
            "failures": [f.to_dict() for f in self._failures.get_failures()],
            "top_errors": self._failures.top_errors(10),
            "verdict": self._verdict(rates),
        }
        return report

    def save_json(self, report: dict) -> None:
        try:
            with open(self._cfg.test_params.report_file, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, default=str)
        except OSError as exc:
            log.warning("Could not write JSON report: %s", exc)

    # ------------------------------------------------------------------ stdout
    def print_summary(self, report: dict) -> None:
        dur = report["metadata"]["duration_seconds"]
        mins, secs = divmod(int(dur), 60)
        duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        completed = report["summary"]["requests_completed"]
        target = report["parameters"]["total_requests_target"]
        rates = report["rates"]
        lat = report["latency_ms"]
        verdict = report["verdict"]
        verdict_symbol = "✓" if verdict == "PASSED" else "✗"

        print()
        print("=" * 50)
        print("  X-Claw Stress Test Report")
        print("=" * 50)
        print(f"  Duration  : {duration_str}")
        print(f"  Requests  : {completed}/{target} completed  (concurrency={report['parameters']['concurrency']})")
        print()
        print("[SUMMARY]")
        print(f"  Success  : {report['summary']['success_count']:>6}  ({rates['success_rate']:.1%})")
        print(f"  Denied   : {report['summary']['denied_count']:>6}  ({rates['denial_rate']:.1%})")
        print(f"  Pending  : {report['summary']['pending_count']:>6}  ({rates['pending_rate']:.1%})")
        print(f"  Error    : {report['summary']['error_count']:>6}  ({rates['error_rate']:.1%})")
        print()
        print("[LATENCY]")
        print(f"  Min: {lat['min']}ms   Max: {lat['max']}ms")
        print(f"  Avg: {lat['avg']}ms   Median: {lat['median']}ms")
        print(f"  P95: {lat['p95']}ms   P99: {lat['p99']}ms")

        if report["status_distribution"]:
            print()
            print("[STATUS CODES]")
            dist_str = "  " + "  ".join(
                f"{code}: {cnt}"
                for code, cnt in sorted(report["status_distribution"].items())
            )
            print(dist_str)

        if report["per_endpoint"]:
            print()
            print("[PER ENDPOINT]")
            for ep, stats in report["per_endpoint"].items():
                print(
                    f"  {ep:<20}  req={stats['requests']}  ok={stats['success']}"
                    f"  denied={stats['denied']}  err={stats['error']}"
                    f"  avg={stats['avg_latency_ms']}ms"
                )

        if report["top_errors"]:
            print()
            print("[TOP ERRORS]")
            for item in report["top_errors"]:
                print(f"  - {item['error'][:80]} ({item['count']}x)")

        tp = self._cfg.test_params
        print()
        print(f"  VERDICT: {verdict} {verdict_symbol}")
        print(f"  Full report  : {tp.report_file}")
        if tp.log_failures and report["summary"]["error_count"] > 0:
            print(f"  Failures log : {tp.failure_log_file}")
        print("=" * 50)
        print()


# ---------------------------------------------------------------------------
# 7. Stress Test Runner
# ---------------------------------------------------------------------------

class StressTestRunner:
    """
    Main async orchestrator.

    Spawns N concurrent worker coroutines (N = concurrency).
    Each worker sends randomized /execute and /approve requests until
    the shared request counter is exhausted.
    """

    def __init__(self, cfg: LoadTestConfig) -> None:
        self._cfg = cfg
        self._metrics = MetricsCollector()
        tp = cfg.test_params
        self._failures = FailureLogger(
            filepath=tp.failure_log_file if tp.log_failures else None
        )
        self._factory = RequestFactory(cfg)
        # Shared counter — workers atomically decrement this
        self._remaining = cfg.total_requests
        self._remaining_lock = asyncio.Lock()
        # Pending approval IDs shared across workers
        self._pending_ids: list[str] = []
        self._pending_lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    async def _validate_api_key(self, client: HttpClient) -> None:
        result = await client.get_agent_me()
        if result.status_code == 401:
            raise SystemExit(
                "[setup] API key rejected (401). Ensure the agent exists and the key is valid."
            )
        if result.status_code == 403:
            raise SystemExit(
                "[setup] API key lacks required permissions (403). "
                "Agent needs EXECUTE + APPROVE permissions."
            )
        if result.is_ok:
            info = result.body
            agent_id = info.get("agent_id", "?")
            role = info.get("role", "?")
            perms = info.get("permissions", [])
            log.info(
                "Authenticated as agent_id='%s' role='%s' permissions=%s",
                agent_id, role, perms,
            )
            # Warn if APPROVE permission is missing (approve tests will all fail)
            if "approve" not in perms:
                log.warning(
                    "Agent '%s' lacks APPROVE permission. All /approve requests will be denied.",
                    agent_id,
                )
        else:
            log.warning(
                "GET /auth/agents/me returned status=%d — proceeding anyway",
                result.status_code,
            )

    # ------------------------------------------------------------------ approvals
    async def _refresh_pending(self, client: HttpClient) -> None:
        """Fetch the current pending approval list and cache request IDs."""
        result = await client.get_pending_approvals()
        if result.is_ok and isinstance(result.body, dict):
            ids = [r["request_id"] for r in result.body.get("requests", [])]
            async with self._pending_lock:
                self._pending_ids = ids

    # ------------------------------------------------------------------ workers
    async def _claim_slot(self) -> bool:
        """Atomically claim a request slot. Returns False if quota exhausted."""
        async with self._remaining_lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True

    async def _worker(self, worker_id: int, client: HttpClient) -> None:
        """Single concurrent worker loop."""
        tp = self._cfg.test_params
        since_refresh = 0

        while await self._claim_slot():
            # Refresh pending approval list periodically
            since_refresh += 1
            if since_refresh >= tp.pending_refresh_interval:
                await self._refresh_pending(client)
                since_refresh = 0

            # Choose endpoint
            roll = random.random()
            use_execute = roll < tp.execute_ratio

            if use_execute:
                await self._do_execute(client)
            else:
                await self._do_approve(client)

            # Jitter between requests
            if tp.timing_jitter_ms > 0:
                await asyncio.sleep(self._factory.jitter())

    async def _do_execute(self, client: HttpClient) -> None:
        payload = self._factory.random_execute()
        result = await client.execute_trade(payload)

        if result.error:
            self._metrics.record_error("/execute", result.latency_ms, result.status_code)
            self._failures.log(
                endpoint="/execute", method="POST",
                status_code=result.status_code,
                payload=payload,
                response_body=str(result.body),
                error_message=result.error,
            )
            log.debug("[execute] network error: %s", result.error)
            return

        body = result.body if isinstance(result.body, dict) else {}
        status_val = body.get("status", "")

        if result.status_code == 200 and status_val == "executed":
            self._metrics.record_success("/execute", result.latency_ms, 200)
            log.debug("[execute] OK — filled %.6s %s", payload["amount"], payload["asset"])

        elif result.status_code == 200 and status_val == "pending":
            self._metrics.record_pending("/execute", result.latency_ms)
            # Cache the pending request_id for approval workers
            apr_id = body.get("approval_request_id", "")
            if apr_id:
                async with self._pending_lock:
                    self._pending_ids.append(apr_id)
            log.debug("[execute] pending approval %s", apr_id)

        elif result.status_code == 403:
            # Two cases: permission-denied (our agent config error) vs trade-denied (policy/risk)
            detail = body.get("detail", {})
            if isinstance(detail, dict) and detail.get("status") == "denied":
                self._metrics.record_denied("/execute", result.latency_ms)
                log.debug("[execute] denied — %s", detail.get("reason", "?"))
            else:
                # Auth / scope error — this is a harness configuration problem
                self._metrics.record_error("/execute", result.latency_ms, 403)
                self._failures.log(
                    endpoint="/execute", method="POST",
                    status_code=403,
                    payload=payload,
                    response_body=str(body),
                    error_message=f"403 permission/scope error: {detail}",
                )
                log.warning("[execute] unexpected 403: %s", detail)

        else:
            self._metrics.record_error("/execute", result.latency_ms, result.status_code)
            self._failures.log(
                endpoint="/execute", method="POST",
                status_code=result.status_code,
                payload=payload,
                response_body=str(body)[:512],
                error_message=f"unexpected status {result.status_code}",
            )
            log.debug("[execute] error status=%d body=%s", result.status_code, str(body)[:80])

    async def _do_approve(self, client: HttpClient) -> None:
        # Grab a pending request ID
        async with self._pending_lock:
            if not self._pending_ids:
                pending_id = None
            else:
                pending_id = random.choice(self._pending_ids)

        if not pending_id:
            # No pending requests yet — fall back to an execute instead
            await self._do_execute(client)
            return

        payload = self._factory.random_approve(pending_id)
        result = await client.approve_request(payload)

        if result.error:
            self._metrics.record_error("/approve", result.latency_ms, result.status_code)
            self._failures.log(
                endpoint="/approve", method="POST",
                status_code=result.status_code,
                payload=payload,
                response_body=str(result.body),
                error_message=result.error,
            )
            return

        body = result.body if isinstance(result.body, dict) else {}
        status_val = body.get("status", "")

        if result.status_code == 200 and status_val in ("approved", "rejected"):
            self._metrics.record_success("/approve", result.latency_ms, 200)
            # Remove the handled request_id from the pending list
            async with self._pending_lock:
                try:
                    self._pending_ids.remove(pending_id)
                except ValueError:
                    pass
            log.debug("[approve] %s — %s", status_val, pending_id)

        elif result.status_code == 404:
            # Request was already handled by another worker — not an error
            async with self._pending_lock:
                try:
                    self._pending_ids.remove(pending_id)
                except ValueError:
                    pass
            log.debug("[approve] 404 — already handled: %s", pending_id)
            # Count as success since the item was correctly processed
            self._metrics.record_success("/approve", result.latency_ms, 404)

        elif result.status_code == 409:
            # Not pending (already decided) — not a real error
            async with self._pending_lock:
                try:
                    self._pending_ids.remove(pending_id)
                except ValueError:
                    pass
            self._metrics.record_success("/approve", result.latency_ms, 409)
            log.debug("[approve] 409 — already decided: %s", pending_id)

        else:
            self._metrics.record_error("/approve", result.latency_ms, result.status_code)
            self._failures.log(
                endpoint="/approve", method="POST",
                status_code=result.status_code,
                payload=payload,
                response_body=str(body)[:512],
                error_message=f"unexpected status {result.status_code}",
            )
            log.debug("[approve] error status=%d", result.status_code)

    # ------------------------------------------------------------------ main
    async def run(self) -> int:
        """Run the full stress test. Returns exit code (0=PASSED, 1=FAILED)."""
        cfg = self._cfg
        log.info(
            "Starting stress test: %d requests, concurrency=%d, target=%s",
            cfg.total_requests, cfg.concurrency, cfg.base_url,
        )

        start_time = time.monotonic()

        async with HttpClient(cfg) as client:
            # Validate credentials
            await self._validate_api_key(client)

            # Pre-warm the pending list
            await self._refresh_pending(client)

            # Spawn N concurrent workers
            tasks = [
                asyncio.create_task(self._worker(i, client))
                for i in range(cfg.concurrency)
            ]
            await asyncio.gather(*tasks)

        end_time = time.monotonic()
        elapsed = end_time - start_time
        log.info(
            "Test complete — %d requests in %.1fs (%.1f req/s)",
            self._metrics.total,
            elapsed,
            self._metrics.total / elapsed if elapsed > 0 else 0,
        )

        # Generate report
        reporter = ReportGenerator(
            cfg=cfg,
            metrics=self._metrics,
            failures=self._failures,
            start_time=start_time + time.time() - time.monotonic(),  # convert to wall clock
            end_time=end_time + time.time() - time.monotonic(),
        )
        report = reporter.build_json()
        reporter.save_json(report)
        reporter.print_summary(report)

        return 0 if report["verdict"] == "PASSED" else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "stress_test_config.json"
    cfg = ConfigLoader.load(config_path)
    runner = StressTestRunner(cfg)
    exit_code = asyncio.run(runner.run())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
