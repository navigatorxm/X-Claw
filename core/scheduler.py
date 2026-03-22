"""
XClaw Scheduler — background recurring tasks.

Navigator can say "monitor BTC price every hour" or
"check my competitor news every morning at 09:00 and send to Telegram".

Tasks are stored in SQLite, survive restarts, and run in a background
asyncio loop that wakes every 60 seconds.

Supported interval formats:
  "30m"       → every 30 minutes
  "2h"        → every 2 hours
  "daily"     → every 24 hours
  "daily@HH:MM" → once per day at a specific UTC time (e.g. "daily@09:00")
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from core.memory import Memory

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    interval_str TEXT NOT NULL,
    notify_to    TEXT NOT NULL DEFAULT '',
    last_run     TEXT,
    next_run     TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);
"""

# Callback type: given (session_id, prompt) returns result text
RunFn = Callable[[str, str], Awaitable[str]]
NotifyFn = Callable[[str, str], Awaitable[None]]


@dataclass
class ScheduledTask:
    id: str
    session_id: str
    prompt: str
    interval_str: str
    notify_to: str
    last_run: datetime | None
    next_run: datetime
    enabled: bool


def _parse_interval(s: str) -> timedelta:
    """Parse '30m', '2h', 'daily', 'daily@HH:MM' into a timedelta."""
    s = s.strip().lower()
    if s.startswith("daily@"):
        # Will handle time-of-day in _calc_next_run
        return timedelta(hours=24)
    if s == "daily":
        return timedelta(hours=24)
    if s.endswith("m"):
        return timedelta(minutes=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    raise ValueError(f"Unrecognised interval: {s!r}. Use '30m', '2h', 'daily', 'daily@HH:MM'")


def _calc_next_run(interval_str: str, from_dt: datetime) -> datetime:
    """Calculate the next run time after from_dt."""
    s = interval_str.strip().lower()
    if s.startswith("daily@"):
        hh, mm = (int(x) for x in s[len("daily@"):].split(":"))
        candidate = from_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= from_dt:
            candidate += timedelta(days=1)
        return candidate
    delta = _parse_interval(s)
    return from_dt + delta


class Scheduler:
    """
    Async background task scheduler.

    Usage in main.py:
        scheduler = Scheduler(memory, run_fn=agent_loop.run, notify_fn=notifier.send)
        asyncio.create_task(scheduler.run_forever())
    """

    CHECK_INTERVAL = 60   # seconds between scheduler ticks

    def __init__(
        self,
        memory: "Memory",
        run_fn: RunFn,
        notify_fn: NotifyFn | None = None,
    ) -> None:
        self._memory = memory
        self._run_fn = run_fn
        self._notify_fn = notify_fn
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Task management
    # ------------------------------------------------------------------

    def add_task(
        self,
        session_id: str,
        prompt: str,
        interval_str: str,
        notify_to: str = "",
    ) -> str:
        """Register a recurring task. Returns the task ID."""
        _parse_interval(interval_str)  # validate
        task_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc)
        next_run = _calc_next_run(interval_str, now)
        now_s = now.isoformat(timespec="seconds")
        with self._memory._conn() as conn:
            conn.execute(
                "INSERT INTO scheduled_tasks (id, session_id, prompt, interval_str, notify_to, next_run, created_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, session_id, prompt, interval_str, notify_to, next_run.isoformat(), now_s),
            )
        logger.info("[scheduler] task %s added: %s every %s", task_id, prompt[:50], interval_str)
        return task_id

    def list_tasks(self, session_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM scheduled_tasks WHERE enabled=1"
        args: tuple = ()
        if session_id:
            query += " AND session_id=?"
            args = (session_id,)
        with self._memory._conn() as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def disable_task(self, task_id: str) -> bool:
        with self._memory._conn() as conn:
            cur = conn.execute("UPDATE scheduled_tasks SET enabled=0 WHERE id=?", (task_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Execution loop
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Main scheduler loop. Run with asyncio.create_task()."""
        logger.info("[scheduler] started (check interval: %ds)", self.CHECK_INTERVAL)
        while True:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.error("[scheduler] tick error: %s", exc)
            await asyncio.sleep(self.CHECK_INTERVAL)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        due = self._get_due_tasks(now)
        if not due:
            return
        logger.info("[scheduler] %d task(s) due", len(due))
        await asyncio.gather(*[self._run_task(t, now) for t in due], return_exceptions=True)

    async def _run_task(self, task: ScheduledTask, now: datetime) -> None:
        logger.info("[scheduler] running task %s: %s", task.id, task.prompt[:60])
        try:
            result = await self._run_fn(task.session_id, task.prompt)
            if self._notify_fn and task.notify_to:
                await self._notify_fn(task.notify_to, f"Scheduled report:\n\n{result}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[scheduler] task %s failed: %s", task.id, exc)
            result = f"Task failed: {exc}"

        next_run = _calc_next_run(task.interval_str, now)
        with self._memory._conn() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET last_run=?, next_run=? WHERE id=?",
                (now.isoformat(timespec="seconds"), next_run.isoformat(), task.id),
            )

    def _get_due_tasks(self, now: datetime) -> list[ScheduledTask]:
        now_s = now.isoformat(timespec="seconds")
        with self._memory._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE enabled=1 AND next_run <= ?", (now_s,)
            ).fetchall()
        tasks = []
        for r in rows:
            tasks.append(ScheduledTask(
                id=r["id"],
                session_id=r["session_id"],
                prompt=r["prompt"],
                interval_str=r["interval_str"],
                notify_to=r["notify_to"],
                last_run=datetime.fromisoformat(r["last_run"]) if r["last_run"] else None,
                next_run=datetime.fromisoformat(r["next_run"]),
                enabled=bool(r["enabled"]),
            ))
        return tasks

    def _ensure_schema(self) -> None:
        with self._memory._conn() as conn:
            conn.executescript(_DDL)
