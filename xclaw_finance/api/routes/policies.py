"""GET|POST /policies — policy CRUD."""
from __future__ import annotations
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_policy_store
from auth.dependencies import require_permission
from auth.models import AgentIdentity, Permission
from policy_engine.models import Rule, RuleType
from policy_engine.store import PolicyStore

router = APIRouter(prefix="/policies", tags=["policies"])


class RuleSchema(BaseModel):
    rule_type: str
    value: Any
    description: str = ""

    def to_rule(self) -> Rule:
        return Rule.from_dict({
            "rule_type": self.rule_type,
            "value": self.value,
            "description": self.description,
        })


class CreatePolicyRequest(BaseModel):
    agent_id: str
    name: str
    rules: list[RuleSchema]


@router.get("")
async def list_policies(
    agent_id: Optional[str] = None,
    store: PolicyStore = Depends(get_policy_store),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """List policies. Non-admin agents see only their own."""
    if not caller.is_admin():
        agent_id = caller.agent_id  # enforce scoping
    policies = store.list_for_agent(agent_id) if agent_id else store.list_all()
    return {"policies": [p.to_dict() for p in policies]}


@router.post("")
async def create_policy(
    body: CreatePolicyRequest,
    store: PolicyStore = Depends(get_policy_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Create a new policy (admin only)."""
    rules = [r.to_rule() for r in body.rules]
    policy = store.create(agent_id=body.agent_id, name=body.name, rules=rules)
    return {"message": "Policy created.", "policy": policy.to_dict()}


@router.get("/{policy_id}")
async def get_policy(
    policy_id: str,
    store: PolicyStore = Depends(get_policy_store),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    policy = store.get(policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found.")
    if not caller.is_admin() and policy.agent_id != caller.agent_id and policy.agent_id != "*":
        raise HTTPException(status_code=403, detail="Access denied.")
    return policy.to_dict()


@router.delete("/{policy_id}")
async def disable_policy(
    policy_id: str,
    store: PolicyStore = Depends(get_policy_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Disable a policy (admin only)."""
    store.disable(policy_id)
    return {"message": f"Policy '{policy_id}' disabled."}
