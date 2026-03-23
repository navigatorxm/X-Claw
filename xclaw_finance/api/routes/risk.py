"""Risk engine API — config management and live state queries."""
from __future__ import annotations
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_risk_engine
from auth.dependencies import require_permission
from auth.models import AgentIdentity, Permission
from risk_engine.models import RiskConfig
from risk_engine.risk_engine import RiskEngine

router = APIRouter(prefix="/risk", tags=["risk"])


class RiskConfigRequest(BaseModel):
    agent_id: str
    total_capital_usd: Decimal = Field(..., gt=0)
    max_daily_drawdown_pct: Optional[Decimal] = Field(default=Decimal("0.05"), ge=0, le=1)
    max_trades_per_minute: Optional[int] = Field(default=10, ge=1)
    max_trades_per_day: Optional[int] = Field(default=200, ge=1)
    max_open_exposure_pct: Optional[Decimal] = Field(default=Decimal("0.80"), ge=0, le=1)
    max_open_exposure_approval_pct: Optional[Decimal] = Field(default=Decimal("0.60"), ge=0, le=1)
    max_single_asset_pct: Optional[Decimal] = Field(default=Decimal("0.40"), ge=0, le=1)


@router.post("/config")
async def set_risk_config(
    body: RiskConfigRequest,
    engine: RiskEngine = Depends(get_risk_engine),
    caller: AgentIdentity = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    """Create or update risk configuration for an agent (admin only)."""
    config = RiskConfig(
        agent_id=body.agent_id,
        total_capital_usd=body.total_capital_usd,
        max_daily_drawdown_pct=body.max_daily_drawdown_pct,
        max_trades_per_minute=body.max_trades_per_minute,
        max_trades_per_day=body.max_trades_per_day,
        max_open_exposure_pct=body.max_open_exposure_pct,
        max_open_exposure_approval_pct=body.max_open_exposure_approval_pct,
        max_single_asset_pct=body.max_single_asset_pct,
    )
    engine._configs.upsert(config)
    return {"message": "Risk config saved.", "config": config.to_dict()}


@router.get("/config/{agent_id}")
async def get_risk_config(
    agent_id: str,
    engine: RiskEngine = Depends(get_risk_engine),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """Fetch risk config. Non-admin agents may only query their own."""
    if not caller.is_admin() and caller.agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    config = engine.get_config(agent_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"No risk config for agent '{agent_id}'.")
    return config.to_dict()


@router.get("/status/{agent_id}")
async def get_risk_status(
    agent_id: str,
    engine: RiskEngine = Depends(get_risk_engine),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """Live risk state. Non-admin agents may only query their own."""
    if not caller.is_admin() and caller.agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    state = engine.get_state(agent_id)
    config = engine.get_config(agent_id)
    return {
        "state": state.to_dict(),
        "config": config.to_dict() if config else None,
    }
