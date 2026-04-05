"""Tests for the audit logger."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from audit_logger.logger import AuditLogger


@pytest.fixture
def logger(tmp_path):
    return AuditLogger(db_path=str(tmp_path / "test.db"))


def test_log_creates_entry(logger):
    entry = logger.log(
        agent_id="agent_1",
        action="buy:BTC",
        policy_decision="allow",
        approval_chain=None,
        execution_result={"success": True},
        metadata={"amount_usd": "500"},
    )
    assert entry.entry_id.startswith("aud_")
    assert entry.agent_id == "agent_1"
    assert entry.action == "buy:BTC"


def test_get_entry_roundtrip(logger):
    entry = logger.log(
        agent_id="a1", action="sell:ETH",
        policy_decision="deny", approval_chain=None,
        execution_result=None, metadata={"reason": "Too large"},
    )
    fetched = logger.get_entry(entry.entry_id)
    assert fetched is not None
    assert fetched.entry_id == entry.entry_id
    assert fetched.metadata["reason"] == "Too large"


def test_history_filter_by_agent(logger):
    logger.log("a1", "buy:BTC", "allow", None, None)
    logger.log("a1", "sell:ETH", "deny", None, None)
    logger.log("a2", "buy:SOL", "allow", None, None)

    a1_entries = logger.get_history(agent_id="a1")
    a2_entries = logger.get_history(agent_id="a2")
    all_entries = logger.get_history()

    assert len(a1_entries) == 2
    assert len(a2_entries) == 1
    assert len(all_entries) == 3


def test_count(logger):
    logger.log("a1", "buy:BTC", "allow", None, None)
    logger.log("a1", "sell:ETH", "deny", None, None)
    assert logger.count() == 2
    assert logger.count(agent_id="a1") == 2
    assert logger.count(agent_id="a2") == 0


def test_pagination(logger):
    for i in range(10):
        logger.log("a1", f"action_{i}", "allow", None, None)

    page1 = logger.get_history(limit=5, offset=0)
    page2 = logger.get_history(limit=5, offset=5)
    assert len(page1) == 5
    assert len(page2) == 5
    # No overlap
    ids1 = {e.entry_id for e in page1}
    ids2 = {e.entry_id for e in page2}
    assert ids1.isdisjoint(ids2)


def test_execution_result_serialised(logger):
    exec_result = {"success": True, "order_id": "ord_123", "avg_price": "67500.00"}
    entry = logger.log("a1", "buy:BTC", "allow", "apr_xyz", exec_result)
    fetched = logger.get_entry(entry.entry_id)
    assert fetched.execution_result["order_id"] == "ord_123"
    assert fetched.approval_chain == "apr_xyz"
