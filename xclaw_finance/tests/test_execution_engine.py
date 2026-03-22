"""Tests for the execution engine with the mock adapter."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import asyncio
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock

from approval_system.models import ApprovalStatus
from approval_system.queue import ApprovalQueue
from audit_logger.logger import AuditLogger
from execution_engine.adapters.mock import MockExchangeAdapter
from execution_engine.engine import (
    ExecutionDeniedError,
    ExecutionEngine,
    ExecutionPendingError,
)
from execution_engine.models import OrderStatus
from policy_engine.engine import PolicyEngine
from policy_engine.models import Policy, PolicyDecision, PolicyEvalResult, Rule, RuleType
from wallet.manager import WalletManager
from wallet.models import WalletStatus


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def wallet_manager(tmp_db):
    wm = WalletManager(db_path=tmp_db)
    wm.register("agent_1", "Test Wallet", "mock", "key", "secret")
    return wm


@pytest.fixture
def engine_allow(tmp_db, wallet_manager):
    """Engine configured to ALLOW all trades."""
    policy_engine = MagicMock(spec=PolicyEngine)
    policy_engine.evaluate.return_value = PolicyEvalResult(
        decision=PolicyDecision.ALLOW,
        policy_id="p_1",
        violated_rule=None,
        reason="All good.",
    )
    approval_queue = ApprovalQueue(db_path=tmp_db)
    audit = AuditLogger(db_path=tmp_db)
    engine = ExecutionEngine(wallet_manager, policy_engine, approval_queue, audit)
    engine.register_adapter(MockExchangeAdapter())
    return engine, wallet_manager


@pytest.fixture
def engine_deny(tmp_db, wallet_manager):
    """Engine configured to DENY all trades."""
    policy_engine = MagicMock(spec=PolicyEngine)
    policy_engine.evaluate.return_value = PolicyEvalResult(
        decision=PolicyDecision.DENY,
        policy_id="p_1",
        violated_rule=Rule(RuleType.MAX_TRADE_SIZE, Decimal("100")),
        reason="Trade too large.",
    )
    approval_queue = ApprovalQueue(db_path=tmp_db)
    audit = AuditLogger(db_path=tmp_db)
    engine = ExecutionEngine(wallet_manager, policy_engine, approval_queue, audit)
    engine.register_adapter(MockExchangeAdapter())
    return engine, wallet_manager


@pytest.fixture
def engine_pending(tmp_db, wallet_manager):
    """Engine configured to REQUIRE_APPROVAL."""
    policy_engine = MagicMock(spec=PolicyEngine)
    policy_engine.evaluate.return_value = PolicyEvalResult(
        decision=PolicyDecision.REQUIRE_APPROVAL,
        policy_id="p_1",
        violated_rule=Rule(RuleType.APPROVAL_THRESHOLD, Decimal("500")),
        reason="Approval required.",
    )
    approval_queue = ApprovalQueue(db_path=tmp_db)
    audit = AuditLogger(db_path=tmp_db)
    engine = ExecutionEngine(wallet_manager, policy_engine, approval_queue, audit)
    engine.register_adapter(MockExchangeAdapter())
    return engine, wallet_manager, approval_queue


# ----------------------------------------------------------------- buy / sell
def test_buy_executes_successfully(engine_allow):
    engine, wm = engine_allow
    wallet_id = wm.list_for_agent("agent_1")[0].wallet_id

    result = asyncio.get_event_loop().run_until_complete(
        engine.execute_trade(
            agent_id="agent_1",
            wallet_id=wallet_id,
            side="buy",
            asset="BTC",
            amount=Decimal("0.01"),
        )
    )
    assert result.success is True
    assert result.order.status == OrderStatus.FILLED
    assert result.filled_amount == Decimal("0.01")


def test_sell_executes_successfully(engine_allow):
    engine, wm = engine_allow
    wallet_id = wm.list_for_agent("agent_1")[0].wallet_id

    result = asyncio.get_event_loop().run_until_complete(
        engine.execute_trade(
            agent_id="agent_1",
            wallet_id=wallet_id,
            side="sell",
            asset="ETH",
            amount=Decimal("1.0"),
        )
    )
    assert result.success is True


# ----------------------------------------------------------------- policy deny
def test_denied_raises(engine_deny):
    engine, wm = engine_deny
    wallet_id = wm.list_for_agent("agent_1")[0].wallet_id

    with pytest.raises(ExecutionDeniedError) as exc_info:
        asyncio.get_event_loop().run_until_complete(
            engine.execute_trade("agent_1", wallet_id, "buy", "BTC", Decimal("0.01"))
        )
    assert "Trade too large" in str(exc_info.value)


# ----------------------------------------------------------------- pending
def test_pending_raises_with_request_id(engine_pending):
    engine, wm, queue = engine_pending
    wallet_id = wm.list_for_agent("agent_1")[0].wallet_id

    with pytest.raises(ExecutionPendingError) as exc_info:
        asyncio.get_event_loop().run_until_complete(
            engine.execute_trade("agent_1", wallet_id, "buy", "BTC", Decimal("0.01"))
        )
    exc = exc_info.value
    assert exc.request_id.startswith("apr_")
    pending = queue.list_pending()
    assert any(p.request_id == exc.request_id for p in pending)


def test_execute_approved_order(engine_pending):
    engine, wm, queue = engine_pending
    wallet_id = wm.list_for_agent("agent_1")[0].wallet_id

    # Create pending request
    with pytest.raises(ExecutionPendingError) as exc_info:
        asyncio.get_event_loop().run_until_complete(
            engine.execute_trade("agent_1", wallet_id, "buy", "BTC", Decimal("0.01"))
        )
    req_id = exc_info.value.request_id

    # Approve it
    queue.approve(req_id, decided_by="admin")

    # Execute the approved order
    result = asyncio.get_event_loop().run_until_complete(engine.execute_approved(req_id))
    assert result.success is True


# ----------------------------------------------------------------- balance
def test_get_balance(engine_allow):
    engine, wm = engine_allow
    wallet_id = wm.list_for_agent("agent_1")[0].wallet_id
    result = asyncio.get_event_loop().run_until_complete(engine.get_balance(wallet_id))
    assert result.wallet_id == wallet_id
    assert "USDT" in result.balances
