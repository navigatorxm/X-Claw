"""
Metrics Aggregator — behavioral analytics from the audit log.

Computes:
  total_actions          — all audit entries in the window
  trades_executed        — policy=allow + adapter success
  trades_failed          — policy=allow + adapter failure
  trades_denied          — policy:deny or risk:deny
  trades_pending         — require_approval escalations
  denial_rate            — denied / total_actions
  approval_required_rate — pending / total_actions
  avg_execution_time_ms  — mean of (filled_at − created_at) for filled orders
  total_volume_usd       — from risk_fills (accurate; 0 if risk engine not wired)
  simulation_trades      — entries with metadata.simulation=True
  real_trades            — entries with metadata.simulation=False

Data sources:
  audit_log   — all decisions and execution results (always written)
  risk_fills  — trade volume (only when risk engine is wired)
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .models import AgentMetrics


class MetricsAggregator:
    """
    Read-only projection over audit_log and risk_fills.
    One instance per DB path; safe to share across requests (no state).
    """

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    # ─────────────────────────────────────────────────── public API

    def get_metrics(
        self,
        agent_id: str,
        start: Optional[str] = None,    # ISO datetime string (inclusive)
        end: Optional[str] = None,
    ) -> AgentMetrics:
        """
        Compute execution and behavioral metrics for agent_id.

        Parameters
        ----------
        agent_id : str
        start    : ISO datetime string — lower bound (inclusive, optional)
        end      : ISO datetime string — upper bound (inclusive, optional)
        """
        audit_rows = self._fetch_audit(agent_id, start, end)
        volume_usd = self._fetch_volume(agent_id, start, end)

        total      = len(audit_rows)
        executed   = 0
        failed     = 0
        denied     = 0
        pending    = 0
        sim_count  = 0
        real_count = 0
        exec_times: list[Decimal] = []

        for row in audit_rows:
            decision = row["policy_decision"]
            meta     = json.loads(row["metadata"] or "{}")
            exec_res = json.loads(row["execution_result"]) if row["execution_result"] else None

            # ── decision bucket ───────────────────────────────────────
            if "deny" in decision:
                denied += 1
            elif "require_approval" in decision:
                pending += 1
            elif decision == "allow":
                if exec_res and exec_res.get("success") is True:
                    executed += 1
                    # Execution latency: order.created_at → order.filled_at
                    order   = exec_res.get("order", {})
                    created = order.get("created_at")
                    filled  = order.get("filled_at")
                    if created and filled:
                        try:
                            dt_ms = Decimal(str(
                                (datetime.fromisoformat(filled) -
                                 datetime.fromisoformat(created)).total_seconds() * 1000
                            ))
                            exec_times.append(dt_ms)
                        except (ValueError, TypeError):
                            pass
                elif exec_res and exec_res.get("success") is False:
                    failed += 1

            # ── simulation split ──────────────────────────────────────
            sim_flag = meta.get("simulation")
            if sim_flag is True:
                sim_count += 1
            elif sim_flag is False:
                real_count += 1

        avg_exec = (
            (sum(exec_times) / Decimal(len(exec_times))).quantize(Decimal("0.01"))
            if exec_times
            else Decimal("0")
        )
        denial_rate = (
            (Decimal(denied) / Decimal(total)).quantize(Decimal("0.0001"))
            if total else Decimal("0")
        )
        approval_rate = (
            (Decimal(pending) / Decimal(total)).quantize(Decimal("0.0001"))
            if total else Decimal("0")
        )

        return AgentMetrics(
            agent_id=agent_id,
            period_start=start,
            period_end=end,
            total_actions=total,
            trades_executed=executed,
            trades_failed=failed,
            trades_denied=denied,
            trades_pending=pending,
            denial_rate=denial_rate,
            approval_required_rate=approval_rate,
            avg_execution_time_ms=avg_exec,
            total_volume_usd=volume_usd,
            simulation_trades=sim_count,
            real_trades=real_count,
        )

    # ─────────────────────────────────────────────────── helpers

    def _fetch_audit(
        self,
        agent_id: str,
        start: Optional[str],
        end: Optional[str],
    ) -> list[sqlite3.Row]:
        params: list = [agent_id]
        clauses = ["agent_id = ?"]
        if start:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end:
            clauses.append("timestamp <= ?")
            params.append(end)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            return conn.execute(
                f"SELECT policy_decision, execution_result, metadata "
                f"FROM audit_log WHERE {where} ORDER BY timestamp",
                params,
            ).fetchall()

    def _fetch_volume(
        self,
        agent_id: str,
        start: Optional[str],
        end: Optional[str],
    ) -> Decimal:
        """
        Total USD notional from risk_fills in the window.
        Returns 0 gracefully when the risk_fills table has not been created
        (i.e. when the risk engine was never wired into the execution engine).
        """
        params: list = [agent_id]
        clauses = ["agent_id = ?"]
        if start:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end:
            clauses.append("timestamp <= ?")
            params.append(end)
        where = " AND ".join(clauses)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT COALESCE(SUM(CAST(amount_usd AS REAL)), 0) "
                    f"FROM risk_fills WHERE {where}",
                    params,
                ).fetchone()
            return Decimal(str(row[0])).quantize(Decimal("0.01"))
        except sqlite3.OperationalError:
            # risk_fills table not present — risk engine not wired
            return Decimal("0")
