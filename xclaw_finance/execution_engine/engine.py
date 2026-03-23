"""
Execution engine — orchestrates the full lifecycle of a financial action.

Flow:
  1. Validate wallet
  2. Fetch live price
  3. Policy check    (PolicyEngine)        ← static rules
  4. Risk check      (RiskEngine, optional) ← dynamic state-aware controls
  5. Approval gate   (ApprovalQueue)
  6. Build + submit order (ExchangeAdapter)
  7. Update balances + exposure state
  8. Audit log

Decision table:
  policy=DENY                          → ExecutionDeniedError (policy)
  policy=ALLOW,  risk=DENY             → ExecutionDeniedError (risk)
  policy=ALLOW,  risk=REQUIRE_APPROVAL → ExecutionPendingError (risk escalation)
  policy=ALLOW,  risk=ALLOW            → execute
  policy=REQUIRE_APPROVAL, risk=DENY   → ExecutionDeniedError (risk overrides)
  policy=REQUIRE_APPROVAL, risk=*      → ExecutionPendingError (policy escalation)
"""
from __future__ import annotations
import uuid
from decimal import Decimal
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from approval_system.models import ApprovalStatus
from approval_system.queue import ApprovalQueue
from audit_logger.logger import AuditLogger
from policy_engine.engine import ActionContext, PolicyEngine
from policy_engine.models import PolicyDecision
from wallet.manager import WalletManager

from .adapters.base import ExchangeAdapter
from .models import (
    BalanceResult,
    ExecutionResult,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)

if TYPE_CHECKING:
    from risk_engine.risk_engine import RiskEngine
    from risk_engine.models import RiskContext


class ExecutionError(Exception):
    pass


class ExecutionDeniedError(ExecutionError):
    """Raised when policy or risk denies the action."""
    def __init__(self, reason: str, source: str = "policy"):
        super().__init__(reason)
        self.reason = reason
        self.source = source        # "policy" | "risk"


class ExecutionPendingError(ExecutionError):
    """Raised when the action requires manual approval."""
    def __init__(self, request_id: str, reason: str, source: str = "policy"):
        super().__init__(f"Pending approval [{request_id}]: {reason}")
        self.request_id = request_id
        self.reason = reason
        self.source = source        # "policy" | "risk"


class ExecutionEngine:
    """
    Unified entry point for all financial actions.

    Supports multiple exchange adapters — register by exchange_id.
    All actions pass through policy + risk evaluation and approval before execution.
    """

    def __init__(
        self,
        wallet_manager: WalletManager,
        policy_engine: PolicyEngine,
        approval_queue: ApprovalQueue,
        audit_logger: AuditLogger,
        risk_engine: Optional["RiskEngine"] = None,
    ) -> None:
        self._wallets = wallet_manager
        self._policy = policy_engine
        self._approvals = approval_queue
        self._audit = audit_logger
        self._risk = risk_engine
        self._adapters: dict[str, ExchangeAdapter] = {}

    def register_adapter(self, adapter: ExchangeAdapter) -> None:
        self._adapters[adapter.exchange_id] = adapter

    def _get_adapter(self, exchange: str) -> ExchangeAdapter:
        adapter = self._adapters.get(exchange)
        if not adapter:
            raise ExecutionError(
                f"No adapter registered for exchange '{exchange}'. "
                f"Available: {list(self._adapters.keys())}"
            )
        return adapter

    # ----------------------------------------------------------------- public
    async def execute_trade(
        self,
        agent_id: str,
        wallet_id: str,
        side: str,                  # "buy" | "sell"
        asset: str,                 # "BTC"
        amount: Decimal,            # base asset quantity
        quote: str = "USDT",
        order_type: str = "market",
        limit_price: Optional[Decimal] = None,
        approval_request_id: Optional[str] = None,
        daily_volume_usd: Decimal = Decimal("0"),
    ) -> ExecutionResult:
        """
        Execute a buy or sell order.

        If `approval_request_id` is supplied the engine skips policy + risk evaluation
        (the operator has already reviewed and approved it).
        """
        wallet = self._wallets.get(wallet_id)
        if not wallet:
            raise ExecutionError(f"Wallet '{wallet_id}' not found.")
        if not wallet.is_active():
            raise ExecutionError(f"Wallet '{wallet_id}' is not active ({wallet.status.value}).")

        adapter = self._get_adapter(wallet.exchange)
        is_simulation = wallet.exchange == "simulation"

        # ── price fetch ───────────────────────────────────────────────
        symbol = f"{asset.upper()}{quote.upper()}"
        try:
            price = await adapter.get_price(symbol)
        except (ValueError, KeyError) as exc:
            raise ExecutionError(f"Unknown symbol '{symbol}' on {wallet.exchange}: {exc}") from exc
        amount_usd = (amount * price).quantize(Decimal("0.01"))

        # ── policy + risk gates (skipped for pre-approved orders) ─────
        if not approval_request_id:
            policy_source = "policy"
            risk_source = "risk"

            # ── 1. Policy ─────────────────────────────────────────────
            ctx = ActionContext(
                agent_id=agent_id,
                action=side,
                asset=asset.upper(),
                amount_usd=amount_usd,
                exchange=wallet.exchange,
                wallet_id=wallet_id,
                daily_volume_usd=daily_volume_usd,
            )
            policy_result = self._policy.evaluate(ctx)

            if policy_result.decision == PolicyDecision.DENY:
                self._audit.log(
                    agent_id=agent_id,
                    action=f"{side}:{asset}",
                    policy_decision=f"policy:deny",
                    approval_chain=None,
                    execution_result=None,
                    metadata={
                        "reason": policy_result.reason,
                        "amount_usd": str(amount_usd),
                        "simulation": is_simulation,
                    },
                )
                raise ExecutionDeniedError(policy_result.reason, source="policy")

            # ── 2. Risk ───────────────────────────────────────────────
            if self._risk is not None:
                from risk_engine.models import RiskContext as RC
                risk_ctx = RC(
                    agent_id=agent_id,
                    action=side,
                    asset=asset.upper(),
                    amount_usd=amount_usd,
                    wallet_id=wallet_id,
                )
                risk_result = self._risk.evaluate(risk_ctx)

                if risk_result.decision.value == "deny":
                    self._audit.log(
                        agent_id=agent_id,
                        action=f"{side}:{asset}",
                        policy_decision=f"risk:deny",
                        approval_chain=None,
                        execution_result=None,
                        metadata={
                            "reason": risk_result.reason,
                            "guard": risk_result.guard.value if risk_result.guard else None,
                            "amount_usd": str(amount_usd),
                            "simulation": is_simulation,
                        },
                    )
                    raise ExecutionDeniedError(risk_result.reason, source="risk")

                if risk_result.decision.value == "require_approval" and policy_result.decision == PolicyDecision.ALLOW:
                    # Risk escalates what policy allowed
                    req = self._approvals.enqueue(
                        agent_id=agent_id,
                        wallet_id=wallet_id,
                        action=side,
                        asset=asset.upper(),
                        amount_usd=amount_usd,
                        exchange=wallet.exchange,
                        policy_id=policy_result.policy_id,
                        policy_reason=risk_result.reason,
                        metadata={
                            "order_type": order_type,
                            "limit_price": str(limit_price),
                            "escalated_by": "risk",
                            "guard": risk_result.guard.value if risk_result.guard else None,
                            "simulation": is_simulation,
                        },
                    )
                    self._audit.log(
                        agent_id=agent_id,
                        action=f"{side}:{asset}",
                        policy_decision="risk:require_approval",
                        approval_chain=req.request_id,
                        execution_result=None,
                        metadata={
                            "amount_usd": str(amount_usd),
                            "reason": risk_result.reason,
                            "guard": risk_result.guard.value if risk_result.guard else None,
                            "simulation": is_simulation,
                        },
                    )
                    raise ExecutionPendingError(req.request_id, risk_result.reason, source="risk")

            # ── 3. Policy-level approval gate ─────────────────────────
            if policy_result.decision == PolicyDecision.REQUIRE_APPROVAL:
                req = self._approvals.enqueue(
                    agent_id=agent_id,
                    wallet_id=wallet_id,
                    action=side,
                    asset=asset.upper(),
                    amount_usd=amount_usd,
                    exchange=wallet.exchange,
                    policy_id=policy_result.policy_id,
                    policy_reason=policy_result.reason,
                    metadata={
                        "order_type": order_type,
                        "limit_price": str(limit_price),
                        "simulation": is_simulation,
                    },
                )
                self._audit.log(
                    agent_id=agent_id,
                    action=f"{side}:{asset}",
                    policy_decision="policy:require_approval",
                    approval_chain=req.request_id,
                    execution_result=None,
                    metadata={
                        "amount_usd": str(amount_usd),
                        "reason": policy_result.reason,
                        "simulation": is_simulation,
                    },
                )
                raise ExecutionPendingError(req.request_id, policy_result.reason, source="policy")

        # ── build order ───────────────────────────────────────────────
        order = Order(
            order_id=f"ord_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            wallet_id=wallet_id,
            exchange=wallet.exchange,
            side=OrderSide(side),
            asset=asset.upper(),
            quote=quote.upper(),
            amount=amount,
            price=limit_price,
            order_type=OrderType(order_type),
            approval_request_id=approval_request_id,
        )

        # ── execute ───────────────────────────────────────────────────
        result = await adapter.place_order(order, wallet.api_key, wallet.api_secret)

        # ── post-fill updates ─────────────────────────────────────────
        if result.success:
            # Wallet balances
            bal_result = await adapter.get_balance(wallet_id, wallet.api_key, wallet.api_secret)
            from wallet.models import Balance
            new_bals = {
                k: Balance(asset=k, available=Decimal(v["available"]), locked=Decimal(v["locked"]))
                for k, v in bal_result.balances.items()
            }
            self._wallets.update_balances(wallet_id, new_bals)

            # Risk exposure state
            if self._risk is not None:
                self._risk.record_execution(
                    agent_id=agent_id,
                    side=side,
                    asset=asset.upper(),
                    amount=amount,
                    price_usd=result.avg_price,
                )

        self._audit.log(
            agent_id=agent_id,
            action=f"{side}:{asset}",
            policy_decision="allow",
            approval_chain=approval_request_id,
            execution_result=result.to_dict() if result.success else None,
            metadata={
                "order_id": order.order_id,
                "amount_usd": str(amount_usd),
                "success": result.success,
                "error": result.error,
                "simulation": is_simulation,
            },
        )
        return result

    async def get_balance(self, wallet_id: str) -> BalanceResult:
        wallet = self._wallets.get(wallet_id)
        if not wallet:
            raise ExecutionError(f"Wallet '{wallet_id}' not found.")
        adapter = self._get_adapter(wallet.exchange)
        result = await adapter.get_balance(wallet_id, wallet.api_key, wallet.api_secret)
        from wallet.models import Balance
        new_bals = {
            k: Balance(asset=k, available=Decimal(v["available"]), locked=Decimal(v["locked"]))
            for k, v in result.balances.items()
        }
        self._wallets.update_balances(wallet_id, new_bals)
        return result

    async def execute_approved(self, approval_request_id: str) -> ExecutionResult:
        """Execute an already-approved request from the approval queue."""
        req = self._approvals.get(approval_request_id)
        if not req:
            raise ExecutionError(f"Approval request '{approval_request_id}' not found.")
        if req.status not in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED):
            raise ExecutionError(
                f"Request '{approval_request_id}' is not approved (status: {req.status.value})."
            )
        wallet = self._wallets.get(req.wallet_id)
        if not wallet:
            raise ExecutionError(f"Wallet '{req.wallet_id}' not found.")
        adapter = self._get_adapter(wallet.exchange)
        price = await adapter.get_price(f"{req.asset}USDT")
        base_amount = (req.amount_usd / price).quantize(Decimal("0.00001"))

        return await self.execute_trade(
            agent_id=req.agent_id,
            wallet_id=req.wallet_id,
            side=req.action,
            asset=req.asset,
            amount=base_amount,
            approval_request_id=approval_request_id,
        )
