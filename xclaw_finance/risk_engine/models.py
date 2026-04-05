"""Risk engine domain models."""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from datetime import datetime
from typing import Optional


class RiskDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class GuardType(str, Enum):
    DRAWDOWN = "drawdown"
    RATE_LIMIT = "rate_limit"
    EXPOSURE = "exposure"
    CONCENTRATION = "concentration"


@dataclass
class RiskConfig:
    """
    Per-agent risk limits. All percentage values are fractions (0.05 = 5%).
    Set a limit to None to disable that guard for the agent.
    """
    agent_id: str
    total_capital_usd: Decimal              # baseline capital for % calculations

    # Drawdown guard
    max_daily_drawdown_pct: Optional[Decimal] = Decimal("0.05")   # 5% daily loss

    # Rate limit guard
    max_trades_per_minute: Optional[int] = 10
    max_trades_per_day: Optional[int] = 200

    # Exposure guard
    max_open_exposure_pct: Optional[Decimal] = Decimal("0.80")    # 80% of capital in open positions
    max_open_exposure_approval_pct: Optional[Decimal] = Decimal("0.60")  # escalate at 60%

    # Concentration guard
    max_single_asset_pct: Optional[Decimal] = Decimal("0.40")     # max 40% in one asset

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        def _s(v) -> Optional[str]:
            return str(v) if v is not None else None

        return {
            "agent_id": self.agent_id,
            "total_capital_usd": str(self.total_capital_usd),
            "max_daily_drawdown_pct": _s(self.max_daily_drawdown_pct),
            "max_trades_per_minute": self.max_trades_per_minute,
            "max_trades_per_day": self.max_trades_per_day,
            "max_open_exposure_pct": _s(self.max_open_exposure_pct),
            "max_open_exposure_approval_pct": _s(self.max_open_exposure_approval_pct),
            "max_single_asset_pct": _s(self.max_single_asset_pct),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class ExposureState:
    """Live snapshot of an agent's financial risk position."""
    agent_id: str
    total_capital_usd: Decimal
    open_exposure_usd: Decimal              # sum of open long positions in USD
    daily_realized_pnl: Decimal             # today's closed P&L
    daily_volume_usd: Decimal               # total USD traded today
    asset_distribution: dict[str, Decimal]  # {BTC: Decimal("0.45"), ...} — fraction of capital
    positions: dict[str, dict]              # {BTC: {amount, avg_cost_usd}}
    as_of: datetime = field(default_factory=datetime.utcnow)

    @property
    def open_exposure_pct(self) -> Decimal:
        if self.total_capital_usd == 0:
            return Decimal("0")
        return (self.open_exposure_usd / self.total_capital_usd).quantize(Decimal("0.0001"))

    @property
    def drawdown_pct(self) -> Decimal:
        """Positive number = loss fraction of capital."""
        if self.total_capital_usd == 0:
            return Decimal("0")
        if self.daily_realized_pnl >= 0:
            return Decimal("0")
        return (-self.daily_realized_pnl / self.total_capital_usd).quantize(Decimal("0.0001"))

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "total_capital_usd": str(self.total_capital_usd),
            "open_exposure_usd": str(self.open_exposure_usd),
            "open_exposure_pct": str(self.open_exposure_pct),
            "daily_realized_pnl": str(self.daily_realized_pnl),
            "daily_volume_usd": str(self.daily_volume_usd),
            "drawdown_pct": str(self.drawdown_pct),
            "asset_distribution": {k: str(v) for k, v in self.asset_distribution.items()},
            "positions": self.positions,
            "as_of": self.as_of.isoformat(),
        }


@dataclass
class RiskEvalResult:
    decision: RiskDecision
    guard: Optional[GuardType]       # which guard triggered (None on ALLOW)
    reason: str
    metadata: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == RiskDecision.ALLOW

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "guard": self.guard.value if self.guard else None,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass
class RiskContext:
    """Everything the risk engine needs to evaluate a proposed action."""
    agent_id: str
    action: str                     # "buy" | "sell"
    asset: str
    amount_usd: Decimal
    wallet_id: str
