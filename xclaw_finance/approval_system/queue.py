"""Approval queue — persistent store for approval requests."""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .models import ApprovalRequest, ApprovalStatus


class ApprovalQueue:
    """
    Stores and manages approval requests.

    Supports:
    - Creating pending requests
    - Manual approve / reject by an operator
    - Automatic expiry (configurable TTL)
    - Querying pending, by agent, by status
    """

    DEFAULT_TTL_SECONDS = 3600  # 1 hour

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    request_id      TEXT PRIMARY KEY,
                    agent_id        TEXT NOT NULL,
                    wallet_id       TEXT NOT NULL,
                    action          TEXT NOT NULL,
                    asset           TEXT NOT NULL,
                    amount_usd      TEXT NOT NULL,
                    exchange        TEXT NOT NULL,
                    policy_id       TEXT,
                    policy_reason   TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    created_at      TEXT NOT NULL,
                    decided_at      TEXT,
                    decided_by      TEXT,
                    decision_note   TEXT NOT NULL DEFAULT '',
                    metadata        TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approval_agent ON approvals(agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approval_status ON approvals(status)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------------- create
    def enqueue(
        self,
        agent_id: str,
        wallet_id: str,
        action: str,
        asset: str,
        amount_usd: Decimal,
        exchange: str,
        policy_id: Optional[str],
        policy_reason: str,
        metadata: Optional[dict] = None,
    ) -> ApprovalRequest:
        req = ApprovalRequest(
            request_id=f"apr_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            wallet_id=wallet_id,
            action=action,
            asset=asset,
            amount_usd=amount_usd,
            exchange=exchange,
            policy_id=policy_id,
            policy_reason=policy_reason,
            metadata=metadata or {},
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO approvals
                   (request_id, agent_id, wallet_id, action, asset, amount_usd,
                    exchange, policy_id, policy_reason, status, created_at,
                    decided_at, decided_by, decision_note, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    req.request_id, agent_id, wallet_id, action, asset,
                    str(amount_usd), exchange, policy_id, policy_reason,
                    ApprovalStatus.PENDING.value,
                    req.created_at.isoformat(),
                    None, None, "", json.dumps(req.metadata),
                ),
            )
        return req

    # ----------------------------------------------------------------- decide
    def approve(
        self, request_id: str, decided_by: str = "operator", note: str = ""
    ) -> Optional[ApprovalRequest]:
        return self._decide(request_id, ApprovalStatus.APPROVED, decided_by, note)

    def reject(
        self, request_id: str, decided_by: str = "operator", note: str = ""
    ) -> Optional[ApprovalRequest]:
        return self._decide(request_id, ApprovalStatus.REJECTED, decided_by, note)

    def auto_approve(self, request_id: str, note: str = "") -> Optional[ApprovalRequest]:
        return self._decide(request_id, ApprovalStatus.AUTO_APPROVED, "auto", note)

    def _decide(
        self, request_id: str, status: ApprovalStatus, decided_by: str, note: str
    ) -> Optional[ApprovalRequest]:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE approvals
                   SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?
                   WHERE request_id = ? AND status = 'pending'""",
                (status.value, now, decided_by, note, request_id),
            )
        return self.get(request_id)

    # ------------------------------------------------------------------ query
    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self._row_to_req(row) if row else None

    def list_pending(self) -> list[ApprovalRequest]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        return [self._row_to_req(r) for r in rows]

    def list_for_agent(self, agent_id: str, limit: int = 50) -> list[ApprovalRequest]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        return [self._row_to_req(r) for r in rows]

    def expire_old(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
        """Mark pending requests older than ttl_seconds as EXPIRED. Returns count."""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE approvals
                   SET status = 'expired', decided_at = ?, decided_by = 'system'
                   WHERE status = 'pending' AND created_at < ?""",
                (datetime.utcnow().isoformat(), cutoff),
            )
            return cur.rowcount

    # --------------------------------------------------------------- serialise
    def _row_to_req(self, row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest(
            request_id=row["request_id"],
            agent_id=row["agent_id"],
            wallet_id=row["wallet_id"],
            action=row["action"],
            asset=row["asset"],
            amount_usd=Decimal(row["amount_usd"]),
            exchange=row["exchange"],
            policy_id=row["policy_id"],
            policy_reason=row["policy_reason"],
            status=ApprovalStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
            decided_by=row["decided_by"],
            decision_note=row["decision_note"] or "",
            metadata=json.loads(row["metadata"] or "{}"),
        )
