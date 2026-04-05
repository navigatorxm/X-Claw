"""
Analytics API endpoints.

GET /analytics/pnl/{agent_id}     — realized P&L and volume per asset
GET /analytics/metrics/{agent_id} — trade counts, rates, timing, volume

Both endpoints require Permission.READ.
Non-admin agents may only query their own agent_id.
All results are read-only projections — no side effects.

Query parameters (both endpoints):
  start  ISO-8601 datetime string (inclusive lower bound)
  end    ISO-8601 datetime string (inclusive upper bound)

Additional for /pnl:
  asset  Filter to a single asset (e.g. "BTC")
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from analytics.metrics_aggregator import MetricsAggregator
from analytics.pnl_tracker import PnLTracker
from api.deps import get_metrics_aggregator, get_pnl_tracker
from auth.dependencies import require_permission
from auth.models import AgentIdentity, Permission

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/pnl/{agent_id}")
async def get_pnl(
    agent_id: str,
    asset:  Optional[str] = Query(default=None, description="Filter to a single asset, e.g. BTC"),
    start:  Optional[str] = Query(default=None, description="ISO-8601 start datetime (inclusive)"),
    end:    Optional[str] = Query(default=None, description="ISO-8601 end datetime (inclusive)"),
    tracker: PnLTracker  = Depends(get_pnl_tracker),
    caller: AgentIdentity = Depends(require_permission(Permission.READ)),
) -> dict:
    """
    Realized P&L and open-position breakdown for an agent.

    Returns per-asset breakdown with:
    - realized_pnl — closed profit/loss from sell fills
    - volume_usd   — total notional traded
    - open_amount, avg_cost_usd, open_value_usd — current position

    Note: P&L data is only populated when the risk engine is enabled
    (ExposureTracker.record_fill is called after each successful trade).
    """
    if not caller.is_admin() and caller.agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="You may only query your own P&L.",
        )
    report = tracker.get_pnl(agent_id, asset=asset, start=start, end=end)
    return report.to_dict()


@router.get("/pnl/{agent_id}/fills")
async def get_fills(
    agent_id: str,
    asset:  Optional[str] = Query(default=None),
    start:  Optional[str] = Query(default=None),
    end:    Optional[str] = Query(default=None),
    limit:  int           = Query(default=100, ge=1, le=1000),
    tracker: PnLTracker   = Depends(get_pnl_tracker),
    caller: AgentIdentity  = Depends(require_permission(Permission.READ)),
) -> dict:
    """
    Raw fill history for an agent (most-recent first).
    Useful for auditing or charting cumulative P&L over time.
    """
    if not caller.is_admin() and caller.agent_id != agent_id:
        raise HTTPException(status_code=403, detail="You may only query your own fills.")
    fills = tracker.get_fills(agent_id, asset=asset, start=start, end=end, limit=limit)
    return {"agent_id": agent_id, "count": len(fills), "fills": fills}


@router.get("/metrics/{agent_id}")
async def get_metrics(
    agent_id: str,
    start:  Optional[str] = Query(default=None, description="ISO-8601 start datetime (inclusive)"),
    end:    Optional[str] = Query(default=None, description="ISO-8601 end datetime (inclusive)"),
    aggregator: MetricsAggregator = Depends(get_metrics_aggregator),
    caller: AgentIdentity          = Depends(require_permission(Permission.READ)),
) -> dict:
    """
    Execution and behavioral metrics for an agent.

    Returns:
    - total_actions         — all audit entries in the window
    - trades_executed       — successfully filled orders
    - trades_denied         — policy or risk denials
    - trades_pending        — approval escalations
    - denial_rate           — denied / total_actions
    - approval_required_rate — pending / total_actions
    - avg_execution_time_ms — mean order fill latency
    - total_volume_usd      — notional traded (from risk engine fills)
    - simulation_trades     — trades on simulation wallets
    - real_trades           — trades on real exchange wallets
    """
    if not caller.is_admin() and caller.agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="You may only query your own metrics.",
        )
    metrics = aggregator.get_metrics(agent_id, start=start, end=end)
    return metrics.to_dict()
