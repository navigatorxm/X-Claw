"""Agent wallet management routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_agent_store, get_wallet_manager
from auth.dependencies import get_current_agent, require_permission
from auth.models import AgentIdentity, Permission
from auth.store import AgentStore
from wallet.manager import WalletManager

router = APIRouter(prefix="/agent", tags=["agents"])


class ProvisionWalletRequest(BaseModel):
    agent_id: str = Field(..., description="Agent ID to provision a wallet for")
    label: str = Field(..., description="Human-readable wallet label")
    exchange: str = Field(..., description="Exchange identifier: 'mock' | 'binance'")
    api_key: str = Field(..., description="Exchange API key")
    api_secret: str = Field(..., description="Exchange API secret")


@router.post("/register")
async def provision_wallet(
    body: ProvisionWalletRequest,
    wallets: WalletManager = Depends(get_wallet_manager),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """
    Provision an exchange wallet for an agent (admin only).

    The agent identity must already exist (created via POST /auth/agents).
    """
    existing = wallets.list_for_agent(body.agent_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{body.agent_id}' already has {len(existing)} wallet(s).",
        )
    wallet = wallets.register(
        agent_id=body.agent_id,
        label=body.label,
        exchange=body.exchange,
        api_key=body.api_key,
        api_secret=body.api_secret,
    )
    return {
        "agent_id": body.agent_id,
        "wallet_id": wallet.wallet_id,
        "exchange": wallet.exchange,
        "label": wallet.label,
        "message": "Wallet provisioned.",
    }


@router.get("/{agent_id}/wallets")
async def list_wallets(
    agent_id: str,
    wallets: WalletManager = Depends(get_wallet_manager),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """List wallets for an agent. Non-admin agents may only query their own."""
    if not caller.is_admin() and caller.agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="You may only list your own wallets.",
        )
    ws = wallets.list_for_agent(agent_id)
    return {"agent_id": agent_id, "wallets": [w.to_dict() for w in ws]}
