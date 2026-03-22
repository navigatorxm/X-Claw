"""
Rate limit guard — caps trades per minute and per day.

Both limits are checked independently; the stricter one wins.
Exceeding a rate limit is always a hard DENY — not an escalation.
Operators cannot approve their way past a rate limit; it's a systemic control.
"""
from __future__ import annotations

from .exposure_tracker import ExposureTracker
from .models import GuardType, RiskConfig, RiskDecision, RiskEvalResult


class RateLimitGuard:
    """
    Enforces maximum trade frequency per agent.

    - Per-minute: prevents burst/runaway execution
    - Per-day: hard daily cap regardless of size
    """

    def __init__(self, tracker: ExposureTracker) -> None:
        self._tracker = tracker

    def check(self, agent_id: str, config: RiskConfig) -> RiskEvalResult:
        # ── per-minute check ──────────────────────────────────────────
        if config.max_trades_per_minute is not None:
            count_1m = self._tracker.get_trade_count(agent_id, since_minutes=1)
            limit_1m = config.max_trades_per_minute
            if count_1m >= limit_1m:
                return RiskEvalResult(
                    decision=RiskDecision.DENY,
                    guard=GuardType.RATE_LIMIT,
                    reason=(
                        f"Rate limit breached: {count_1m} trades in the last minute "
                        f"(limit: {limit_1m}/min). Wait before retrying."
                    ),
                    metadata={
                        "window": "1m",
                        "count": count_1m,
                        "limit": limit_1m,
                    },
                )

        # ── per-day check ─────────────────────────────────────────────
        if config.max_trades_per_day is not None:
            count_day = self._tracker.get_trade_count(agent_id, since_minutes=None)
            limit_day = config.max_trades_per_day
            if count_day >= limit_day:
                return RiskEvalResult(
                    decision=RiskDecision.DENY,
                    guard=GuardType.RATE_LIMIT,
                    reason=(
                        f"Daily trade limit reached: {count_day} trades today "
                        f"(limit: {limit_day}/day). Resets at next UTC midnight."
                    ),
                    metadata={
                        "window": "day",
                        "count": count_day,
                        "limit": limit_day,
                    },
                )

        count_day = self._tracker.get_trade_count(agent_id, since_minutes=None)
        count_1m = self._tracker.get_trade_count(agent_id, since_minutes=1)
        return RiskEvalResult(
            decision=RiskDecision.ALLOW,
            guard=None,
            reason=f"Rate limits OK: {count_1m}/min, {count_day}/day.",
            metadata={"count_1m": count_1m, "count_day": count_day},
        )
