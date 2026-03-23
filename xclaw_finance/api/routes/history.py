"""GET /history — audit log query endpoints."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_audit_logger
from auth.dependencies import get_current_agent, require_permission
from auth.models import AgentIdentity, Permission
from audit_logger.logger import AuditLogger

router = APIRouter(prefix="/history", tags=["history"])


@router.get("")
async def get_history(
    agent_id: Optional[str] = Query(default=None, description="Filter by agent ID"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    audit: AuditLogger = Depends(get_audit_logger),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """
    Retrieve paginated audit history.

    Non-admin agents always see only their own history regardless of the
    `agent_id` query parameter.
    """
    # Enforce scoping: non-admin can only see their own entries
    if not caller.is_admin():
        if agent_id and agent_id != caller.agent_id:
            raise HTTPException(
                status_code=403,
                detail="You may only query your own audit history.",
            )
        agent_id = caller.agent_id  # always filter to own data

    entries = audit.get_history(agent_id=agent_id, limit=limit, offset=offset)
    total = audit.count(agent_id=agent_id)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "entries": [e.to_dict() for e in entries],
    }


@router.get("/{entry_id}")
async def get_entry(
    entry_id: str,
    audit: AuditLogger = Depends(get_audit_logger),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """Get a specific audit entry. Non-admin agents may only read their own."""
    entry = audit.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Audit entry not found.")
    if not caller.is_admin() and entry.agent_id != caller.agent_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    return entry.to_dict()
