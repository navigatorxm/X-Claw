"""POST /execute — submit a trade for policy evaluation and execution."""
from __future__ import annotations
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_execution_engine
from execution_engine.engine import (
    ExecutionDeniedError,
    ExecutionEngine,
    ExecutionError,
    ExecutionPendingError,
)

router = APIRouter(prefix="/execute", tags=["execution"])


class TradeRequest(BaseModel):
    agent_id: str
    wallet_id: str
    side: str = Field(..., pattern="^(buy|sell)$")
    asset: str = Field(..., description="Base asset symbol e.g. 'BTC'")
    amount: Decimal = Field(..., gt=0, description="Base asset quantity")
    quote: str = Field(default="USDT")
    order_type: str = Field(default="market", pattern="^(market|limit)$")
    limit_price: Optional[Decimal] = Field(default=None, gt=0)
    approval_request_id: Optional[str] = Field(
        default=None,
        description="Supply this to execute a pre-approved request",
    )
    daily_volume_usd: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="USD volume already traded today (for daily limit checks)",
    )


@router.post("")
async def execute_trade(
    body: TradeRequest,
    engine: ExecutionEngine = Depends(get_execution_engine),
) -> dict:
    """
    Submit a trade order.

    Outcomes:
    - `status: executed`  — trade filled, result returned
    - `status: pending`   — approval required; approval_request_id returned
    - `status: denied`    — policy blocked the action
    """
    try:
        result = await engine.execute_trade(
            agent_id=body.agent_id,
            wallet_id=body.wallet_id,
            side=body.side,
            asset=body.asset,
            amount=body.amount,
            quote=body.quote,
            order_type=body.order_type,
            limit_price=body.limit_price,
            approval_request_id=body.approval_request_id,
            daily_volume_usd=body.daily_volume_usd,
        )
        return {"status": "executed", "result": result.to_dict()}

    except ExecutionDeniedError as exc:
        raise HTTPException(status_code=403, detail={"status": "denied", "reason": exc.reason})

    except ExecutionPendingError as exc:
        return {
            "status": "pending",
            "approval_request_id": exc.request_id,
            "message": exc.reason,
        }

    except ExecutionError as exc:
        raise HTTPException(status_code=400, detail={"status": "error", "reason": str(exc)})


@router.get("/balance/{wallet_id}")
async def get_balance(
    wallet_id: str,
    engine: ExecutionEngine = Depends(get_execution_engine),
) -> dict:
    """Fetch live balance for a wallet."""
    try:
        result = await engine.get_balance(wallet_id)
        return result.to_dict()
    except ExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
