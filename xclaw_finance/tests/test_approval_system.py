"""Tests for the approval system."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import tempfile
from decimal import Decimal

from approval_system.models import ApprovalStatus
from approval_system.queue import ApprovalQueue


@pytest.fixture
def queue(tmp_path):
    return ApprovalQueue(db_path=str(tmp_path / "test.db"))


def _enqueue(queue: ApprovalQueue, **kwargs) -> object:
    defaults = dict(
        agent_id="agent_1",
        wallet_id="w_1",
        action="buy",
        asset="BTC",
        amount_usd=Decimal("1000"),
        exchange="mock",
        policy_id="p_1",
        policy_reason="Over threshold",
    )
    defaults.update(kwargs)
    return queue.enqueue(**defaults)


def test_enqueue_creates_pending(queue):
    req = _enqueue(queue)
    assert req.status == ApprovalStatus.PENDING
    assert req.request_id.startswith("apr_")


def test_approve(queue):
    req = _enqueue(queue)
    updated = queue.approve(req.request_id, decided_by="admin", note="Looks good")
    assert updated.status == ApprovalStatus.APPROVED
    assert updated.decided_by == "admin"
    assert updated.decision_note == "Looks good"


def test_reject(queue):
    req = _enqueue(queue)
    updated = queue.reject(req.request_id, decided_by="admin", note="Too large")
    assert updated.status == ApprovalStatus.REJECTED


def test_auto_approve(queue):
    req = _enqueue(queue)
    updated = queue.auto_approve(req.request_id, note="Policy re-eval passed")
    assert updated.status == ApprovalStatus.AUTO_APPROVED
    assert updated.decided_by == "auto"


def test_double_decide_is_noop(queue):
    req = _enqueue(queue)
    queue.approve(req.request_id)
    # Second approve on already-approved should not change status
    queue.reject(req.request_id)
    final = queue.get(req.request_id)
    assert final.status == ApprovalStatus.APPROVED


def test_list_pending(queue):
    r1 = _enqueue(queue)
    r2 = _enqueue(queue)
    queue.approve(r1.request_id)
    pending = queue.list_pending()
    ids = [r.request_id for r in pending]
    assert r2.request_id in ids
    assert r1.request_id not in ids


def test_list_for_agent(queue):
    _enqueue(queue, agent_id="a1")
    _enqueue(queue, agent_id="a1")
    _enqueue(queue, agent_id="a2")
    assert len(queue.list_for_agent("a1")) == 2
    assert len(queue.list_for_agent("a2")) == 1


def test_expire_old(queue):
    req = _enqueue(queue)
    expired_count = queue.expire_old(ttl_seconds=0)  # 0s TTL expires everything
    assert expired_count >= 1
    final = queue.get(req.request_id)
    assert final.status == ApprovalStatus.EXPIRED
