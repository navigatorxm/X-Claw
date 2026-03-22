"""
Execution engine — orchestrates the full lifecycle of a financial action.

Flow:
  1. Validate inputs
  2. Evaluate policy  (PolicyEngine)
  3a. DENY  → raise ExecutionDeniedError, log to audit
  3b. REQUIRE_APPROVAL → enqueue in ApprovalQueue, return pending result
  3c. ALLOW → build Order, route to exchange adapter, log to audit
"""
from __future__ import annotations
import uuid
from decimal import Decimal
from datetime import datetime
from typing import Optional

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


class ExecutionError(Exception):
    pass


class ExecutionDeniedError(ExecutionError):
    """Raised when policy denies the action outright."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ExecutionPendingError(ExecutionError):
    """Raised when the action requires manual approval."""
    def __init__(self, request_id: str, reason: str):
        super().__init__(f"Pending approval [{request_id}]: {reason}")
        self.request_id = request_id
        self.reason = reason


class ExecutionEngine:
    """
    Unified entry point for all financial actions.

    Supports multiple exchange adapters — register by exchange_id.
    All actions pass through policy evaluation + approval before execution.
    """

    def __init__(
        self,
        wallet_manager: WalletManager,
        policy_engine: PolicyEngine,
        approval_queue: ApprovalQueue,
        audit_logger: AuditLogger,
    ) -> None:
        self._wallets = wallet_manager
        self._policy = policy_engine
        self._approvals = approval_queue
        self._audit = audit_logger
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

        If `approval_request_id` is supplied the engine skips policy evaluation
        (the operator has already approved it).
        """
        wallet = self._wallets.get(wallet_id)
        if not wallet:
            raise ExecutionError(f"Wallet '{wallet_id}' not found.")
        if not wallet.is_active():
            raise ExecutionError(f"Wallet '{wallet_id}' is not active ({wallet.status.value}).")

        adapter = self._get_adapter(wallet.exchange)

        # Resolve USD value for policy checks
        symbol = f"{asset.upper()}{quote.upper()}"
        try:
            price = await adapter.get_price(symbol)
        except (ValueError, KeyError) as exc:
            raise ExecutionError(f"Unknown symbol '{symbol}' on {wallet.exchange}: {exc}") from exc
        amount_usd = (amount * price).quantize(Decimal("0.01"))

        # ---------------------------------------- policy check (skip if pre-approved)
        if not approval_request_id:
            ctx = ActionContext(
                agent_id=agent_id,
                action=side,
                asset=asset.upper(),
                amount_usd=amount_usd,
                exchange=wallet.exchange,
                wallet_id=wallet_id,
                daily_volume_usd=daily_volume_usd,
            )
            eval_result = self._policy.evaluate(ctx)

            if eval_result.decision == PolicyDecision.DENY:
                self._audit.log(
                    agent_id=agent_id,
                    action=f"{side}:{asset}",
                    policy_decision=eval_result.decision.value,
                    approval_chain=None,
                    execution_result=None,
                    metadata={"reason": eval_result.reason, "amount_usd": str(amount_usd)},
                )
                raise ExecutionDeniedError(eval_result.reason)

            if eval_result.decision == PolicyDecision.REQUIRE_APPROVAL:
                req = self._approvals.enqueue(
                    agent_id=agent_id,
                    wallet_id=wallet_id,
                    action=side,
                    asset=asset.upper(),
                    amount_usd=amount_usd,
                    exchange=wallet.exchange,
                    policy_id=eval_result.policy_id,
                    policy_reason=eval_result.reason,
                    metadata={"order_type": order_type, "limit_price": str(limit_price)},
                )
                self._audit.log(
                    agent_id=agent_id,
                    action=f"{side}:{asset}",
                    policy_decision=eval_result.decision.value,
                    approval_chain=req.request_id,
                    execution_result=None,
                    metadata={"amount_usd": str(amount_usd), "reason": eval_result.reason},
                )
                raise ExecutionPendingError(req.request_id, eval_result.reason)

        # ---------------------------------------- build order
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

        # ---------------------------------------- execute
        result = await adapter.place_order(order, wallet.api_key, wallet.api_secret)

        # ---------------------------------------- update wallet balances post-fill
        if result.success:
            bal_result = await adapter.get_balance(wallet_id, wallet.api_key, wallet.api_secret)
            from wallet.models import Balance
            new_bals = {
                k: Balance(asset=k, available=Decimal(v["available"]), locked=Decimal(v["locked"]))
                for k, v in bal_result.balances.items()
            }
            self._wallets.update_balances(wallet_id, new_bals)

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
        # Reconstruct amount in base asset
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
