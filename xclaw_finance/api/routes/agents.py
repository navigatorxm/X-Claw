"""POST /agent/register — register an agent and provision its wallet."""
from __future__ import annotations
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_wallet_manager
from wallet.manager import WalletManager

router = APIRouter(prefix="/agent", tags=["agents"])


class RegisterAgentRequest(BaseModel):
    agent_id: str = Field(..., description="Unique agent identifier")
    label: str = Field(..., description="Human-readable wallet label")
    exchange: str = Field(..., description="Exchange identifier: 'mock' | 'binance'")
    api_key: str = Field(..., description="Exchange API key")
    api_secret: str = Field(..., description="Exchange API secret")


class RegisterAgentResponse(BaseModel):
    agent_id: str
    wallet_id: str
    exchange: str
    label: str
    message: str


@router.post("/register", response_model=RegisterAgentResponse)
async def register_agent(
    body: RegisterAgentRequest,
    wallets: WalletManager = Depends(get_wallet_manager),
) -> RegisterAgentResponse:
    """Register a new agent and provision a wallet for it."""
    existing = wallets.list_for_agent(body.agent_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{body.agent_id}' already has {len(existing)} wallet(s). "
                   "Use GET /agent/{agent_id}/wallets to list them.",
        )

    wallet = wallets.register(
        agent_id=body.agent_id,
        label=body.label,
        exchange=body.exchange,
        api_key=body.api_key,
        api_secret=body.api_secret,
    )
    return RegisterAgentResponse(
        agent_id=body.agent_id,
        wallet_id=wallet.wallet_id,
        exchange=wallet.exchange,
        label=wallet.label,
        message="Agent registered successfully.",
    )


@router.get("/{agent_id}/wallets")
async def list_wallets(
    agent_id: str,
    wallets: WalletManager = Depends(get_wallet_manager),
) -> dict:
    ws = wallets.list_for_agent(agent_id)
    return {"agent_id": agent_id, "wallets": [w.to_dict() for w in ws]}
