"""POST /approve — approve or reject pending execution requests."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_approval_queue, get_execution_engine
from approval_system.queue import ApprovalQueue
from execution_engine.engine import ExecutionEngine, ExecutionError

router = APIRouter(prefix="/approve", tags=["approvals"])


class DecisionRequest(BaseModel):
    request_id: str
    decision: str                   # "approve" | "reject"
    decided_by: str = "operator"
    note: str = ""
    execute_immediately: bool = True  # if approved, run the trade right away


@router.post("")
async def decide(
    body: DecisionRequest,
    queue: ApprovalQueue = Depends(get_approval_queue),
    engine: ExecutionEngine = Depends(get_execution_engine),
) -> dict:
    """Approve or reject a pending approval request."""
    req = queue.get(body.request_id)
    if not req:
        raise HTTPException(status_code=404, detail=f"Request '{body.request_id}' not found.")
    if req.status.value != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Request is not pending (current status: {req.status.value}).",
        )

    if body.decision == "approve":
        updated = queue.approve(body.request_id, decided_by=body.decided_by, note=body.note)
        response: dict = {"status": "approved", "request": updated.to_dict() if updated else None}

        if body.execute_immediately:
            try:
                result = await engine.execute_approved(body.request_id)
                response["execution"] = result.to_dict()
            except ExecutionError as exc:
                response["execution_error"] = str(exc)

    elif body.decision == "reject":
        updated = queue.reject(body.request_id, decided_by=body.decided_by, note=body.note)
        response = {"status": "rejected", "request": updated.to_dict() if updated else None}

    else:
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'.")

    return response


@router.get("/pending")
async def list_pending(queue: ApprovalQueue = Depends(get_approval_queue)) -> dict:
    """List all pending approval requests."""
    pending = queue.list_pending()
    return {"count": len(pending), "requests": [r.to_dict() for r in pending]}


@router.get("/{request_id}")
async def get_request(
    request_id: str,
    queue: ApprovalQueue = Depends(get_approval_queue),
) -> dict:
    req = queue.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Approval request not found.")
    return req.to_dict()
