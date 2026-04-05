"""
Simulation wallet management endpoints.

POST /simulation/wallets              — create a simulation wallet with virtual balances
GET  /simulation/wallets/{wallet_id}  — get current virtual balances
POST /simulation/wallets/{wallet_id}/reset — restore balances to initial seeded values
GET  /simulation/portfolio/{agent_id} — portfolio view with estimated USDT values

Simulation wallets are stored in the regular wallets table with exchange="simulation".
They are visible via GET /agent/{agent_id}/wallets alongside real wallets.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_sim_adapter, get_wallet_manager
from auth.dependencies import get_current_agent, require_permission
from auth.models import AgentIdentity, Permission
from simulation.adapter import SimulationAdapter
from simulation.models import DEFAULT_SIM_BALANCES
from wallet.manager import WalletManager

router = APIRouter(prefix="/simulation", tags=["simulation"])


class CreateSimWalletRequest(BaseModel):
    agent_id: str
    label: str = "Simulation Wallet"
    initial_balances: Optional[dict[str, str]] = None   # e.g. {"USDT": "50000", "BTC": "0.5"}


# ─────────────────────────────────────────────────── create simulation wallet
@router.post("/wallets", status_code=201)
async def create_sim_wallet(
    body: CreateSimWalletRequest,
    wallets: WalletManager = Depends(get_wallet_manager),
    sim: SimulationAdapter = Depends(get_sim_adapter),
    caller: AgentIdentity = Depends(require_permission(Permission.EXECUTE)),
) -> dict:
    """
    Create a simulation wallet seeded with virtual balances.

    Non-admin agents may only create wallets for themselves.
    Custom initial_balances override the defaults; omit for the standard seed
    ($100,000 USDT + 1 BTC + 10 ETH).
    """
    if not caller.is_admin() and caller.agent_id != body.agent_id:
        raise HTTPException(
            status_code=403,
            detail="You may only create simulation wallets for your own agent_id.",
        )

    # Parse custom balances if provided
    starting: Optional[dict[str, Decimal]] = None
    if body.initial_balances is not None:
        try:
            starting = {
                asset.upper(): Decimal(amount)
                for asset, amount in body.initial_balances.items()
            }
        except InvalidOperation as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid balance amount: {exc}",
            )

    # Register in the regular wallets table (exchange="simulation", no real creds)
    wallet = wallets.register(
        agent_id=body.agent_id,
        label=body.label,
        exchange="simulation",
        api_key="sim",
        api_secret="sim",
    )

    # Seed virtual balances in the simulation adapter
    sim.seed_balances(wallet.wallet_id, starting)

    # Fetch the seeded state for the response
    balance_result = await sim.get_balance(wallet.wallet_id, "sim", "sim")

    return {
        "wallet_id": wallet.wallet_id,
        "agent_id": wallet.agent_id,
        "label": wallet.label,
        "exchange": "simulation",
        "balances": balance_result.balances,
        "message": "Simulation wallet created. No real funds are involved.",
    }


# ─────────────────────────────────────────────────── get sim wallet balances
@router.get("/wallets/{wallet_id}")
async def get_sim_wallet(
    wallet_id: str,
    wallets: WalletManager = Depends(get_wallet_manager),
    sim: SimulationAdapter = Depends(get_sim_adapter),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """Return the current virtual balances for a simulation wallet."""
    wallet = wallets.get(wallet_id)
    if not wallet:
        raise HTTPException(status_code=404, detail=f"Wallet '{wallet_id}' not found.")
    if wallet.exchange != "simulation":
        raise HTTPException(
            status_code=400,
            detail=f"Wallet '{wallet_id}' is not a simulation wallet (exchange={wallet.exchange}).",
        )
    if not caller.is_admin() and wallet.agent_id != caller.agent_id:
        raise HTTPException(status_code=403, detail="You may only view your own wallets.")

    balance_result = await sim.get_balance(wallet_id, "sim", "sim")
    portfolio = sim.get_portfolio_value(wallet_id)
    return {
        "wallet_id": wallet_id,
        "agent_id": wallet.agent_id,
        "label": wallet.label,
        "exchange": "simulation",
        "balances": balance_result.balances,
        "total_usd_value": portfolio["total_usd_value"],
    }


# ─────────────────────────────────────────────────── reset balances
@router.post("/wallets/{wallet_id}/reset")
async def reset_sim_wallet(
    wallet_id: str,
    wallets: WalletManager = Depends(get_wallet_manager),
    sim: SimulationAdapter = Depends(get_sim_adapter),
    caller: AgentIdentity = Depends(require_permission(Permission.EXECUTE)),
) -> dict:
    """
    Reset a simulation wallet to its initial seeded balances.

    All virtual P&L is wiped — useful to restart a strategy test from scratch.
    Non-admin agents may only reset their own wallets.
    """
    wallet = wallets.get(wallet_id)
    if not wallet:
        raise HTTPException(status_code=404, detail=f"Wallet '{wallet_id}' not found.")
    if wallet.exchange != "simulation":
        raise HTTPException(
            status_code=400,
            detail=f"Wallet '{wallet_id}' is not a simulation wallet.",
        )
    if not caller.is_admin() and wallet.agent_id != caller.agent_id:
        raise HTTPException(status_code=403, detail="You may only reset your own wallets.")

    sim.reset_balances(wallet_id)
    balance_result = await sim.get_balance(wallet_id, "sim", "sim")
    return {
        "wallet_id": wallet_id,
        "message": "Simulation wallet reset to initial balances.",
        "balances": balance_result.balances,
    }


# ─────────────────────────────────────────────────── portfolio view
@router.get("/portfolio/{agent_id}")
async def get_sim_portfolio(
    agent_id: str,
    wallets: WalletManager = Depends(get_wallet_manager),
    sim: SimulationAdapter = Depends(get_sim_adapter),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """
    Aggregate portfolio view across all simulation wallets for an agent.

    Returns per-wallet breakdown and combined total USDT value.
    Non-admin agents may only view their own portfolio.
    """
    if not caller.is_admin() and caller.agent_id != agent_id:
        raise HTTPException(status_code=403, detail="You may only view your own portfolio.")

    all_wallets = wallets.list_for_agent(agent_id)
    sim_wallets = [w for w in all_wallets if w.exchange == "simulation"]

    portfolio_items = []
    grand_total = Decimal("0")

    for w in sim_wallets:
        pv = sim.get_portfolio_value(w.wallet_id)
        grand_total += Decimal(pv["total_usd_value"])
        portfolio_items.append({
            "wallet_id":       w.wallet_id,
            "label":           w.label,
            "breakdown":       pv["breakdown"],
            "total_usd_value": pv["total_usd_value"],
        })

    return {
        "agent_id":              agent_id,
        "simulation_wallets":    len(sim_wallets),
        "wallets":               portfolio_items,
        "grand_total_usd_value": str(grand_total.quantize(Decimal("0.01"))),
    }
