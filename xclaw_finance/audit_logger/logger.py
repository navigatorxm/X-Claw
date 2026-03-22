"""Audit logger — immutable SQLite audit trail."""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import AuditEntry


class AuditLogger:
    """
    Append-only audit log.

    Every financial action — regardless of outcome — is recorded here with:
    - timestamp
    - agent ID
    - action description
    - policy decision
    - approval chain reference
    - execution result
    - arbitrary metadata

    Rows are never updated or deleted (append-only by convention).
    """

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    entry_id            TEXT PRIMARY KEY,
                    timestamp           TEXT NOT NULL,
                    agent_id            TEXT NOT NULL,
                    action              TEXT NOT NULL,
                    policy_decision     TEXT NOT NULL,
                    approval_chain      TEXT,
                    execution_result    TEXT,
                    metadata            TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------- write
    def log(
        self,
        agent_id: str,
        action: str,
        policy_decision: str,
        approval_chain: Optional[str],
        execution_result: Optional[dict],
        metadata: Optional[dict] = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            entry_id=f"aud_{uuid.uuid4().hex[:14]}",
            timestamp=datetime.utcnow(),
            agent_id=agent_id,
            action=action,
            policy_decision=policy_decision,
            approval_chain=approval_chain,
            execution_result=execution_result,
            metadata=metadata or {},
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (entry_id, timestamp, agent_id, action, policy_decision,
                    approval_chain, execution_result, metadata)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    entry.entry_id,
                    entry.timestamp.isoformat(),
                    agent_id,
                    action,
                    policy_decision,
                    approval_chain,
                    json.dumps(execution_result) if execution_result else None,
                    json.dumps(entry.metadata),
                ),
            )
        return entry

    # ------------------------------------------------------------------- read
    def get_history(
        self,
        agent_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEntry]:
        with self._connect() as conn:
            if agent_id:
                rows = conn.execute(
                    """SELECT * FROM audit_log WHERE agent_id = ?
                       ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
                    (agent_id, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_entry(self, entry_id: str) -> Optional[AuditEntry]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM audit_log WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def count(self, agent_id: Optional[str] = None) -> int:
        with self._connect() as conn:
            if agent_id:
                return conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE agent_id = ?", (agent_id,)
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    # -------------------------------------------------------------- serialise
    def _row_to_entry(self, row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            entry_id=row["entry_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            agent_id=row["agent_id"],
            action=row["action"],
            policy_decision=row["policy_decision"],
            approval_chain=row["approval_chain"],
            execution_result=json.loads(row["execution_result"]) if row["execution_result"] else None,
            metadata=json.loads(row["metadata"] or "{}"),
        )
