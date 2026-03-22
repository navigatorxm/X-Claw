"""Audit logger domain models."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class AuditEntry:
    entry_id: str
    timestamp: datetime
    agent_id: str
    action: str                         # e.g. "buy:BTC", "balance_check", "policy_eval"
    policy_decision: str                # "allow" | "deny" | "require_approval"
    approval_chain: Optional[str]       # approval_request_id if applicable
    execution_result: Optional[dict]    # filled ExecutionResult dict or None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp.isoformat(),
            "agent_id": self.agent_id,
            "action": self.action,
            "policy_decision": self.policy_decision,
            "approval_chain": self.approval_chain,
            "execution_result": self.execution_result,
            "metadata": self.metadata,
        }
