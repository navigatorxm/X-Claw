"""GET /history — audit log query endpoints."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, Query

from api.deps import get_audit_logger
from audit_logger.logger import AuditLogger

router = APIRouter(prefix="/history", tags=["history"])


@router.get("")
async def get_history(
    agent_id: Optional[str] = Query(default=None, description="Filter by agent ID"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    audit: AuditLogger = Depends(get_audit_logger),
) -> dict:
    """Retrieve paginated audit history, optionally filtered by agent."""
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
) -> dict:
    entry = audit.get_entry(entry_id)
    if not entry:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Audit entry not found.")
    return entry.to_dict()
