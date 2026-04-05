"""Policy store — SQLite-backed CRUD for policies."""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Policy, Rule, RuleType


class PolicyStore:
    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS policies (
                    policy_id   TEXT PRIMARY KEY,
                    agent_id    TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    rules       TEXT NOT NULL DEFAULT '[]',
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_policy_agent ON policies(agent_id)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, agent_id: str, name: str, rules: list[Rule]) -> Policy:
        policy = Policy(
            policy_id=f"p_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            name=name,
            rules=rules,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO policies (policy_id, agent_id, name, rules, enabled, created_at) VALUES (?,?,?,?,?,?)",
                (
                    policy.policy_id,
                    agent_id,
                    name,
                    json.dumps([r.to_dict() for r in rules]),
                    1,
                    policy.created_at.isoformat(),
                ),
            )
        return policy

    def get(self, policy_id: str) -> Optional[Policy]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM policies WHERE policy_id = ?", (policy_id,)).fetchone()
        return self._row_to_policy(row) if row else None

    def list_for_agent(self, agent_id: str) -> list[Policy]:
        """Returns agent-specific policies + the global default (agent_id='*')."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM policies WHERE agent_id IN (?, '*') AND enabled = 1",
                (agent_id,),
            ).fetchall()
        return [self._row_to_policy(r) for r in rows]

    def list_all(self) -> list[Policy]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM policies ORDER BY created_at").fetchall()
        return [self._row_to_policy(r) for r in rows]

    def update_rules(self, policy_id: str, rules: list[Rule]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE policies SET rules = ? WHERE policy_id = ?",
                (json.dumps([r.to_dict() for r in rules]), policy_id),
            )

    def disable(self, policy_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE policies SET enabled = 0 WHERE policy_id = ?", (policy_id,))

    def _row_to_policy(self, row: sqlite3.Row) -> Policy:
        rules = [Rule.from_dict(d) for d in json.loads(row["rules"])]
        return Policy(
            policy_id=row["policy_id"],
            agent_id=row["agent_id"],
            name=row["name"],
            rules=rules,
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
