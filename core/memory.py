"""
XClaw Memory — persistent context, task store, and execution history.

Uses SQLite for structured data (tasks, executions) and a plain Markdown
file for Navigator's rolling context notes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    results     TEXT    NOT NULL,   -- JSON array of strings
    executed_at TEXT    NOT NULL
);
"""


class Memory:
    """
    Thread-safe (single-process) SQLite-backed memory store.

    Args:
        db_path: Path to the SQLite file.  Created automatically if absent.
        context_path: Path to the Markdown context file.
    """

    def __init__(
        self,
        db_path: str | Path = "memory/tasks.db",
        context_path: str | Path = "memory/context.md",
    ) -> None:
        self._db_path = Path(db_path)
        self._context_path = Path(context_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._context_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def add_task(self, session_id: str, title: str) -> int:
        now = self._now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (session_id, title, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (session_id, title, "pending", now, now),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def update_task_status(self, task_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (status, self._now(), task_id),
            )

    def get_tasks(self, session_id: str, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM tasks WHERE session_id=?"
        args: tuple = (session_id,)
        if status:
            query += " AND status=?"
            args = (session_id, status)
        with self._conn() as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Executions
    # ------------------------------------------------------------------

    def save_execution(self, session_id: str, summary: str, results: list[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO executions (session_id, summary, results, executed_at) VALUES (?,?,?,?)",
                (session_id, summary, json.dumps(results), self._now()),
            )

    def get_executions(self, session_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM executions WHERE session_id=? ORDER BY executed_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        records = []
        for r in rows:
            d = dict(r)
            d["results"] = json.loads(d["results"])
            records.append(d)
        return records

    # ------------------------------------------------------------------
    # Navigator context (Markdown)
    # ------------------------------------------------------------------

    def read_context(self) -> str:
        if self._context_path.exists():
            return self._context_path.read_text(encoding="utf-8")
        return ""

    def append_context(self, note: str) -> None:
        with self._context_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n## {self._now()}\n{note}\n")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat(timespec="seconds")
