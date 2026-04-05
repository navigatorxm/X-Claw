"""
PnL Tracker — per-agent, per-asset realized profit and loss.

Data source:
  risk_fills     — immutable fill log (written by ExposureTracker.record_fill)
  risk_positions — current open positions with average cost basis

Fills are populated only when the risk engine is wired into the execution
engine. Without it, all volume and P&L values will be zero.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .models import AssetPnL, PnLReport


class PnLTracker:
    """
    Reads P&L data from the shared SQLite database.

    No writes — purely a read projection over risk_fills and risk_positions.
    """

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    # ─────────────────────────────────────────────────── public API

    def get_pnl(
        self,
        agent_id: str,
        asset: Optional[str] = None,
        start: Optional[str] = None,    # ISO datetime string (inclusive)
        end: Optional[str] = None,      # ISO datetime string (inclusive)
    ) -> PnLReport:
        """
        Compute realized P&L for agent_id.

        Parameters
        ----------
        agent_id : str
        asset    : str, optional — filter to one asset (e.g. "BTC")
        start    : ISO datetime string — lower bound for fills (inclusive)
        end      : ISO datetime string — upper bound for fills (inclusive)

        Returns
        -------
        PnLReport with per-asset breakdown and aggregate totals.
        Open positions always reflect the current state (not historical).
        """
        fills   = self._fetch_fills(agent_id, asset, start, end)
        positions = self._fetch_positions(agent_id, asset)

        # Aggregate fills per asset
        asset_data: dict[str, dict] = {}
        for row in fills:
            a = row["asset"]
            if a not in asset_data:
                asset_data[a] = {
                    "realized_pnl": Decimal("0"),
                    "volume_usd":   Decimal("0"),
                    "fills_count":  0,
                    "buy_fills":    0,
                    "sell_fills":   0,
                }
            asset_data[a]["realized_pnl"] += Decimal(row["realized_pnl"])
            asset_data[a]["volume_usd"]   += Decimal(row["amount_usd"])
            asset_data[a]["fills_count"]  += 1
            if row["side"] == "buy":
                asset_data[a]["buy_fills"] += 1
            else:
                asset_data[a]["sell_fills"] += 1

        # Build AssetPnL objects, merging in open positions
        all_assets = set(asset_data) | set(positions)
        per_asset: dict[str, AssetPnL] = {}
        for a in sorted(all_assets):
            fd  = asset_data.get(a)
            pos = positions.get(a)
            per_asset[a] = AssetPnL(
                asset=a,
                realized_pnl=(fd["realized_pnl"] if fd else Decimal("0")).quantize(Decimal("0.01")),
                volume_usd  =(fd["volume_usd"]   if fd else Decimal("0")).quantize(Decimal("0.01")),
                fills_count =fd["fills_count"]  if fd else 0,
                buy_fills   =fd["buy_fills"]    if fd else 0,
                sell_fills  =fd["sell_fills"]   if fd else 0,
                open_amount =Decimal(pos["amount"])   if pos else Decimal("0"),
                avg_cost_usd=Decimal(pos["avg_cost"]) if pos else Decimal("0"),
            )

        total_pnl    = sum((a.realized_pnl for a in per_asset.values()), Decimal("0"))
        total_volume = sum((a.volume_usd   for a in per_asset.values()), Decimal("0"))
        total_fills  = sum((a.fills_count  for a in per_asset.values()), 0)

        return PnLReport(
            agent_id=agent_id,
            total_realized_pnl=total_pnl.quantize(Decimal("0.01")),
            total_volume_usd=total_volume.quantize(Decimal("0.01")),
            total_fills=total_fills,
            per_asset=per_asset,
            period_start=start,
            period_end=end,
        )

    def get_fills(
        self,
        agent_id: str,
        asset: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return raw fill rows for an agent, most-recent first."""
        rows = self._fetch_fills(agent_id, asset, start, end, order="DESC", limit=limit)
        return [
            {
                "fill_id":      row["fill_id"],
                "asset":        row["asset"],
                "side":         row["side"],
                "amount":       row["amount"],
                "price_usd":    row["price_usd"],
                "amount_usd":   row["amount_usd"],
                "realized_pnl": row["realized_pnl"],
                "timestamp":    row["timestamp"],
            }
            for row in rows
        ]

    # ─────────────────────────────────────────────────── private helpers

    def _fetch_fills(
        self,
        agent_id: str,
        asset: Optional[str],
        start: Optional[str],
        end: Optional[str],
        order: str = "ASC",
        limit: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        params: list = [agent_id]
        clauses = ["agent_id = ?"]
        if asset:
            clauses.append("asset = ?")
            params.append(asset.upper())
        if start:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end:
            clauses.append("timestamp <= ?")
            params.append(end)
        where = " AND ".join(clauses)
        sql = (
            f"SELECT fill_id, asset, side, amount, price_usd, "
            f"amount_usd, realized_pnl, timestamp "
            f"FROM risk_fills WHERE {where} ORDER BY timestamp {order}"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _fetch_positions(
        self,
        agent_id: str,
        asset: Optional[str],
    ) -> dict[str, sqlite3.Row]:
        params: list = [agent_id]
        clauses = ["agent_id = ?", "CAST(amount AS REAL) > 0"]
        if asset:
            clauses.append("asset = ?")
            params.append(asset.upper())
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT asset, amount, avg_cost FROM risk_positions WHERE {where}",
                params,
            ).fetchall()
        return {row["asset"]: row for row in rows}
