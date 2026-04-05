"""Policy engine domain models."""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from datetime import datetime
from typing import Any, Optional


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class RuleType(str, Enum):
    MAX_TRADE_SIZE = "max_trade_size"        # max USD value per single trade
    ALLOWED_ASSETS = "allowed_assets"        # whitelist of tradeable symbols
    APPROVAL_THRESHOLD = "approval_threshold"  # USD amount that needs manual OK
    DAILY_LIMIT = "daily_limit"             # max USD volume per day
    ALLOWED_EXCHANGES = "allowed_exchanges"  # whitelist of exchanges
    BLOCKED_HOURS = "blocked_hours"          # UTC hours when trading is blocked


@dataclass
class Rule:
    rule_type: RuleType
    value: Any                  # list[str] | Decimal | float | list[int]
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "rule_type": self.rule_type.value,
            "value": self.value if not isinstance(self.value, Decimal) else str(self.value),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        v = d["value"]
        rt = RuleType(d["rule_type"])
        if rt in (RuleType.MAX_TRADE_SIZE, RuleType.APPROVAL_THRESHOLD, RuleType.DAILY_LIMIT):
            v = Decimal(str(v))
        return cls(rule_type=rt, value=v, description=d.get("description", ""))


@dataclass
class Policy:
    policy_id: str
    agent_id: str               # "*" means global/default
    name: str
    rules: list[Rule] = field(default_factory=list)
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)

    def get_rule(self, rule_type: RuleType) -> Optional[Rule]:
        for r in self.rules:
            if r.rule_type == rule_type:
                return r
        return None

    def to_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "agent_id": self.agent_id,
            "name": self.name,
            "rules": [r.to_dict() for r in self.rules],
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class PolicyEvalResult:
    decision: PolicyDecision
    policy_id: Optional[str]
    violated_rule: Optional[Rule]
    reason: str
    daily_used_usd: Decimal = Decimal("0")

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "policy_id": self.policy_id,
            "violated_rule": self.violated_rule.to_dict() if self.violated_rule else None,
            "reason": self.reason,
            "daily_used_usd": str(self.daily_used_usd),
        }
