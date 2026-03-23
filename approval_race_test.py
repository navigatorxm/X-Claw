"""
X-Claw Approval Race Condition Detector
========================================
Fires N parallel POST /approve requests for the same request_id and
verifies that at most ONE execution occurs — regardless of how many
concurrent approvers "win" the HTTP race.

Exit codes
----------
0  PASS  – exactly one execution observed
1  FAIL  – zero or multiple executions observed
2  ERROR – could not complete the test (network, bad config, etc.)

Usage
-----
  python approval_race_test.py --request-id apr_abc123 --api-key xclaw_...
  python approval_race_test.py --config config.json --request-id apr_abc123
  python approval_race_test.py --request-id apr_abc123 --api-key xclaw_... --workers 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# ANSI colour helpers (degrade gracefully on non-TTY)
# ---------------------------------------------------------------------------

_USE_COLOUR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text

RED    = lambda t: _c("31;1", t)
GREEN  = lambda t: _c("32;1", t)
YELLOW = lambda t: _c("33;1", t)
CYAN   = lambda t: _c("36;1", t)
BOLD   = lambda t: _c("1", t)
DIM    = lambda t: _c("2", t)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class RaceTestConfig:
    api_url: str          = "http://localhost:8000"
    api_key: str          = ""
    request_id: str       = ""
    workers: int          = 20          # parallel approval senders
    decision: str         = "approve"   # "approve" | "reject"
    execute_immediately: bool = True
    note: str             = "race-condition-test"
    timeout_seconds: float = 30.0
    stagger_ms: int       = 0           # optional stagger between worker launches

    @classmethod
    def from_file(cls, path: str | Path) -> "RaceTestConfig":
        raw = json.loads(Path(path).read_text())
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Per-response record
# ---------------------------------------------------------------------------

@dataclass
class ApprovalResponse:
    worker_id: int
    status_code: int | None
    body: dict[str, Any] | None
    latency_ms: float
    error: str | None

    # Derived helpers
    @property
    def api_status(self) -> str | None:
        return self.body.get("status") if self.body else None

    @property
    def has_execution(self) -> bool:
        """True when the server actually ran the trade."""
        return bool(self.body and "execution" in self.body)

    @property
    def has_execution_error(self) -> bool:
        return bool(self.body and "execution_error" in self.body)

    @property
    def already_decided(self) -> bool:
        """HTTP 409 — request was no longer pending when this worker arrived."""
        return self.status_code == 409

    @property
    def not_found(self) -> bool:
        return self.status_code == 404

    @property
    def is_approved(self) -> bool:
        return self.status_code == 200 and self.api_status in ("approved", "auto_approved")

    @property
    def is_rejected(self) -> bool:
        return self.status_code == 200 and self.api_status == "rejected"

    @property
    def is_unexpected(self) -> bool:
        """Any response that is not one of the expected race-test outcomes."""
        return (
            self.error is not None
            or self.status_code not in (200, 404, 409, None)
            or (self.status_code == 200 and self.api_status not in
                ("approved", "auto_approved", "rejected"))
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _make_logger() -> logging.Logger:
    log = logging.getLogger("race_test")
    log.setLevel(logging.DEBUG)
    h = logging.StreamHandler(sys.stdout)
    h.setLevel(logging.DEBUG)
    h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(h)
    return log


LOG = _make_logger()


def _log(msg: str) -> None:
    LOG.info(msg)


def _banner(text: str) -> None:
    width = 64
    _log(BOLD("=" * width))
    _log(BOLD(f"  {text}"))
    _log(BOLD("=" * width))


# ---------------------------------------------------------------------------
# Single worker
# ---------------------------------------------------------------------------

async def _send_approval(
    worker_id: int,
    client: httpx.AsyncClient,
    cfg: RaceTestConfig,
    barrier: asyncio.Barrier,
    stagger_lock: asyncio.Lock,
    stagger_slot: list[int],
) -> ApprovalResponse:
    """
    Wait at the barrier so every worker fires as simultaneously as possible,
    then POST /approve.
    """
    payload: dict[str, Any] = {
        "request_id": cfg.request_id,
        "decision": cfg.decision,
        "note": f"{cfg.note} [worker-{worker_id:02d}]",
        "execute_immediately": cfg.execute_immediately,
    }

    # Optional stagger — spreads workers slightly to stress the "check then act" gap
    if cfg.stagger_ms > 0:
        async with stagger_lock:
            slot = stagger_slot[0]
            stagger_slot[0] += 1
        await asyncio.sleep(slot * cfg.stagger_ms / 1000)

    # All workers reach the barrier, then GO simultaneously
    await barrier.wait()

    t0 = time.perf_counter()
    status_code: int | None = None
    body: dict[str, Any] | None = None
    error: str | None = None

    try:
        resp = await client.post("/approve", json=payload)
        status_code = resp.status_code
        latency_ms = (time.perf_counter() - t0) * 1000
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
    except httpx.TimeoutException as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        error = f"Timeout: {exc}"
    except httpx.RequestError as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        error = f"Connection error: {exc}"

    return ApprovalResponse(
        worker_id=worker_id,
        status_code=status_code,
        body=body,
        latency_ms=latency_ms,
        error=error,
    )


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

async def run_race_test(cfg: RaceTestConfig) -> list[ApprovalResponse]:
    _banner("X-CLAW APPROVAL RACE CONDITION TEST")
    _log(f"  request_id : {CYAN(cfg.request_id)}")
    _log(f"  api_url    : {cfg.api_url}")
    _log(f"  workers    : {cfg.workers}")
    _log(f"  decision   : {cfg.decision}")
    _log(f"  stagger_ms : {cfg.stagger_ms}")
    _log(BOLD("=" * 64))
    _log("")

    barrier = asyncio.Barrier(cfg.workers)
    stagger_lock = asyncio.Lock()
    stagger_slot: list[int] = [0]

    headers = {"X-API-Key": cfg.api_key}
    timeout = httpx.Timeout(cfg.timeout_seconds)

    _log(f"  Launching {cfg.workers} concurrent workers …")
    wall_start = time.perf_counter()

    async with httpx.AsyncClient(
        base_url=cfg.api_url,
        headers=headers,
        timeout=timeout,
    ) as client:
        tasks = [
            asyncio.create_task(
                _send_approval(i, client, cfg, barrier, stagger_lock, stagger_slot)
            )
            for i in range(cfg.workers)
        ]
        results: list[ApprovalResponse] = await asyncio.gather(*tasks)

    wall_elapsed = (time.perf_counter() - wall_start) * 1000
    _log(f"  All workers finished in {wall_elapsed:.1f} ms")
    _log("")
    return results


# ---------------------------------------------------------------------------
# Response printer
# ---------------------------------------------------------------------------

def _status_label(r: ApprovalResponse) -> str:
    if r.error:
        return RED(f"ERROR  ({r.error})")
    if r.not_found:
        return YELLOW("404 NOT-FOUND")
    if r.already_decided:
        return DIM("409 ALREADY-DECIDED")
    if r.is_approved:
        tag = "✓ EXECUTION" if r.has_execution else "✓ APPROVED (no exec body)"
        if r.has_execution_error:
            tag = YELLOW("✓ APPROVED (exec failed)")
        return GREEN(tag)
    if r.is_rejected:
        return DIM("✓ REJECTED")
    return YELLOW(f"??? {r.status_code} {r.api_status}")


def print_all_responses(results: list[ApprovalResponse]) -> None:
    _log(BOLD("─── All Responses ") + BOLD("─" * 46))
    _log(f"  {'#':>3}  {'HTTP':>4}  {'Latency':>8}  Status")
    _log(f"  {'─'*3}  {'─'*4}  {'─'*8}  {'─'*35}")
    for r in sorted(results, key=lambda x: x.worker_id):
        http_str = str(r.status_code) if r.status_code else "---"
        lat_str  = f"{r.latency_ms:>6.1f}ms"
        _log(f"  {r.worker_id:>3}  {http_str:>4}  {lat_str}  {_status_label(r)}")
    _log("")


def print_anomalies(anomalies: list[ApprovalResponse]) -> None:
    if not anomalies:
        return
    _log(RED(BOLD("─── ANOMALIES DETECTED ") + "─" * 41))
    for r in anomalies:
        _log(RED(f"  [worker-{r.worker_id:02d}] http={r.status_code} status={r.api_status}"))
        _log(f"    payload_request_id : {r.body.get('request', {}).get('request_id') if r.body else 'N/A'}")
        if r.error:
            _log(f"    error              : {r.error}")
        if r.body:
            _log(f"    response           : {json.dumps(r.body, indent=6)}")
        _log("")


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    executions: list[ApprovalResponse]    # workers that saw an actual execution
    approved_no_exec: list[ApprovalResponse]  # approved but no execution body
    exec_errors: list[ApprovalResponse]   # approved but execution failed
    already_decided: list[ApprovalResponse]
    errors: list[ApprovalResponse]
    unexpected: list[ApprovalResponse]

    @property
    def execution_count(self) -> int:
        return len(self.executions)

    @property
    def passed(self) -> bool:
        return self.execution_count == 1

    @property
    def anomalies(self) -> list[ApprovalResponse]:
        return self.unexpected + (self.executions[1:] if self.execution_count > 1 else [])


def analyse(results: list[ApprovalResponse]) -> Verdict:
    executions        = [r for r in results if r.has_execution]
    approved_no_exec  = [r for r in results if r.is_approved and not r.has_execution and not r.has_execution_error]
    exec_errors       = [r for r in results if r.has_execution_error]
    already_decided   = [r for r in results if r.already_decided]
    errors            = [r for r in results if r.error is not None]
    unexpected        = [r for r in results if r.is_unexpected and not r.already_decided]
    return Verdict(executions, approved_no_exec, exec_errors, already_decided, errors, unexpected)


def print_verdict(v: Verdict, results: list[ApprovalResponse]) -> None:
    _log(BOLD("─── Analysis ") + BOLD("─" * 51))
    _log(f"  Total workers            : {len(results)}")
    _log(f"  Executions (trade ran)   : {v.execution_count}")
    _log(f"  Approved (no exec body)  : {len(v.approved_no_exec)}")
    _log(f"  Approved (exec error)    : {len(v.exec_errors)}")
    _log(f"  Already decided (409)    : {len(v.already_decided)}")
    _log(f"  Transport/network errors : {len(v.errors)}")
    _log(f"  Unexpected responses     : {len(v.unexpected)}")
    _log("")

    if v.executions:
        _log(BOLD("  Workers that triggered an execution:"))
        for r in v.executions:
            exec_body = r.body.get("execution", {}) if r.body else {}
            order_id  = exec_body.get("order", {}).get("order_id", "?") if isinstance(exec_body, dict) else "?"
            exch_id   = exec_body.get("exchange_order_id", "?") if isinstance(exec_body, dict) else "?"
            _log(f"    worker-{r.worker_id:02d} | latency={r.latency_ms:.1f}ms | "
                 f"order_id={order_id} | exchange_order_id={exch_id}")
        _log("")

    # ── Final verdict ──────────────────────────────────────────────────────
    _log(BOLD("─── VERDICT ") + BOLD("─" * 52))

    if v.execution_count == 0:
        # Could be: all rejected, execution_error, or no execute_immediately
        if v.approved_no_exec:
            _log(YELLOW(BOLD("  INCONCLUSIVE — approved but no execution body present.")))
            _log(YELLOW("  Possibly execute_immediately=false or server returned partial data."))
            _log(YELLOW(f"  Workers with approved+no-exec: {[r.worker_id for r in v.approved_no_exec]}"))
        elif v.exec_errors:
            _log(YELLOW(BOLD("  INCONCLUSIVE — approved but execution engine raised an error.")))
            _log(YELLOW("  Idempotency cannot be confirmed. Check server logs."))
            for r in v.exec_errors:
                err = r.body.get("execution_error", "?") if r.body else "?"
                _log(YELLOW(f"    worker-{r.worker_id:02d}: {err}"))
        else:
            _log(YELLOW(BOLD("  INCONCLUSIVE — no executions observed.")))
            _log(YELLOW("  All approvals may have been rejected or network errors prevented results."))

    elif v.execution_count == 1:
        _log(GREEN(BOLD("  PASS ✓")))
        _log(GREEN("  Exactly ONE execution occurred despite concurrent approval storm."))
        _log(GREEN(f"  The idempotency guard held. Winner: worker-{v.executions[0].worker_id:02d}"))
        _log(GREEN(f"  All other {len(v.already_decided)} workers received 409 Already Decided."))

    else:
        _log(RED(BOLD("  FAIL ✗  — RACE CONDITION DETECTED")))
        _log(RED(f"  {v.execution_count} executions were triggered for the SAME approval request."))
        _log(RED("  Multiple trades may have been placed. Investigate immediately."))
        _log("")
        _log(RED("  Duplicate execution details:"))
        for i, r in enumerate(v.executions, 1):
            _log(RED(f"    [{i}] worker-{r.worker_id:02d} latency={r.latency_ms:.1f}ms"))
            if r.body and "execution" in r.body:
                exec_body = r.body["execution"]
                _log(RED(f"        order_id         : {exec_body.get('order', {}).get('order_id', '?')}"))
                _log(RED(f"        exchange_order_id: {exec_body.get('exchange_order_id', '?')}"))
                _log(RED(f"        filled_amount    : {exec_body.get('filled_amount', '?')}"))
                _log(RED(f"        avg_price        : {exec_body.get('avg_price', '?')}"))

    _log(BOLD("=" * 64))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Detect race conditions in X-Claw concurrent approval handling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--request-id",  dest="request_id",  required=True,
                   help="Approval request ID to target (e.g. apr_abc123)")
    p.add_argument("--api-key",     dest="api_key",      default="",
                   help="X-API-Key value for authentication")
    p.add_argument("--config",      default=None,
                   help="Optional JSON config file (values are overridden by CLI flags)")
    p.add_argument("--url",         default=None,
                   help="API base URL (default: http://localhost:8000)")
    p.add_argument("--workers",     type=int,  default=None,
                   help="Number of concurrent approval senders (default: 20)")
    p.add_argument("--decision",    choices=["approve", "reject"], default=None,
                   help="Decision to send (default: approve)")
    p.add_argument("--no-execute",  dest="no_execute", action="store_true",
                   help="Set execute_immediately=false (tests approve-only idempotency)")
    p.add_argument("--stagger-ms",  dest="stagger_ms", type=int, default=None,
                   help="Stagger workers N ms apart to widen the race window (default: 0)")
    return p


def main() -> None:
    args = build_parser().parse_args()

    # Load base config
    if args.config and Path(args.config).exists():
        cfg = RaceTestConfig.from_file(args.config)
    else:
        cfg = RaceTestConfig()

    # CLI overrides (always win)
    cfg.request_id = args.request_id
    if args.api_key:
        cfg.api_key = args.api_key
    if args.url:
        cfg.api_url = args.url
    if args.workers is not None:
        cfg.workers = args.workers
    if args.decision is not None:
        cfg.decision = args.decision
    if args.no_execute:
        cfg.execute_immediately = False
    if args.stagger_ms is not None:
        cfg.stagger_ms = args.stagger_ms

    # Validation
    if not cfg.request_id:
        _log(RED("ERROR: --request-id is required."))
        sys.exit(2)
    if not cfg.api_key:
        _log(YELLOW("WARNING: --api-key is empty. Requests will likely return 401."))
    if cfg.workers < 2:
        _log(YELLOW("WARNING: --workers < 2 is not a concurrency test. Setting to 20."))
        cfg.workers = 20

    try:
        results = asyncio.run(run_race_test(cfg))
    except KeyboardInterrupt:
        _log("\nAborted.")
        sys.exit(2)
    except Exception as exc:
        _log(RED(f"Fatal error: {exc}"))
        sys.exit(2)

    print_all_responses(results)

    verdict = analyse(results)
    print_anomalies(verdict.anomalies)
    print_verdict(verdict, results)

    if verdict.passed:
        sys.exit(0)
    elif verdict.execution_count > 1:
        sys.exit(1)
    else:
        sys.exit(2)  # Inconclusive


if __name__ == "__main__":
    main()
