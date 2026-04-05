"""Policy engine — evaluates rules against an incoming action."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from .models import Policy, PolicyDecision, PolicyEvalResult, Rule, RuleType
from .store import PolicyStore


@dataclass
class ActionContext:
    """Everything the policy engine needs to decide on an action."""
    agent_id: str
    action: str                 # "buy" | "sell" | "transfer"
    asset: str                  # e.g. "BTC"
    amount_usd: Decimal         # USD equivalent of the trade
    exchange: str
    wallet_id: str
    daily_volume_usd: Decimal = Decimal("0")   # already spent today


class PolicyEngine:
    """
    Evaluates policies for a given agent + action.

    Resolution order:
      1. If any rule is violated → DENY
      2. If amount_usd >= approval_threshold → REQUIRE_APPROVAL
      3. Otherwise → ALLOW
    """

    def __init__(self, store: PolicyStore) -> None:
        self._store = store

    def evaluate(self, ctx: ActionContext) -> PolicyEvalResult:
        policies = self._store.list_for_agent(ctx.agent_id)

        if not policies:
            # No policy configured — block by default (safe default)
            return PolicyEvalResult(
                decision=PolicyDecision.DENY,
                policy_id=None,
                violated_rule=None,
                reason="No policy configured for this agent. Register a policy first.",
            )

        # Check every applicable policy; first DENY wins.
        for policy in policies:
            result = self._eval_policy(policy, ctx)
            if result.decision != PolicyDecision.ALLOW:
                return result

        # All policies passed → ALLOW
        return PolicyEvalResult(
            decision=PolicyDecision.ALLOW,
            policy_id=policies[0].policy_id,
            violated_rule=None,
            reason="All policy rules satisfied.",
            daily_used_usd=ctx.daily_volume_usd,
        )

    # ---------------------------------------------------------------- private
    def _eval_policy(self, policy: Policy, ctx: ActionContext) -> PolicyEvalResult:
        for rule in policy.rules:
            result = self._eval_rule(rule, policy, ctx)
            if result.decision != PolicyDecision.ALLOW:
                return result
        return PolicyEvalResult(
            decision=PolicyDecision.ALLOW,
            policy_id=policy.policy_id,
            violated_rule=None,
            reason=f"Policy '{policy.name}' satisfied.",
        )

    def _eval_rule(self, rule: Rule, policy: Policy, ctx: ActionContext) -> PolicyEvalResult:
        def deny(reason: str) -> PolicyEvalResult:
            return PolicyEvalResult(
                decision=PolicyDecision.DENY,
                policy_id=policy.policy_id,
                violated_rule=rule,
                reason=reason,
            )

        def escalate(reason: str) -> PolicyEvalResult:
            return PolicyEvalResult(
                decision=PolicyDecision.REQUIRE_APPROVAL,
                policy_id=policy.policy_id,
                violated_rule=rule,
                reason=reason,
            )

        match rule.rule_type:
            case RuleType.MAX_TRADE_SIZE:
                limit: Decimal = Decimal(str(rule.value))
                if ctx.amount_usd > limit:
                    return deny(
                        f"Trade size ${ctx.amount_usd} exceeds max ${limit}."
                    )

            case RuleType.ALLOWED_ASSETS:
                allowed: list[str] = [a.upper() for a in rule.value]
                if ctx.asset.upper() not in allowed:
                    return deny(
                        f"Asset '{ctx.asset}' not in allowed list: {allowed}."
                    )

            case RuleType.ALLOWED_EXCHANGES:
                allowed_ex: list[str] = [e.lower() for e in rule.value]
                if ctx.exchange.lower() not in allowed_ex:
                    return deny(
                        f"Exchange '{ctx.exchange}' not in allowed list: {allowed_ex}."
                    )

            case RuleType.APPROVAL_THRESHOLD:
                threshold: Decimal = Decimal(str(rule.value))
                if ctx.amount_usd >= threshold:
                    return escalate(
                        f"Trade ${ctx.amount_usd} >= approval threshold ${threshold}. Manual approval required."
                    )

            case RuleType.DAILY_LIMIT:
                limit_daily: Decimal = Decimal(str(rule.value))
                projected = ctx.daily_volume_usd + ctx.amount_usd
                if projected > limit_daily:
                    return deny(
                        f"Daily limit would be exceeded: ${projected} > ${limit_daily}."
                    )

            case RuleType.BLOCKED_HOURS:
                current_hour = datetime.now(timezone.utc).hour
                blocked: list[int] = rule.value
                if current_hour in blocked:
                    return deny(
                        f"Trading blocked at UTC hour {current_hour}. Blocked hours: {blocked}."
                    )

        return PolicyEvalResult(
            decision=PolicyDecision.ALLOW,
            policy_id=policy.policy_id,
            violated_rule=None,
            reason="Rule passed.",
        )
