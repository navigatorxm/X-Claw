"""
Risk engine — combines all guards into a single, ordered decision pipeline.

Evaluation order (first DENY wins, first REQUIRE_APPROVAL is collected):
  1. Drawdown guard    — is the agent already in breach? (hard stop)
  2. Rate limit guard  — too many trades recently?       (hard stop)
  3. Exposure guard    — would this trade breach open-exposure limits?
  4. Concentration guard — would one asset exceed the concentration cap?

Decision matrix:
  Any guard returns DENY           → final decision = DENY
  Any guard returns REQUIRE_APPROVAL (and no DENY) → final = REQUIRE_APPROVAL
  All guards return ALLOW          → final = ALLOW

The risk engine runs AFTER the policy engine and BEFORE execution.
It can block or escalate actions that policy has already allowed.
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .drawdown_guard import DrawdownGuard
from .exposure_tracker import ExposureTracker
from .models import (
    GuardType,
    RiskConfig,
    RiskContext,
    RiskDecision,
    RiskEvalResult,
    ExposureState,
)
from .rate_limit_guard import RateLimitGuard


# ─────────────────────────────────────────────── Exposure guard (inline, simple)

class _ExposureGuard:
    """
    Checks whether the proposed trade would push open exposure beyond the configured limits.

    - Above `max_open_exposure_approval_pct` → REQUIRE_APPROVAL
    - Above `max_open_exposure_pct`           → DENY
    - Above `max_single_asset_pct`            → DENY
    """

    def check(
        self,
        state: ExposureState,
        config: RiskConfig,
        action: str,
        asset: str,
        amount_usd: Decimal,
    ) -> RiskEvalResult:
        # Only buys increase exposure
        if action != "buy":
            return RiskEvalResult(RiskDecision.ALLOW, None, "Exposure guard: sell reduces exposure.")

        capital = config.total_capital_usd
        if capital <= 0:
            return RiskEvalResult(RiskDecision.ALLOW, None, "Exposure guard: capital not set, skipped.")

        projected_exposure = state.open_exposure_usd + amount_usd
        projected_pct = (projected_exposure / capital).quantize(Decimal("0.0001"))

        # ── hard cap ──────────────────────────────────────────────────
        if config.max_open_exposure_pct is not None:
            if projected_pct > config.max_open_exposure_pct:
                return RiskEvalResult(
                    decision=RiskDecision.DENY,
                    guard=GuardType.EXPOSURE,
                    reason=(
                        f"Exposure hard cap: projected exposure {projected_pct * 100:.1f}% "
                        f"> limit {config.max_open_exposure_pct * 100:.1f}% of capital ${capital}."
                    ),
                    metadata={
                        "projected_exposure_usd": str(projected_exposure),
                        "projected_pct": str(projected_pct),
                        "limit_pct": str(config.max_open_exposure_pct),
                    },
                )

        # ── approval threshold ────────────────────────────────────────
        if config.max_open_exposure_approval_pct is not None:
            if projected_pct > config.max_open_exposure_approval_pct:
                return RiskEvalResult(
                    decision=RiskDecision.REQUIRE_APPROVAL,
                    guard=GuardType.EXPOSURE,
                    reason=(
                        f"Exposure approval threshold: projected {projected_pct * 100:.1f}% "
                        f"> {config.max_open_exposure_approval_pct * 100:.1f}%. Manual review required."
                    ),
                    metadata={
                        "projected_exposure_usd": str(projected_exposure),
                        "projected_pct": str(projected_pct),
                        "approval_pct": str(config.max_open_exposure_approval_pct),
                    },
                )

        # ── concentration check ───────────────────────────────────────
        if config.max_single_asset_pct is not None:
            current_asset_usd = Decimal(
                state.positions.get(asset, {}).get("value_usd", "0")
            )
            projected_asset_usd = current_asset_usd + amount_usd
            projected_asset_pct = (projected_asset_usd / capital).quantize(Decimal("0.0001"))

            if projected_asset_pct > config.max_single_asset_pct:
                return RiskEvalResult(
                    decision=RiskDecision.DENY,
                    guard=GuardType.CONCENTRATION,
                    reason=(
                        f"Concentration limit: {asset} would reach {projected_asset_pct * 100:.1f}% "
                        f"of capital (limit: {config.max_single_asset_pct * 100:.1f}%)."
                    ),
                    metadata={
                        "asset": asset,
                        "projected_asset_pct": str(projected_asset_pct),
                        "limit_pct": str(config.max_single_asset_pct),
                    },
                )

        return RiskEvalResult(
            RiskDecision.ALLOW, None,
            f"Exposure OK: projected {projected_pct * 100:.1f}% of capital.",
            metadata={"projected_exposure_pct": str(projected_pct)},
        )


# ───────────────────────────────────────────────────── RiskConfig store

class RiskConfigStore:
    """SQLite-backed CRUD for per-agent RiskConfig."""

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS risk_configs (
                    agent_id                    TEXT PRIMARY KEY,
                    total_capital_usd           TEXT NOT NULL,
                    max_daily_drawdown_pct      TEXT,
                    max_trades_per_minute       INTEGER,
                    max_trades_per_day          INTEGER,
                    max_open_exposure_pct       TEXT,
                    max_open_exposure_approval_pct TEXT,
                    max_single_asset_pct        TEXT,
                    created_at                  TEXT NOT NULL,
                    updated_at                  TEXT NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(self, config: RiskConfig) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO risk_configs VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET
                       total_capital_usd = excluded.total_capital_usd,
                       max_daily_drawdown_pct = excluded.max_daily_drawdown_pct,
                       max_trades_per_minute = excluded.max_trades_per_minute,
                       max_trades_per_day = excluded.max_trades_per_day,
                       max_open_exposure_pct = excluded.max_open_exposure_pct,
                       max_open_exposure_approval_pct = excluded.max_open_exposure_approval_pct,
                       max_single_asset_pct = excluded.max_single_asset_pct,
                       updated_at = excluded.updated_at""",
                (
                    config.agent_id,
                    str(config.total_capital_usd),
                    str(config.max_daily_drawdown_pct) if config.max_daily_drawdown_pct is not None else None,
                    config.max_trades_per_minute,
                    config.max_trades_per_day,
                    str(config.max_open_exposure_pct) if config.max_open_exposure_pct is not None else None,
                    str(config.max_open_exposure_approval_pct) if config.max_open_exposure_approval_pct is not None else None,
                    str(config.max_single_asset_pct) if config.max_single_asset_pct is not None else None,
                    config.created_at.isoformat(),
                    now,
                ),
            )

    def get(self, agent_id: str) -> Optional[RiskConfig]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM risk_configs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._row_to_config(row) if row else None

    def list_all(self) -> list[RiskConfig]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM risk_configs").fetchall()
        return [self._row_to_config(r) for r in rows]

    def _row_to_config(self, row: sqlite3.Row) -> RiskConfig:
        def _d(v) -> Optional[Decimal]:
            return Decimal(v) if v is not None else None

        return RiskConfig(
            agent_id=row["agent_id"],
            total_capital_usd=Decimal(row["total_capital_usd"]),
            max_daily_drawdown_pct=_d(row["max_daily_drawdown_pct"]),
            max_trades_per_minute=row["max_trades_per_minute"],
            max_trades_per_day=row["max_trades_per_day"],
            max_open_exposure_pct=_d(row["max_open_exposure_pct"]),
            max_open_exposure_approval_pct=_d(row["max_open_exposure_approval_pct"]),
            max_single_asset_pct=_d(row["max_single_asset_pct"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


# ─────────────────────────────────────────────────────── RiskEngine

class RiskEngine:
    """
    Stateful risk gate that runs after policy evaluation.

    Inject into ExecutionEngine to enable dynamic risk controls that
    can block or escalate regardless of what the policy engine decided.
    """

    def __init__(
        self,
        config_store: RiskConfigStore,
        tracker: ExposureTracker,
    ) -> None:
        self._configs = config_store
        self._tracker = tracker
        self._drawdown = DrawdownGuard(tracker)
        self._rate_limit = RateLimitGuard(tracker)
        self._exposure = _ExposureGuard()

    def evaluate(self, ctx: RiskContext) -> RiskEvalResult:
        """
        Run all guards in order. Returns the first DENY immediately.
        Collects REQUIRE_APPROVAL if no DENY found.
        Returns ALLOW only if all guards pass.
        """
        config = self._configs.get(ctx.agent_id)
        if config is None:
            # No risk config — pass through (policy engine is still active)
            return RiskEvalResult(
                decision=RiskDecision.ALLOW,
                guard=None,
                reason="No risk config for this agent — risk checks skipped.",
            )

        # Sync capital to tracker so drawdown % calculations are consistent
        self._tracker.set_capital(ctx.agent_id, config.total_capital_usd)

        state = self._tracker.get_state(ctx.agent_id, config_capital=config.total_capital_usd)
        escalation: Optional[RiskEvalResult] = None

        # ── 1. Drawdown ───────────────────────────────────────────────
        result = self._drawdown.check(ctx.agent_id, config)
        if result.decision == RiskDecision.DENY:
            return result
        if result.decision == RiskDecision.REQUIRE_APPROVAL and escalation is None:
            escalation = result

        # ── 2. Rate limit ─────────────────────────────────────────────
        result = self._rate_limit.check(ctx.agent_id, config)
        if result.decision == RiskDecision.DENY:
            return result
        if result.decision == RiskDecision.REQUIRE_APPROVAL and escalation is None:
            escalation = result

        # ── 3. Exposure + concentration ───────────────────────────────
        result = self._exposure.check(state, config, ctx.action, ctx.asset, ctx.amount_usd)
        if result.decision == RiskDecision.DENY:
            return result
        if result.decision == RiskDecision.REQUIRE_APPROVAL and escalation is None:
            escalation = result

        if escalation:
            return escalation

        return RiskEvalResult(
            decision=RiskDecision.ALLOW,
            guard=None,
            reason="All risk guards passed.",
        )

    def record_execution(
        self,
        agent_id: str,
        side: str,
        asset: str,
        amount: Decimal,
        price_usd: Decimal,
    ) -> Decimal:
        """
        Call this after a successful fill to update exposure state.
        Returns realized P&L (non-zero on sells).
        """
        return self._tracker.record_fill(agent_id, side, asset, amount, price_usd)

    def get_state(self, agent_id: str) -> ExposureState:
        config = self._configs.get(agent_id)
        capital = config.total_capital_usd if config else None
        return self._tracker.get_state(agent_id, config_capital=capital)

    def get_config(self, agent_id: str) -> Optional[RiskConfig]:
        return self._configs.get(agent_id)
