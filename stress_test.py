"""
X-Claw Stress Testing Harness
Production-grade async load test for /execute and /approve endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class TestConfig:
    api_url: str = "http://localhost:8000"
    api_key: str = ""
    agent_id: str = "stress-test-agent"
    wallet_id: str = ""
    total_requests: int = 100
    concurrency: int = 10
    endpoints: list[str] = field(default_factory=lambda: ["execute"])
    assets: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL", "BNB"])
    amount_min: float = 0.001
    amount_max: float = 1.0
    jitter_min_ms: int = 0
    jitter_max_ms: int = 200
    timeout_seconds: float = 30.0
    log_file: str | None = "stress_test.log"

    @classmethod
    def from_file(cls, path: str | Path) -> "TestConfig":
        raw = json.loads(Path(path).read_text())
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class RequestResult:
    endpoint: str
    status_code: int | None
    response_status: str | None     # "executed" | "pending" | "denied" | "error" | None
    latency_ms: float
    success: bool
    payload: dict[str, Any]
    response_body: dict[str, Any] | None
    error: str | None


@dataclass
class Metrics:
    total: int = 0
    success: int = 0
    denied: int = 0
    approval_required: int = 0
    error: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    failures: list[RequestResult] = field(default_factory=list)

    def record(self, result: RequestResult) -> None:
        self.total += 1
        self.latencies_ms.append(result.latency_ms)

        if result.error or result.status_code is None:
            self.error += 1
            self.failures.append(result)
            return

        rs = result.response_status
        if rs == "executed" or result.status_code == 200:
            self.success += 1
        elif rs == "pending" or result.status_code == 202:
            self.approval_required += 1
        elif rs == "denied" or result.status_code == 403:
            self.denied += 1
        else:
            self.error += 1
            self.failures.append(result)

    # Derived stats
    @property
    def avg_latency_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def max_latency_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def min_latency_ms(self) -> float:
        return min(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_l = sorted(self.latencies_ms)
        idx = max(0, int(len(sorted_l) * 0.95) - 1)
        return sorted_l[idx]

    @property
    def success_rate(self) -> float:
        return (self.success / self.total * 100) if self.total else 0.0

    @property
    def error_rate(self) -> float:
        return (self.error / self.total * 100) if self.total else 0.0


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_file: str | None) -> logging.Logger:
    logger = logging.getLogger("xclaw_stress")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, mode="w")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------

def build_execute_payload(cfg: TestConfig) -> dict[str, Any]:
    asset = random.choice(cfg.assets)
    amount = round(random.uniform(cfg.amount_min, cfg.amount_max), 8)
    side = random.choice(["buy", "sell"])
    return {
        "agent_id": cfg.agent_id,
        "wallet_id": cfg.wallet_id,
        "side": side,
        "asset": asset,
        "amount": amount,
        "quote": "USDT",
        "order_type": "market",
        "daily_volume_usd": round(random.uniform(0, 50000), 2),
    }


def build_approve_payload(request_id: str) -> dict[str, Any]:
    decision = random.choice(["approve", "reject"])
    return {
        "request_id": request_id,
        "decision": decision,
        "note": f"Stress-test auto-decision: {decision}",
        "execute_immediately": True,
    }


# ---------------------------------------------------------------------------
# Single request executor
# ---------------------------------------------------------------------------

async def run_execute(
    client: httpx.AsyncClient,
    cfg: TestConfig,
    logger: logging.Logger,
) -> RequestResult:
    payload = build_execute_payload(cfg)
    t0 = time.perf_counter()
    status_code = None
    response_body = None
    error = None
    response_status = None

    try:
        resp = await client.post("/execute", json=payload)
        status_code = resp.status_code
        latency_ms = (time.perf_counter() - t0) * 1000

        try:
            response_body = resp.json()
            response_status = response_body.get("status")
        except Exception:
            response_body = {"raw": resp.text}

        success = status_code in (200, 202, 403)  # 403 denied is a valid business outcome
        return RequestResult(
            endpoint="/execute",
            status_code=status_code,
            response_status=response_status,
            latency_ms=latency_ms,
            success=success,
            payload=payload,
            response_body=response_body,
            error=None,
        )

    except httpx.TimeoutException as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        error = f"Timeout: {exc}"
    except httpx.RequestError as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        error = f"Request error: {exc}"

    result = RequestResult(
        endpoint="/execute",
        status_code=status_code,
        response_status=None,
        latency_ms=latency_ms,
        success=False,
        payload=payload,
        response_body=response_body,
        error=error,
    )
    logger.error(
        "FAILURE /execute | error=%s | payload=%s | response=%s",
        error,
        json.dumps(payload),
        json.dumps(response_body),
    )
    return result


async def run_approve(
    client: httpx.AsyncClient,
    pending_ids: list[str],
    logger: logging.Logger,
) -> RequestResult:
    if not pending_ids:
        # Attempt with a fake ID to test 404 handling
        request_id = "apr_nonexistent000"
    else:
        request_id = pending_ids.pop(0)

    payload = build_approve_payload(request_id)
    t0 = time.perf_counter()
    status_code = None
    response_body = None
    error = None
    response_status = None

    try:
        resp = await client.post("/approve", json=payload)
        status_code = resp.status_code
        latency_ms = (time.perf_counter() - t0) * 1000

        try:
            response_body = resp.json()
            response_status = response_body.get("status")
        except Exception:
            response_body = {"raw": resp.text}

        success = status_code in (200, 404, 409)
        return RequestResult(
            endpoint="/approve",
            status_code=status_code,
            response_status=response_status,
            latency_ms=latency_ms,
            success=success,
            payload=payload,
            response_body=response_body,
            error=None,
        )

    except httpx.TimeoutException as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        error = f"Timeout: {exc}"
    except httpx.RequestError as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        error = f"Request error: {exc}"

    result = RequestResult(
        endpoint="/approve",
        status_code=status_code,
        response_status=None,
        latency_ms=latency_ms,
        success=False,
        payload=payload,
        response_body=response_body,
        error=error,
    )
    logger.error(
        "FAILURE /approve | error=%s | payload=%s | response=%s",
        error,
        json.dumps(payload),
        json.dumps(response_body),
    )
    return result


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

async def worker(
    worker_id: int,
    task_queue: asyncio.Queue[str],
    metrics: Metrics,
    metrics_lock: asyncio.Lock,
    client: httpx.AsyncClient,
    cfg: TestConfig,
    pending_ids: list[str],
    pending_ids_lock: asyncio.Lock,
    logger: logging.Logger,
) -> None:
    while True:
        try:
            endpoint = task_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        # Timing jitter
        jitter = random.randint(cfg.jitter_min_ms, cfg.jitter_max_ms) / 1000
        if jitter > 0:
            await asyncio.sleep(jitter)

        if endpoint == "/execute":
            result = await run_execute(client, cfg, logger)
            # Collect any pending approval IDs for /approve workers
            if result.response_status == "pending" and result.response_body:
                apr_id = result.response_body.get("approval_request_id")
                if apr_id:
                    async with pending_ids_lock:
                        pending_ids.append(apr_id)
        else:
            async with pending_ids_lock:
                ids_snapshot = pending_ids[:]
                pending_ids.clear()
            result = await run_approve(client, ids_snapshot, logger)
            # Put remaining unconsumed IDs back
            async with pending_ids_lock:
                pending_ids.extend(ids_snapshot)

        async with metrics_lock:
            metrics.record(result)

        logger.debug(
            "worker=%d endpoint=%s status=%s latency=%.1fms",
            worker_id,
            result.endpoint,
            result.status_code,
            result.latency_ms,
        )
        task_queue.task_done()


# ---------------------------------------------------------------------------
# Main harness
# ---------------------------------------------------------------------------

async def run_stress_test(cfg: TestConfig, logger: logging.Logger) -> Metrics:
    # Build task queue
    task_queue: asyncio.Queue[str] = asyncio.Queue()
    endpoints = cfg.endpoints or ["/execute"]
    for _ in range(cfg.total_requests):
        task_queue.put_nowait(random.choice(endpoints))

    metrics = Metrics()
    metrics_lock = asyncio.Lock()
    pending_ids: list[str] = []
    pending_ids_lock = asyncio.Lock()

    headers = {"X-API-Key": cfg.api_key}
    timeout = httpx.Timeout(cfg.timeout_seconds)

    logger.info(
        "Starting stress test | url=%s total=%d concurrency=%d endpoints=%s",
        cfg.api_url,
        cfg.total_requests,
        cfg.concurrency,
        endpoints,
    )

    wall_start = time.perf_counter()

    async with httpx.AsyncClient(
        base_url=cfg.api_url,
        headers=headers,
        timeout=timeout,
    ) as client:
        workers = [
            asyncio.create_task(
                worker(
                    i,
                    task_queue,
                    metrics,
                    metrics_lock,
                    client,
                    cfg,
                    pending_ids,
                    pending_ids_lock,
                    logger,
                )
            )
            for i in range(cfg.concurrency)
        ]
        await asyncio.gather(*workers)

    wall_elapsed = time.perf_counter() - wall_start
    logger.info("Stress test complete in %.2fs", wall_elapsed)
    return metrics


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(metrics: Metrics, logger: logging.Logger) -> None:
    bar = "=" * 60
    logger.info(bar)
    logger.info("STRESS TEST REPORT")
    logger.info(bar)
    logger.info("Total Requests      : %d", metrics.total)
    logger.info("Success             : %d  (%.1f%%)", metrics.success, metrics.success_rate)
    logger.info("Approval Required   : %d", metrics.approval_required)
    logger.info("Denied (policy)     : %d", metrics.denied)
    logger.info("Errors              : %d  (%.1f%%)", metrics.error, metrics.error_rate)
    logger.info(bar)
    logger.info("Latency (ms):")
    logger.info("  Min               : %.1f", metrics.min_latency_ms)
    logger.info("  Avg               : %.1f", metrics.avg_latency_ms)
    logger.info("  p95               : %.1f", metrics.p95_latency_ms)
    logger.info("  Max               : %.1f", metrics.max_latency_ms)
    logger.info(bar)

    if metrics.failures:
        logger.info("FAILURES (%d):", len(metrics.failures))
        for i, f in enumerate(metrics.failures, 1):
            logger.info(
                "  [%d] endpoint=%s http=%s api_status=%s error=%s",
                i,
                f.endpoint,
                f.status_code,
                f.response_status,
                f.error or "-",
            )
            logger.debug("       payload   : %s", json.dumps(f.payload))
            logger.debug("       response  : %s", json.dumps(f.response_body))
        logger.info(bar)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="X-Claw Stress Testing Harness")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to JSON config file (default: config.json)",
    )
    parser.add_argument("--url", help="Override API URL")
    parser.add_argument("--api-key", dest="api_key", help="Override API key")
    parser.add_argument("--agent-id", dest="agent_id", help="Override agent ID")
    parser.add_argument("--wallet-id", dest="wallet_id", help="Override wallet ID")
    parser.add_argument("--requests", type=int, help="Override total requests")
    parser.add_argument("--concurrency", type=int, help="Override concurrency level")
    parser.add_argument(
        "--endpoints",
        nargs="+",
        choices=["/execute", "/approve"],
        help="Endpoints to hit (space-separated)",
    )
    args = parser.parse_args()

    # Load config
    cfg_path = Path(args.config)
    if cfg_path.exists():
        cfg = TestConfig.from_file(cfg_path)
        print(f"Loaded config from {cfg_path}")
    else:
        cfg = TestConfig()
        print(f"Config file '{cfg_path}' not found — using defaults.")

    # CLI overrides
    if args.url:
        cfg.api_url = args.url
    if args.api_key:
        cfg.api_key = args.api_key
    if args.agent_id:
        cfg.agent_id = args.agent_id
    if args.wallet_id:
        cfg.wallet_id = args.wallet_id
    if args.requests:
        cfg.total_requests = args.requests
    if args.concurrency:
        cfg.concurrency = args.concurrency
    if args.endpoints:
        cfg.endpoints = args.endpoints

    logger = setup_logging(cfg.log_file)

    try:
        metrics = asyncio.run(run_stress_test(cfg, logger))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)

    print_report(metrics, logger)

    # Exit non-zero if error rate is above 10%
    if metrics.error_rate > 10.0:
        sys.exit(1)


if __name__ == "__main__":
    main()
