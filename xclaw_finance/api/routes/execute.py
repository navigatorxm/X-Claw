"""POST /execute — submit a trade for policy + risk evaluation and execution."""
from __future__ import annotations
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_execution_engine, get_wallet_manager
from auth.dependencies import get_current_agent, require_permission
from auth.models import AgentIdentity, Permission
from wallet.manager import WalletManager as _WM  # re-import alias to avoid shadowing
from execution_engine.engine import (
    ExecutionDeniedError,
    ExecutionEngine,
    ExecutionError,
    ExecutionPendingError,
)
from wallet.manager import WalletManager

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
    approval_request_id: Optional[str] = Field(default=None)
    daily_volume_usd: Decimal = Field(default=Decimal("0"), ge=0)


@router.post("")
async def execute_trade(
    body: TradeRequest,
    engine: ExecutionEngine = Depends(get_execution_engine),
    wallets: _WM = Depends(get_wallet_manager),
    caller: AgentIdentity = Depends(require_permission(Permission.EXECUTE)),
) -> dict:
    """
    Submit a trade order.

    Non-admin agents may only submit trades for their own agent_id.
    Simulation agents (simulation=True) may only trade against simulation wallets.
    """
    if not caller.is_admin() and caller.agent_id != body.agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"You may only execute trades for your own agent_id ('{caller.agent_id}').",
        )

    # Simulation agents are restricted to simulation wallets
    if caller.simulation:
        wallet = wallets.get(body.wallet_id)
        if wallet and wallet.exchange != "simulation":
            raise HTTPException(
                status_code=403,
                detail=(
                    "Simulation agents may only trade on simulation wallets "
                    f"(wallet '{body.wallet_id}' uses exchange '{wallet.exchange}')."
                ),
            )
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
        raise HTTPException(
            status_code=403,
            detail={"status": "denied", "reason": exc.reason, "source": exc.source},
        )
    except ExecutionPendingError as exc:
        return {
            "status": "pending",
            "approval_request_id": exc.request_id,
            "message": exc.reason,
            "source": exc.source,
        }
    except ExecutionError as exc:
        raise HTTPException(status_code=400, detail={"status": "error", "reason": str(exc)})


@router.get("/balance/{wallet_id}")
async def get_balance(
    wallet_id: str,
    engine: ExecutionEngine = Depends(get_execution_engine),
    wallets: WalletManager = Depends(get_wallet_manager),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """
    Fetch live balance for a wallet.
    Non-admin agents may only query wallets that belong to them.
    """
    wallet = wallets.get(wallet_id)
    if not wallet:
        raise HTTPException(status_code=404, detail=f"Wallet '{wallet_id}' not found.")
    if not caller.is_admin() and wallet.agent_id != caller.agent_id:
        raise HTTPException(
            status_code=403,
            detail="You may only query your own wallets.",
        )
    try:
        result = await engine.get_balance(wallet_id)
        return result.to_dict()
    except ExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
