"""
XClaw Memory — persistent context, conversation history, tasks, and executions.

v2 additions:
  • messages table    — full conversation history per session
  • cache table       — short-lived result cache (default TTL 5 min)
  • get_recent_messages() — injects conversation context into LLM prompts
  • cache_get/set()   — prevent redundant LLM calls for identical queries
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
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
    results     TEXT    NOT NULL,
    executed_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,   -- 'user' | 'xclaw'
    content     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
    key         TEXT    PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_executions_session ON executions(session_id, executed_at);
"""


class Memory:
    """
    SQLite-backed memory store.

    All methods are synchronous (SQLite is fast enough; async wrappers
    would add complexity without benefit for single-process use).
    """

    DEFAULT_CACHE_TTL = 300  # seconds

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
    # DB init
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # better concurrency
        conn.execute("PRAGMA foreign_keys=ON")
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
        query += " ORDER BY created_at DESC"
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
    # Conversation history
    # ------------------------------------------------------------------

    def add_message(self, session_id: str, role: str, content: str) -> None:
        """Append a message to the session conversation history."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                (session_id, role, content, self._now()),
            )

    def get_recent_messages(self, session_id: str, limit: int = 10) -> list[dict]:
        """Return the last `limit` messages in chronological order."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT role, content, created_at FROM messages
                   WHERE session_id=?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        # Reverse so oldest is first
        return [dict(r) for r in reversed(rows)]

    def format_history_for_prompt(self, session_id: str, limit: int = 6) -> str:
        """
        Return recent conversation as a compact string for injection into prompts.
        e.g.:
            [user]: Research Harver Space competitors
            [xclaw]: Found 8 competitors including...
        """
        messages = self.get_recent_messages(session_id, limit)
        if not messages:
            return "(no prior conversation)"
        lines = [f"[{m['role']}]: {m['content'][:300]}" for m in messages]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Result cache
    # ------------------------------------------------------------------

    def cache_get(self, key: str) -> str | None:
        """Return cached value if it exists and hasn't expired."""
        now = self._now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM cache WHERE key=? AND expires_at > ?",
                (key, now),
            ).fetchone()
        return row["value"] if row else None

    def cache_set(self, key: str, value: str, session_id: str = "", ttl: int = DEFAULT_CACHE_TTL) -> None:
        """Store a value in the cache with a TTL in seconds."""
        from datetime import timedelta
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, session_id, value, expires_at) VALUES (?,?,?,?)",
                (key, session_id, value, expires),
            )

    def cache_key(self, *parts: str) -> str:
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def cache_evict_expired(self) -> int:
        """Delete all expired cache rows. Returns count deleted."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM cache WHERE expires_at <= ?", (self._now(),))
            return cur.rowcount

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
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
