"""
POST /auth/agents         — register a new agent identity (open if no agents exist)
GET  /auth/agents         — list all agents             (admin)
GET  /auth/agents/me      — show caller's own identity  (any authenticated agent)
POST /auth/agents/{id}/rotate  — rotate API key         (own agent or admin)
PATCH /auth/agents/{id}/role   — update role            (admin)
POST /auth/agents/{id}/revoke  — deactivate agent       (admin)
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from auth.dependencies import _get_agent_store, get_current_agent, require_permission
from auth.models import AgentIdentity, Permission, Role
from auth.store import AgentStore

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterAgentRequest(BaseModel):
    agent_id: str
    role: str = "trader"                    # admin | trader | approver | readonly
    custom_permissions: Optional[list[str]] = None
    simulation: bool = False                # True → agent may only use simulation wallets


class UpdateRoleRequest(BaseModel):
    role: str
    custom_permissions: Optional[list[str]] = None


# ─────────────────────────────────────────────────────────── register
@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def register_agent(
    body: RegisterAgentRequest,
    store: AgentStore = Depends(_get_agent_store),
    raw_key: Optional[str] = None,          # resolved below via conditional dep
) -> dict:
    """
    Register a new agent and return its API key (shown ONCE — store it securely).

    Open (no auth required) when the system has zero agents — this creates the
    initial admin. All subsequent registrations require an existing admin key.
    """
    # Bootstrap: first agent may be registered without auth
    if store.count() > 0:
        # Must be admin — check header manually here since we need conditional auth
        from fastapi import Request
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Use the authenticated /auth/agents endpoint. "
                   "This codepath should not be reached.",
        )

    return await _do_register(body, store)


@router.post("/agents/register", status_code=status.HTTP_201_CREATED)
async def register_agent_authenticated(
    body: RegisterAgentRequest,
    store: AgentStore = Depends(_get_agent_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Register a new agent — requires admin key."""
    return await _do_register(body, store)


async def _do_register(body: RegisterAgentRequest, store: AgentStore) -> dict:
    try:
        role = Role(body.role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown role '{body.role}'. Valid: {[r.value for r in Role]}.",
        )

    custom_perms = None
    if body.custom_permissions:
        try:
            custom_perms = frozenset(Permission(p) for p in body.custom_permissions)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        identity, raw_key = store.register(body.agent_id, role, custom_perms, body.simulation)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    return {
        "agent_id": identity.agent_id,
        "role": identity.role.value,
        "permissions": sorted(p.value for p in identity.permissions),
        "api_key": raw_key,             # ← shown ONCE; not stored server-side
        "key_prefix": identity.key_prefix,
        "message": "Agent registered. Store the api_key securely — it cannot be retrieved again.",
    }


# ─────────────────────────────────────────────────────────── list / me
@router.get("/agents")
async def list_agents(
    store: AgentStore = Depends(_get_agent_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """List all registered agents (admin only)."""
    agents = store.list_all()
    return {"count": len(agents), "agents": [a.to_dict() for a in agents]}


@router.get("/agents/me")
async def get_me(
    caller: AgentIdentity = Depends(get_current_agent),
) -> dict:
    """Return the caller's own identity."""
    return caller.to_dict()


# ─────────────────────────────────────────────────────────── rotate key
@router.post("/agents/{agent_id}/rotate")
async def rotate_key(
    agent_id: str,
    store: AgentStore = Depends(_get_agent_store),
    caller: AgentIdentity = Depends(get_current_agent),
) -> dict:
    """
    Rotate an agent's API key. Returns the new key (shown ONCE).
    Non-admin agents may only rotate their own key.
    """
    if not caller.is_admin() and caller.agent_id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You may only rotate your own API key.",
        )
    try:
        identity, raw_key = store.rotate_key(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    return {
        "agent_id": agent_id,
        "api_key": raw_key,
        "key_prefix": identity.key_prefix,
        "message": "API key rotated. Store the new key securely.",
    }


# ─────────────────────────────────────────────────────────── update role
@router.patch("/agents/{agent_id}/role")
async def update_role(
    agent_id: str,
    body: UpdateRoleRequest,
    store: AgentStore = Depends(_get_agent_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Update an agent's role and permissions (admin only)."""
    try:
        role = Role(body.role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown role '{body.role}'.",
        )
    custom_perms = None
    if body.custom_permissions:
        try:
            custom_perms = frozenset(Permission(p) for p in body.custom_permissions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    try:
        identity = store.update_role(agent_id, role, custom_perms)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"message": "Role updated.", "agent": identity.to_dict()}


# ─────────────────────────────────────────────────────────── revoke
@router.post("/agents/{agent_id}/revoke")
async def revoke_agent(
    agent_id: str,
    store: AgentStore = Depends(_get_agent_store),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Deactivate an agent — their key will no longer authenticate (admin only)."""
    if store.get(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    store.revoke(agent_id)
    return {"message": f"Agent '{agent_id}' revoked."}
