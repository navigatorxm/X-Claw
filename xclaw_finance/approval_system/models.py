"""Approval system domain models."""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from datetime import datetime
from typing import Optional


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    AUTO_APPROVED = "auto_approved"


@dataclass
class ApprovalRequest:
    request_id: str
    agent_id: str
    wallet_id: str
    action: str                         # "buy" | "sell" | "transfer"
    asset: str
    amount_usd: Decimal
    exchange: str
    policy_id: Optional[str]
    policy_reason: str                  # why approval was required
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None    # "auto" | user/operator ID
    decision_note: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "wallet_id": self.wallet_id,
            "action": self.action,
            "asset": self.asset,
            "amount_usd": str(self.amount_usd),
            "exchange": self.exchange,
            "policy_id": self.policy_id,
            "policy_reason": self.policy_reason,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "decision_note": self.decision_note,
            "metadata": self.metadata,
        }
