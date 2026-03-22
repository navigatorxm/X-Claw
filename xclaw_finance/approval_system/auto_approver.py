"""Auto-approver — applies policy rules to auto-resolve pending requests."""
from __future__ import annotations
from decimal import Decimal
from typing import Optional

from policy_engine.engine import ActionContext, PolicyEngine
from policy_engine.models import PolicyDecision, RuleType

from .models import ApprovalRequest, ApprovalStatus
from .queue import ApprovalQueue


class AutoApprover:
    """
    Resolves pending approval requests automatically when policy allows it.

    Used when a new request arrives that was escalated purely due to an
    APPROVAL_THRESHOLD rule — not a hard DENY.  In those cases we check
    whether a lower threshold (or no threshold) now applies.

    Also used to sweep the queue and clear obviously-auto-approvable items.
    """

    def __init__(self, queue: ApprovalQueue, policy_engine: PolicyEngine) -> None:
        self._queue = queue
        self._policy_engine = policy_engine

    def try_auto_approve(
        self,
        request: ApprovalRequest,
        daily_volume_usd: Decimal = Decimal("0"),
    ) -> bool:
        """
        Attempt to auto-approve a request.
        Returns True if the request was auto-approved.
        """
        if request.status != ApprovalStatus.PENDING:
            return False

        ctx = ActionContext(
            agent_id=request.agent_id,
            action=request.action,
            asset=request.asset,
            amount_usd=request.amount_usd,
            exchange=request.exchange,
            wallet_id=request.wallet_id,
            daily_volume_usd=daily_volume_usd,
        )
        result = self._policy_engine.evaluate(ctx)

        if result.decision == PolicyDecision.ALLOW:
            self._queue.auto_approve(
                request.request_id,
                note=f"Policy re-evaluation passed: {result.reason}",
            )
            return True

        return False

    def sweep_pending(self, daily_volume_usd: Decimal = Decimal("0")) -> list[str]:
        """
        Sweep all pending requests and auto-approve any that now pass policy.
        Returns list of auto-approved request IDs.
        """
        approved: list[str] = []
        for req in self._queue.list_pending():
            if self.try_auto_approve(req, daily_volume_usd):
                approved.append(req.request_id)
        return approved
