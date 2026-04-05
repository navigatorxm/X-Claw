"""
Exposure tracker — SQLite-backed per-agent position and P&L accounting.

Tracks:
- Open positions per asset (amount + average cost basis)
- Realized P&L per day
- Trade history (for rate limiting and volume reporting)

All writes are atomic within a single SQLite connection.
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .models import ExposureState


class ExposureTracker:

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._connect() as conn:
            # Agent capital baseline
            conn.execute("""
                CREATE TABLE IF NOT EXISTS risk_capital (
                    agent_id            TEXT PRIMARY KEY,
                    total_capital_usd   TEXT NOT NULL DEFAULT '0',
                    updated_at          TEXT NOT NULL
                )
            """)
            # Open positions — one row per (agent, asset)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS risk_positions (
                    agent_id    TEXT NOT NULL,
                    asset       TEXT NOT NULL,
                    amount      TEXT NOT NULL DEFAULT '0',
                    avg_cost    TEXT NOT NULL DEFAULT '0',
                    updated_at  TEXT NOT NULL,
                    PRIMARY KEY (agent_id, asset)
                )
            """)
            # Immutable fill log — for rate-limiting, P&L, volume queries
            conn.execute("""
                CREATE TABLE IF NOT EXISTS risk_fills (
                    fill_id         TEXT PRIMARY KEY,
                    agent_id        TEXT NOT NULL,
                    asset           TEXT NOT NULL,
                    side            TEXT NOT NULL,
                    amount          TEXT NOT NULL,
                    price_usd       TEXT NOT NULL,
                    amount_usd      TEXT NOT NULL,
                    realized_pnl    TEXT NOT NULL DEFAULT '0',
                    timestamp       TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_agent_ts ON risk_fills(agent_id, timestamp)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------------- capital
    def set_capital(self, agent_id: str, total_capital_usd: Decimal) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO risk_capital (agent_id, total_capital_usd, updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET
                       total_capital_usd = excluded.total_capital_usd,
                       updated_at = excluded.updated_at""",
                (agent_id, str(total_capital_usd), now),
            )

    def get_capital(self, agent_id: str) -> Decimal:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT total_capital_usd FROM risk_capital WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return Decimal(row["total_capital_usd"]) if row else Decimal("0")

    # ------------------------------------------------------------------- fills
    def record_fill(
        self,
        agent_id: str,
        side: str,          # "buy" | "sell"
        asset: str,
        amount: Decimal,
        price_usd: Decimal,
    ) -> Decimal:
        """
        Record a completed fill. Updates open positions and calculates realized P&L.
        Returns realized P&L for this fill (0 for buys).
        """
        amount_usd = (amount * price_usd).quantize(Decimal("0.01"))
        realized_pnl = Decimal("0")
        now = datetime.utcnow().isoformat()

        with self._connect() as conn:
            # Fetch existing position
            row = conn.execute(
                "SELECT amount, avg_cost FROM risk_positions WHERE agent_id = ? AND asset = ?",
                (agent_id, asset),
            ).fetchone()
            pos_amount = Decimal(row["amount"]) if row else Decimal("0")
            pos_avg_cost = Decimal(row["avg_cost"]) if row else Decimal("0")

            if side == "buy":
                # Update average cost basis
                total_cost = pos_amount * pos_avg_cost + amount * price_usd
                new_amount = pos_amount + amount
                new_avg_cost = (total_cost / new_amount).quantize(Decimal("0.00001")) if new_amount else Decimal("0")
                conn.execute(
                    """INSERT INTO risk_positions (agent_id, asset, amount, avg_cost, updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(agent_id, asset) DO UPDATE SET
                           amount = excluded.amount,
                           avg_cost = excluded.avg_cost,
                           updated_at = excluded.updated_at""",
                    (agent_id, asset, str(new_amount), str(new_avg_cost), now),
                )
            else:  # sell
                # Realize P&L
                realized_pnl = ((price_usd - pos_avg_cost) * amount).quantize(Decimal("0.01"))
                new_amount = max(Decimal("0"), pos_amount - amount)
                conn.execute(
                    """INSERT INTO risk_positions (agent_id, asset, amount, avg_cost, updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(agent_id, asset) DO UPDATE SET
                           amount = excluded.amount,
                           updated_at = excluded.updated_at""",
                    (agent_id, asset, str(new_amount), str(pos_avg_cost), now),
                )

            # Log the fill
            conn.execute(
                """INSERT INTO risk_fills
                   (fill_id, agent_id, asset, side, amount, price_usd, amount_usd, realized_pnl, timestamp)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    f"fill_{uuid.uuid4().hex[:12]}",
                    agent_id, asset, side,
                    str(amount), str(price_usd), str(amount_usd),
                    str(realized_pnl), now,
                ),
            )

        return realized_pnl

    # --------------------------------------------------------------- queries
    def get_state(self, agent_id: str, config_capital: Optional[Decimal] = None) -> ExposureState:
        """Return current risk state for an agent."""
        capital = config_capital or self.get_capital(agent_id)
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        with self._connect() as conn:
            # Open positions
            pos_rows = conn.execute(
                "SELECT asset, amount, avg_cost FROM risk_positions WHERE agent_id = ? AND CAST(amount AS REAL) > 0",
                (agent_id,),
            ).fetchall()

            # Today's fills
            fill_rows = conn.execute(
                """SELECT side, amount, price_usd, amount_usd, realized_pnl
                   FROM risk_fills
                   WHERE agent_id = ? AND timestamp >= ?""",
                (agent_id, today_start),
            ).fetchall()

        # Build positions dict and open exposure
        positions: dict[str, dict] = {}
        open_exposure = Decimal("0")
        asset_values: dict[str, Decimal] = {}

        for row in pos_rows:
            amt = Decimal(row["amount"])
            avg_cost = Decimal(row["avg_cost"])
            pos_usd = (amt * avg_cost).quantize(Decimal("0.01"))
            positions[row["asset"]] = {
                "amount": str(amt),
                "avg_cost_usd": str(avg_cost),
                "value_usd": str(pos_usd),
            }
            open_exposure += pos_usd
            asset_values[row["asset"]] = pos_usd

        # Daily P&L and volume
        daily_pnl = Decimal("0")
        daily_volume = Decimal("0")
        for row in fill_rows:
            daily_pnl += Decimal(row["realized_pnl"])
            daily_volume += Decimal(row["amount_usd"])

        # Asset distribution (fraction of total capital)
        asset_distribution: dict[str, Decimal] = {}
        if capital > 0:
            for asset, usd_val in asset_values.items():
                asset_distribution[asset] = (usd_val / capital).quantize(Decimal("0.0001"))

        return ExposureState(
            agent_id=agent_id,
            total_capital_usd=capital,
            open_exposure_usd=open_exposure,
            daily_realized_pnl=daily_pnl,
            daily_volume_usd=daily_volume,
            asset_distribution=asset_distribution,
            positions=positions,
        )

    def get_trade_count(self, agent_id: str, since_minutes: Optional[int] = None) -> int:
        """Count fills since `since_minutes` ago (or today if None)."""
        if since_minutes is not None:
            since = (datetime.utcnow() - timedelta(minutes=since_minutes)).isoformat()
        else:
            since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM risk_fills WHERE agent_id = ? AND timestamp >= ?",
                (agent_id, since),
            ).fetchone()
        return row[0]

    def get_daily_pnl(self, agent_id: str) -> Decimal:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) FROM risk_fills WHERE agent_id = ? AND timestamp >= ?",
                (agent_id, today_start),
            ).fetchone()
        return Decimal(str(row[0])).quantize(Decimal("0.01"))
