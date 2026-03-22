"""
Drawdown guard — blocks execution when daily losses breach the configured threshold.

Logic:
  daily_loss_pct = -daily_realized_pnl / total_capital   (only when pnl is negative)

  If daily_loss_pct >= max_daily_drawdown_pct → DENY
  (There is no escalation to approval here — a breached drawdown is a hard stop.)
"""
from __future__ import annotations
from decimal import Decimal

from .exposure_tracker import ExposureTracker
from .models import GuardType, RiskConfig, RiskDecision, RiskEvalResult


class DrawdownGuard:
    """
    Hard daily drawdown stop.

    If the agent has already lost more than `max_daily_drawdown_pct` of their
    total capital today, all further execution is blocked until the next UTC day.
    """

    def __init__(self, tracker: ExposureTracker) -> None:
        self._tracker = tracker

    def check(self, agent_id: str, config: RiskConfig) -> RiskEvalResult:
        """
        Check current drawdown against the configured limit.

        Does NOT consider the incoming trade — this is a state check only.
        If the limit is already breached, no new trades are allowed.
        """
        if config.max_daily_drawdown_pct is None:
            return RiskEvalResult(
                decision=RiskDecision.ALLOW,
                guard=None,
                reason="Drawdown guard disabled for this agent.",
            )

        daily_pnl = self._tracker.get_daily_pnl(agent_id)
        capital = config.total_capital_usd

        if capital <= 0:
            return RiskEvalResult(
                decision=RiskDecision.ALLOW,
                guard=None,
                reason="Capital not set — drawdown guard skipped.",
            )

        # Only a loss is a drawdown
        if daily_pnl >= 0:
            return RiskEvalResult(
                decision=RiskDecision.ALLOW,
                guard=None,
                reason=f"No drawdown today (P&L: +${daily_pnl}).",
            )

        current_drawdown_pct = (-daily_pnl / capital).quantize(Decimal("0.0001"))
        limit = config.max_daily_drawdown_pct

        if current_drawdown_pct >= limit:
            loss_usd = -daily_pnl
            return RiskEvalResult(
                decision=RiskDecision.DENY,
                guard=GuardType.DRAWDOWN,
                reason=(
                    f"Daily drawdown limit breached: lost ${loss_usd} "
                    f"({current_drawdown_pct * 100:.2f}% of ${capital} capital). "
                    f"Limit is {limit * 100:.2f}%. Trading halted until next UTC day."
                ),
                metadata={
                    "daily_pnl": str(daily_pnl),
                    "drawdown_pct": str(current_drawdown_pct),
                    "limit_pct": str(limit),
                    "capital_usd": str(capital),
                },
            )

        remaining_pct = (limit - current_drawdown_pct).quantize(Decimal("0.0001"))
        return RiskEvalResult(
            decision=RiskDecision.ALLOW,
            guard=None,
            reason=(
                f"Drawdown OK: {current_drawdown_pct * 100:.2f}% of limit "
                f"{limit * 100:.2f}%. Remaining buffer: {remaining_pct * 100:.2f}%."
            ),
            metadata={
                "daily_pnl": str(daily_pnl),
                "drawdown_pct": str(current_drawdown_pct),
                "remaining_pct": str(remaining_pct),
            },
        )
