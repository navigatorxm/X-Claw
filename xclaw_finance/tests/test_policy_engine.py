"""Tests for the policy engine."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from policy_engine.engine import ActionContext, PolicyEngine
from policy_engine.models import Policy, PolicyDecision, Rule, RuleType
from policy_engine.store import PolicyStore


def _make_engine(policies: list[Policy]) -> PolicyEngine:
    store = MagicMock(spec=PolicyStore)
    store.list_for_agent.return_value = policies
    return PolicyEngine(store=store)


def _ctx(**kwargs) -> ActionContext:
    defaults = dict(
        agent_id="agent_1",
        action="buy",
        asset="BTC",
        amount_usd=Decimal("500"),
        exchange="mock",
        wallet_id="w_test",
        daily_volume_usd=Decimal("0"),
    )
    defaults.update(kwargs)
    return ActionContext(**defaults)


def _policy(*rules: Rule) -> Policy:
    from datetime import datetime
    return Policy(
        policy_id="p_test",
        agent_id="agent_1",
        name="Test Policy",
        rules=list(rules),
        created_at=datetime.utcnow(),
    )


# ---------------------------------------------------------------- no policy
def test_no_policy_denies():
    engine = _make_engine([])
    result = engine.evaluate(_ctx())
    assert result.decision == PolicyDecision.DENY
    assert "No policy" in result.reason


# ---------------------------------------------------------------- max_trade_size
def test_max_trade_size_allow():
    rule = Rule(RuleType.MAX_TRADE_SIZE, Decimal("1000"))
    result = _make_engine([_policy(rule)]).evaluate(_ctx(amount_usd=Decimal("500")))
    assert result.decision == PolicyDecision.ALLOW


def test_max_trade_size_deny():
    rule = Rule(RuleType.MAX_TRADE_SIZE, Decimal("1000"))
    result = _make_engine([_policy(rule)]).evaluate(_ctx(amount_usd=Decimal("1500")))
    assert result.decision == PolicyDecision.DENY
    assert "1500" in result.reason


# ---------------------------------------------------------------- allowed_assets
def test_allowed_assets_pass():
    rule = Rule(RuleType.ALLOWED_ASSETS, ["BTC", "ETH"])
    result = _make_engine([_policy(rule)]).evaluate(_ctx(asset="BTC"))
    assert result.decision == PolicyDecision.ALLOW


def test_allowed_assets_deny():
    rule = Rule(RuleType.ALLOWED_ASSETS, ["BTC", "ETH"])
    result = _make_engine([_policy(rule)]).evaluate(_ctx(asset="DOGE"))
    assert result.decision == PolicyDecision.DENY
    assert "DOGE" in result.reason


# ---------------------------------------------------------------- approval_threshold
def test_approval_threshold_requires():
    rule = Rule(RuleType.APPROVAL_THRESHOLD, Decimal("500"))
    result = _make_engine([_policy(rule)]).evaluate(_ctx(amount_usd=Decimal("500")))
    assert result.decision == PolicyDecision.REQUIRE_APPROVAL


def test_approval_threshold_below_allows():
    rule = Rule(RuleType.APPROVAL_THRESHOLD, Decimal("1000"))
    result = _make_engine([_policy(rule)]).evaluate(_ctx(amount_usd=Decimal("500")))
    assert result.decision == PolicyDecision.ALLOW


# ---------------------------------------------------------------- daily_limit
def test_daily_limit_allow():
    rule = Rule(RuleType.DAILY_LIMIT, Decimal("2000"))
    result = _make_engine([_policy(rule)]).evaluate(
        _ctx(amount_usd=Decimal("500"), daily_volume_usd=Decimal("1000"))
    )
    assert result.decision == PolicyDecision.ALLOW


def test_daily_limit_deny():
    rule = Rule(RuleType.DAILY_LIMIT, Decimal("1000"))
    result = _make_engine([_policy(rule)]).evaluate(
        _ctx(amount_usd=Decimal("600"), daily_volume_usd=Decimal("500"))
    )
    assert result.decision == PolicyDecision.DENY
    assert "Daily limit" in result.reason


# ---------------------------------------------------------------- allowed_exchanges
def test_allowed_exchanges_deny():
    rule = Rule(RuleType.ALLOWED_EXCHANGES, ["binance"])
    result = _make_engine([_policy(rule)]).evaluate(_ctx(exchange="coinbase"))
    assert result.decision == PolicyDecision.DENY


def test_allowed_exchanges_allow():
    rule = Rule(RuleType.ALLOWED_EXCHANGES, ["mock", "binance"])
    result = _make_engine([_policy(rule)]).evaluate(_ctx(exchange="mock"))
    assert result.decision == PolicyDecision.ALLOW


# ---------------------------------------------------------------- combined rules
def test_first_deny_wins():
    """MAX_TRADE_SIZE deny should win even if other rules would allow."""
    rules = [
        Rule(RuleType.MAX_TRADE_SIZE, Decimal("100")),    # will deny (500 > 100)
        Rule(RuleType.ALLOWED_ASSETS, ["BTC", "ETH"]),    # would allow
    ]
    result = _make_engine([_policy(*rules)]).evaluate(_ctx(amount_usd=Decimal("500")))
    assert result.decision == PolicyDecision.DENY


def test_all_rules_pass():
    rules = [
        Rule(RuleType.MAX_TRADE_SIZE, Decimal("1000")),
        Rule(RuleType.ALLOWED_ASSETS, ["BTC", "ETH"]),
        Rule(RuleType.DAILY_LIMIT, Decimal("5000")),
        Rule(RuleType.ALLOWED_EXCHANGES, ["mock"]),
    ]
    result = _make_engine([_policy(*rules)]).evaluate(_ctx())
    assert result.decision == PolicyDecision.ALLOW
