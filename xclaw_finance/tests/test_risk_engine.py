"""
Tests for the risk engine module.

Covers:
- Drawdown guard: no breach, breach, exactly-at-limit
- Rate limit guard: per-minute, per-day
- Exposure guard: hard cap, approval threshold, concentration cap
- RiskEngine: combined evaluation, no-config passthrough
- ExposureTracker: buy/sell P&L accounting, state queries
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from risk_engine.drawdown_guard import DrawdownGuard
from risk_engine.exposure_tracker import ExposureTracker
from risk_engine.models import (
    GuardType,
    RiskConfig,
    RiskContext,
    RiskDecision,
)
from risk_engine.rate_limit_guard import RateLimitGuard
from risk_engine.risk_engine import RiskConfigStore, RiskEngine


# ─────────────────────────────────────────── helpers

@pytest.fixture
def tracker(tmp_path):
    return ExposureTracker(db_path=str(tmp_path / "risk.db"))


@pytest.fixture
def config_store(tmp_path):
    return RiskConfigStore(db_path=str(tmp_path / "risk.db"))


@pytest.fixture
def engine(tmp_path):
    db = str(tmp_path / "risk.db")
    return RiskEngine(
        config_store=RiskConfigStore(db_path=db),
        tracker=ExposureTracker(db_path=db),
    )


def _config(
    capital: str = "10000",
    drawdown_pct: str = "0.05",
    trades_per_min: int = 5,
    trades_per_day: int = 100,
    exposure_pct: str = "0.80",
    exposure_approval_pct: str = "0.60",
    concentration_pct: str = "0.40",
    agent_id: str = "agent_1",
) -> RiskConfig:
    return RiskConfig(
        agent_id=agent_id,
        total_capital_usd=Decimal(capital),
        max_daily_drawdown_pct=Decimal(drawdown_pct),
        max_trades_per_minute=trades_per_min,
        max_trades_per_day=trades_per_day,
        max_open_exposure_pct=Decimal(exposure_pct),
        max_open_exposure_approval_pct=Decimal(exposure_approval_pct),
        max_single_asset_pct=Decimal(concentration_pct),
    )


def _ctx(
    agent_id: str = "agent_1",
    action: str = "buy",
    asset: str = "BTC",
    amount_usd: str = "500",
) -> RiskContext:
    return RiskContext(
        agent_id=agent_id,
        action=action,
        asset=asset,
        amount_usd=Decimal(amount_usd),
        wallet_id="w_test",
    )


# ═══════════════════════════════════════════ ExposureTracker

class TestExposureTracker:

    def test_buy_creates_position(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("0.1"), Decimal("50000"))
        state = tracker.get_state("a1")
        assert "BTC" in state.positions
        assert Decimal(state.positions["BTC"]["amount"]) == Decimal("0.1")

    def test_sell_realizes_pnl(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        # Buy at 50000
        tracker.record_fill("a1", "buy", "BTC", Decimal("0.1"), Decimal("50000"))
        # Sell at 60000 → profit of 1000
        pnl = tracker.record_fill("a1", "sell", "BTC", Decimal("0.1"), Decimal("60000"))
        assert pnl == Decimal("1000.00")

    def test_sell_at_loss_realizes_negative_pnl(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("1.0"), Decimal("60000"))
        pnl = tracker.record_fill("a1", "sell", "BTC", Decimal("1.0"), Decimal("50000"))
        assert pnl == Decimal("-10000.00")

    def test_average_cost_basis_updates_on_multiple_buys(self, tracker):
        tracker.set_capital("a1", Decimal("100000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("1.0"), Decimal("40000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("1.0"), Decimal("60000"))
        state = tracker.get_state("a1")
        # avg cost = (40000 + 60000) / 2 = 50000
        avg = Decimal(state.positions["BTC"]["avg_cost_usd"])
        assert avg == Decimal("50000")

    def test_daily_pnl_only_from_sells(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("0.1"), Decimal("50000"))
        pnl = tracker.get_daily_pnl("a1")
        assert pnl == Decimal("0")  # no P&L from a buy

    def test_trade_count_per_minute(self, tracker):
        for _ in range(3):
            tracker.record_fill("a1", "buy", "BTC", Decimal("0.01"), Decimal("50000"))
        assert tracker.get_trade_count("a1", since_minutes=1) == 3

    def test_trade_count_today(self, tracker):
        for _ in range(5):
            tracker.record_fill("a1", "buy", "ETH", Decimal("0.1"), Decimal("3000"))
        assert tracker.get_trade_count("a1") == 5

    def test_open_exposure_usd(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("0.1"), Decimal("50000"))  # $5000
        state = tracker.get_state("a1")
        assert state.open_exposure_usd == Decimal("5000.00")

    def test_exposure_pct(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("0.1"), Decimal("50000"))  # $5000 = 50%
        state = tracker.get_state("a1", config_capital=Decimal("10000"))
        assert state.open_exposure_pct == Decimal("0.5000")


# ═══════════════════════════════════════════ DrawdownGuard

class TestDrawdownGuard:

    def test_no_trades_allows(self, tracker):
        guard = DrawdownGuard(tracker)
        result = guard.check("a1", _config(capital="10000", drawdown_pct="0.05"))
        assert result.decision == RiskDecision.ALLOW

    def test_profit_always_allows(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("1.0"), Decimal("50000"))
        tracker.record_fill("a1", "sell", "BTC", Decimal("1.0"), Decimal("55000"))  # +5000
        guard = DrawdownGuard(tracker)
        result = guard.check("a1", _config(capital="10000", drawdown_pct="0.05"))
        assert result.decision == RiskDecision.ALLOW

    def test_loss_under_limit_allows(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("1.0"), Decimal("10000"))
        tracker.record_fill("a1", "sell", "BTC", Decimal("1.0"), Decimal("9600"))  # -400 = 4%
        guard = DrawdownGuard(tracker)
        result = guard.check("a1", _config(capital="10000", drawdown_pct="0.05"))
        assert result.decision == RiskDecision.ALLOW

    def test_loss_exactly_at_limit_denies(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("1.0"), Decimal("10000"))
        tracker.record_fill("a1", "sell", "BTC", Decimal("1.0"), Decimal("9500"))  # -500 = 5%
        guard = DrawdownGuard(tracker)
        result = guard.check("a1", _config(capital="10000", drawdown_pct="0.05"))
        assert result.decision == RiskDecision.DENY
        assert result.guard == GuardType.DRAWDOWN

    def test_loss_exceeds_limit_denies(self, tracker):
        tracker.set_capital("a1", Decimal("10000"))
        tracker.record_fill("a1", "buy", "BTC", Decimal("1.0"), Decimal("10000"))
        tracker.record_fill("a1", "sell", "BTC", Decimal("1.0"), Decimal("8900"))  # -1100 = 11%
        guard = DrawdownGuard(tracker)
        result = guard.check("a1", _config(capital="10000", drawdown_pct="0.05"))
        assert result.decision == RiskDecision.DENY
        assert "11.00%" in result.reason or "drawdown" in result.reason.lower()

    def test_guard_disabled_allows(self, tracker):
        cfg = _config()
        cfg.max_daily_drawdown_pct = None
        guard = DrawdownGuard(tracker)
        result = guard.check("a1", cfg)
        assert result.decision == RiskDecision.ALLOW


# ═══════════════════════════════════════════ RateLimitGuard

class TestRateLimitGuard:

    def test_no_trades_allows(self, tracker):
        guard = RateLimitGuard(tracker)
        result = guard.check("a1", _config())
        assert result.decision == RiskDecision.ALLOW

    def test_under_per_minute_limit_allows(self, tracker):
        for _ in range(4):  # limit is 5
            tracker.record_fill("a1", "buy", "BTC", Decimal("0.01"), Decimal("50000"))
        guard = RateLimitGuard(tracker)
        result = guard.check("a1", _config(trades_per_min=5))
        assert result.decision == RiskDecision.ALLOW

    def test_at_per_minute_limit_denies(self, tracker):
        for _ in range(5):  # limit is 5, so 5 fills → deny
            tracker.record_fill("a1", "buy", "BTC", Decimal("0.01"), Decimal("50000"))
        guard = RateLimitGuard(tracker)
        result = guard.check("a1", _config(trades_per_min=5))
        assert result.decision == RiskDecision.DENY
        assert result.guard == GuardType.RATE_LIMIT
        assert result.metadata["window"] == "1m"

    def test_at_daily_limit_denies(self, tracker):
        for _ in range(3):  # daily limit is 3
            tracker.record_fill("a1", "buy", "BTC", Decimal("0.01"), Decimal("50000"))
        guard = RateLimitGuard(tracker)
        result = guard.check("a1", _config(trades_per_min=100, trades_per_day=3))
        assert result.decision == RiskDecision.DENY
        assert result.guard == GuardType.RATE_LIMIT
        assert result.metadata["window"] == "day"

    def test_different_agents_independent(self, tracker):
        for _ in range(5):
            tracker.record_fill("a1", "buy", "BTC", Decimal("0.01"), Decimal("50000"))
        guard = RateLimitGuard(tracker)
        # a2 has no trades — should be allowed
        result = guard.check("a2", _config(trades_per_min=5))
        assert result.decision == RiskDecision.ALLOW

    def test_disabled_limit_allows(self, tracker):
        for _ in range(100):
            tracker.record_fill("a1", "buy", "BTC", Decimal("0.01"), Decimal("50000"))
        cfg = _config()
        cfg.max_trades_per_minute = None
        cfg.max_trades_per_day = None
        guard = RateLimitGuard(tracker)
        result = guard.check("a1", cfg)
        assert result.decision == RiskDecision.ALLOW


# ═══════════════════════════════════════════ RiskEngine (combined)

class TestRiskEngine:

    def test_no_config_passes_through(self, engine):
        result = engine.evaluate(_ctx())
        assert result.decision == RiskDecision.ALLOW
        assert "no risk config" in result.reason.lower()

    def test_all_guards_pass(self, engine):
        cfg = _config()
        engine._configs.upsert(cfg)
        result = engine.evaluate(_ctx(amount_usd="500"))
        assert result.decision == RiskDecision.ALLOW

    def test_drawdown_breach_blocks(self, engine):
        cfg = _config(capital="10000", drawdown_pct="0.05")
        engine._configs.upsert(cfg)
        # Manufacture a 6% loss
        engine._tracker.set_capital("agent_1", Decimal("10000"))
        engine._tracker.record_fill("agent_1", "buy", "BTC", Decimal("1"), Decimal("10000"))
        engine._tracker.record_fill("agent_1", "sell", "BTC", Decimal("1"), Decimal("9400"))  # -600 = 6%
        result = engine.evaluate(_ctx())
        assert result.decision == RiskDecision.DENY
        assert result.guard == GuardType.DRAWDOWN

    def test_rate_limit_breach_blocks(self, engine):
        cfg = _config(trades_per_min=3)
        engine._configs.upsert(cfg)
        for _ in range(3):
            engine._tracker.record_fill("agent_1", "buy", "BTC", Decimal("0.01"), Decimal("50000"))
        result = engine.evaluate(_ctx())
        assert result.decision == RiskDecision.DENY
        assert result.guard == GuardType.RATE_LIMIT

    def test_exposure_hard_cap_blocks(self, engine):
        cfg = _config(capital="10000", exposure_pct="0.80", exposure_approval_pct="0.60")
        engine._configs.upsert(cfg)
        # Already have $7000 open (70%)
        engine._tracker.set_capital("agent_1", Decimal("10000"))
        engine._tracker.record_fill("agent_1", "buy", "BTC", Decimal("0.14"), Decimal("50000"))  # $7000
        # Try to add $2000 more → 90% > 80% hard cap
        result = engine.evaluate(_ctx(amount_usd="2000"))
        assert result.decision == RiskDecision.DENY
        assert result.guard == GuardType.EXPOSURE

    def test_exposure_approval_threshold_escalates(self, engine):
        cfg = _config(capital="10000", exposure_pct="0.80", exposure_approval_pct="0.60")
        engine._configs.upsert(cfg)
        # Already $5000 open (50%), adding $2000 → 70% > approval threshold 60%
        engine._tracker.set_capital("agent_1", Decimal("10000"))
        engine._tracker.record_fill("agent_1", "buy", "BTC", Decimal("0.1"), Decimal("50000"))  # $5000
        result = engine.evaluate(_ctx(amount_usd="2000"))
        assert result.decision == RiskDecision.REQUIRE_APPROVAL
        assert result.guard == GuardType.EXPOSURE

    def test_concentration_breach_blocks(self, engine):
        cfg = _config(capital="10000", concentration_pct="0.40")
        engine._configs.upsert(cfg)
        # $3500 in BTC (35%), adding $1000 → 45% > 40%
        engine._tracker.set_capital("agent_1", Decimal("10000"))
        engine._tracker.record_fill("agent_1", "buy", "BTC", Decimal("0.07"), Decimal("50000"))  # $3500
        result = engine.evaluate(_ctx(asset="BTC", amount_usd="1000"))
        assert result.decision == RiskDecision.DENY
        assert result.guard == GuardType.CONCENTRATION

    def test_sell_bypasses_exposure_guard(self, engine):
        cfg = _config(capital="10000", exposure_pct="0.20")  # very tight cap
        engine._configs.upsert(cfg)
        # Even if exposure is over cap, sells should not be blocked by exposure guard
        engine._tracker.set_capital("agent_1", Decimal("10000"))
        engine._tracker.record_fill("agent_1", "buy", "BTC", Decimal("0.5"), Decimal("50000"))  # $25000 > 80% of 10000
        result = engine.evaluate(_ctx(action="sell", amount_usd="1000"))
        # sell should only fail on drawdown/rate — not exposure
        if result.decision == RiskDecision.DENY:
            assert result.guard != GuardType.EXPOSURE

    def test_record_execution_updates_state(self, engine):
        cfg = _config()
        engine._configs.upsert(cfg)
        engine.record_execution("agent_1", "buy", "BTC", Decimal("0.1"), Decimal("50000"))
        state = engine.get_state("agent_1")
        assert "BTC" in state.positions

    def test_drawdown_deny_takes_priority_over_rate_limit(self, engine):
        """Drawdown check runs first — if it denies, rate limit is never evaluated."""
        cfg = _config(capital="10000", drawdown_pct="0.01", trades_per_min=100)
        engine._configs.upsert(cfg)
        # 5% loss breaches 1% drawdown limit
        engine._tracker.set_capital("agent_1", Decimal("10000"))
        engine._tracker.record_fill("agent_1", "buy", "BTC", Decimal("1"), Decimal("10000"))
        engine._tracker.record_fill("agent_1", "sell", "BTC", Decimal("1"), Decimal("9800"))  # -200 = 2%
        result = engine.evaluate(_ctx())
        assert result.decision == RiskDecision.DENY
        assert result.guard == GuardType.DRAWDOWN


# ═══════════════════════════════════════════ RiskConfigStore

class TestRiskConfigStore:

    def test_upsert_and_get(self, config_store):
        cfg = _config()
        config_store.upsert(cfg)
        fetched = config_store.get("agent_1")
        assert fetched is not None
        assert fetched.agent_id == "agent_1"
        assert fetched.total_capital_usd == Decimal("10000")

    def test_upsert_updates_existing(self, config_store):
        config_store.upsert(_config(capital="10000"))
        config_store.upsert(_config(capital="20000"))
        fetched = config_store.get("agent_1")
        assert fetched.total_capital_usd == Decimal("20000")

    def test_get_missing_returns_none(self, config_store):
        assert config_store.get("nonexistent") is None

    def test_nullable_fields_roundtrip(self, config_store):
        cfg = _config()
        cfg.max_daily_drawdown_pct = None
        cfg.max_trades_per_minute = None
        config_store.upsert(cfg)
        fetched = config_store.get("agent_1")
        assert fetched.max_daily_drawdown_pct is None
        assert fetched.max_trades_per_minute is None
