"""Analytics domain models — P&L and behavioral metrics."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


# ────────────────────────────────────────────────────── P&L models

@dataclass
class AssetPnL:
    """Realized P&L, volume, and open position for a single asset."""
    asset: str
    realized_pnl: Decimal       # sum of realized_pnl from sell fills
    volume_usd: Decimal         # total notional traded (buys + sells)
    fills_count: int
    buy_fills: int
    sell_fills: int
    open_amount: Decimal = Decimal("0")     # current position size
    avg_cost_usd: Decimal = Decimal("0")   # average cost basis

    @property
    def open_value_usd(self) -> Decimal:
        """Current open position value at cost basis (not mark-to-market)."""
        return (self.open_amount * self.avg_cost_usd).quantize(Decimal("0.01"))

    def to_dict(self) -> dict:
        return {
            "asset":          self.asset,
            "realized_pnl":   str(self.realized_pnl),
            "volume_usd":     str(self.volume_usd),
            "fills_count":    self.fills_count,
            "buy_fills":      self.buy_fills,
            "sell_fills":     self.sell_fills,
            "open_amount":    str(self.open_amount),
            "avg_cost_usd":   str(self.avg_cost_usd),
            "open_value_usd": str(self.open_value_usd),
        }


@dataclass
class PnLReport:
    """Aggregate realized P&L for an agent over an optional time window."""
    agent_id: str
    total_realized_pnl: Decimal
    total_volume_usd: Decimal
    total_fills: int
    per_asset: dict[str, AssetPnL]
    period_start: Optional[str]         # ISO string or None (all-time)
    period_end: Optional[str]
    generated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def profitable_assets(self) -> int:
        return sum(1 for a in self.per_asset.values() if a.realized_pnl > 0)

    @property
    def losing_assets(self) -> int:
        return sum(1 for a in self.per_asset.values() if a.realized_pnl < 0)

    def to_dict(self) -> dict:
        return {
            "agent_id":           self.agent_id,
            "total_realized_pnl": str(self.total_realized_pnl),
            "total_volume_usd":   str(self.total_volume_usd),
            "total_fills":        self.total_fills,
            "profitable_assets":  self.profitable_assets,
            "losing_assets":      self.losing_assets,
            "per_asset":          {k: v.to_dict() for k, v in self.per_asset.items()},
            "period_start":       self.period_start,
            "period_end":         self.period_end,
            "generated_at":       self.generated_at.isoformat(),
        }


# ────────────────────────────────────────────────────── Metrics models

@dataclass
class AgentMetrics:
    """
    Execution and behavioral metrics for an agent.

    Derived from the audit_log (all decisions) and risk_fills (volume/timing).
    """
    agent_id: str
    period_start: Optional[str]
    period_end: Optional[str]

    # ── action counts ──
    total_actions: int          # all audit entries
    trades_executed: int        # policy=allow + execution success
    trades_failed: int          # policy=allow + execution failure (adapter error)
    trades_denied: int          # policy:deny or risk:deny
    trades_pending: int         # require_approval entries

    # ── rates ──
    denial_rate: Decimal            # denied / total_actions
    approval_required_rate: Decimal # pending / total_actions

    # ── timing ──
    avg_execution_time_ms: Decimal  # mean of (filled_at - created_at) for filled orders

    # ── volume ──
    total_volume_usd: Decimal       # from risk_fills (accurate; 0 if risk engine not wired)

    # ── simulation split ──
    simulation_trades: int          # metadata["simulation"] = True
    real_trades: int                # metadata["simulation"] = False

    generated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "agent_id":               self.agent_id,
            "period_start":           self.period_start,
            "period_end":             self.period_end,
            "total_actions":          self.total_actions,
            "trades_executed":        self.trades_executed,
            "trades_failed":          self.trades_failed,
            "trades_denied":          self.trades_denied,
            "trades_pending":         self.trades_pending,
            "denial_rate":            str(self.denial_rate),
            "approval_required_rate": str(self.approval_required_rate),
            "avg_execution_time_ms":  str(self.avg_execution_time_ms),
            "total_volume_usd":       str(self.total_volume_usd),
            "simulation_trades":      self.simulation_trades,
            "real_trades":            self.real_trades,
            "generated_at":           self.generated_at.isoformat(),
        }
