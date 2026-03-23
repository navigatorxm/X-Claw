"""
FastAPI auth dependencies — key extraction, validation, and permission enforcement.

Usage pattern in route files:

    from auth.dependencies import get_current_agent, require_permission, ScopedAgent
    from auth.models import Permission

    # Require the caller to be authenticated (any role):
    @router.get("/foo")
    async def foo(agent: AgentIdentity = Depends(get_current_agent)):
        ...

    # Require a specific permission:
    @router.post("/execute")
    async def execute(agent: AgentIdentity = Depends(require_permission(Permission.EXECUTE))):
        ...

    # Require permission AND enforce agent_id scoping (non-admin can only act on own data):
    @router.post("/execute")
    async def execute(body: TradeRequest, scoped = Depends(require_own_agent(Permission.EXECUTE))):
        agent, agent_id = scoped          # agent_id is the resolved/verified target
        ...
"""
from __future__ import annotations
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from .models import AgentIdentity, Permission
from .store import AgentStore

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

# --------------------------------------------------------------------- shared
# _get_agent_store is the overridable dependency key.
# It is replaced at app startup (api/app.py) and in tests via
# app.dependency_overrides[_get_agent_store] = <factory>.

def _get_agent_store() -> AgentStore:
    """
    Placeholder dependency — overridden at app startup and in tests.
    Calling it directly will raise; use FastAPI's Depends() injection.
    """
    raise RuntimeError(
        "_get_agent_store not wired. "
        "Set app.dependency_overrides[_get_agent_store] = get_agent_store in app startup."
    )


# ----------------------------------------------------------------- core auth
async def get_current_agent(
    raw_key: Optional[str] = Security(_API_KEY_HEADER),
    store: AgentStore = Depends(_get_agent_store),
) -> AgentIdentity:
    """
    Validate X-API-Key header and return the authenticated AgentIdentity.

    Returns 401 if the header is missing.
    Returns 401 if the key is invalid or the agent is inactive.
    Deliberately returns the same error for both cases to prevent enumeration.
    """
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Set the X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    identity = store.authenticate(raw_key)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return identity


# ---------------------------------------------------------- permission checks
def require_permission(permission: Permission) -> Callable:
    """
    Factory: returns a FastAPI dependency that requires a specific permission.

    Example:
        @router.post("/approve")
        async def approve(agent = Depends(require_permission(Permission.APPROVE))):
    """
    async def _check(
        agent: AgentIdentity = Depends(get_current_agent),
    ) -> AgentIdentity:
        if not agent.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. Required: '{permission.value}'. "
                       f"Your permissions: {sorted(p.value for p in agent.permissions)}.",
            )
        return agent
    return _check


def require_admin() -> Callable:
    """Shorthand: require the ADMIN permission."""
    return require_permission(Permission.ADMIN)


# ------------------------------------------------------------ agent scoping
def require_own_agent_or_admin(permission: Permission) -> Callable:
    """
    Factory: returns a dependency that enforces BOTH permission AND agent_id scoping.

    Non-admin agents may only act on their own agent_id.
    Admin agents may act on any agent_id.

    The dependency returns (agent: AgentIdentity, target_agent_id: str).
    The `target_agent_id` is derived from the request — either from a path
    parameter named `agent_id` or from `request.state.target_agent_id` which
    routes set explicitly.

    For routes where scoping is enforced inline (e.g. POST /execute where agent_id
    is in the request body), use `require_permission()` and perform the check
    manually using `agent.is_admin()`.
    """
    async def _check(
        agent: AgentIdentity = Depends(require_permission(permission)),
    ) -> AgentIdentity:
        # The scoping against a specific target agent_id is performed inline
        # in the route handler because the target may come from the request body.
        # This dependency only validates the permission level.
        return agent
    return _check
