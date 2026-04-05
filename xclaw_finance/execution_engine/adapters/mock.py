"""
Mock exchange adapter — deterministic behaviour for tests and demos.

Uses realistic simulated prices and slippage. No network calls.
"""
from __future__ import annotations
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from ..models import BalanceResult, ExecutionResult, Order, OrderStatus
from .base import ExchangeAdapter

# Static demo prices (USD)
_MOCK_PRICES: dict[str, Decimal] = {
    "BTCUSDT": Decimal("67500.00"),
    "ETHUSDT": Decimal("3450.00"),
    "SOLUSDT": Decimal("185.00"),
    "BNBUSDT": Decimal("590.00"),
    "USDCUSDT": Decimal("1.00"),
}

# Simulated per-wallet balances (resets per process start)
_MOCK_BALANCES: dict[str, dict[str, dict]] = {}


class MockExchangeAdapter(ExchangeAdapter):
    """
    Fully deterministic mock adapter.

    - Orders always fill instantly at mock price ± 0.1% slippage.
    - Balances start at a demo seed and update on every fill.
    - No external calls — safe for unit tests and local demos.
    """

    SLIPPAGE = Decimal("0.001")     # 0.1%
    FEE_RATE = Decimal("0.001")     # 0.1% maker/taker fee

    @property
    def exchange_id(self) -> str:
        return "mock"

    async def get_price(self, symbol: str) -> Decimal:
        key = symbol.upper()
        if key not in _MOCK_PRICES:
            raise ValueError(f"Unknown mock symbol: {symbol}")
        return _MOCK_PRICES[key]

    async def get_balance(self, wallet_id: str, api_key: str, api_secret: str) -> BalanceResult:
        if wallet_id not in _MOCK_BALANCES:
            _MOCK_BALANCES[wallet_id] = {
                "USDT": {"available": "10000.00", "locked": "0.00"},
                "BTC":  {"available": "0.5",      "locked": "0.00"},
                "ETH":  {"available": "5.0",       "locked": "0.00"},
            }
        return BalanceResult(
            wallet_id=wallet_id,
            exchange=self.exchange_id,
            balances=_MOCK_BALANCES[wallet_id],
        )

    async def place_order(self, order: Order, api_key: str, api_secret: str) -> ExecutionResult:
        symbol = order.symbol
        try:
            mid = await self.get_price(symbol)
        except ValueError as exc:
            order.status = OrderStatus.FAILED
            return ExecutionResult(
                success=False,
                order=order,
                exchange_order_id=None,
                filled_amount=Decimal("0"),
                avg_price=Decimal("0"),
                fee=Decimal("0"),
                fee_asset=order.quote,
                error=str(exc),
            )

        # Simulate slippage
        if order.side.value == "buy":
            fill_price = mid * (1 + self.SLIPPAGE)
        else:
            fill_price = mid * (1 - self.SLIPPAGE)

        fill_price = fill_price.quantize(Decimal("0.01"))
        fee = (order.amount * fill_price * self.FEE_RATE).quantize(Decimal("0.01"))

        # Update mock balances
        bal = _MOCK_BALANCES.setdefault(
            order.wallet_id,
            {"USDT": {"available": "10000.00", "locked": "0.00"}},
        )
        if order.side.value == "buy":
            cost = order.amount * fill_price + fee
            usdt_bal = Decimal(bal.get("USDT", {}).get("available", "0")) - cost
            asset_bal = Decimal(bal.get(order.asset, {}).get("available", "0")) + order.amount
            bal["USDT"] = {"available": str(usdt_bal), "locked": "0.00"}
            bal[order.asset] = {"available": str(asset_bal), "locked": "0.00"}
        else:
            usdt_received = order.amount * fill_price - fee
            usdt_bal = Decimal(bal.get("USDT", {}).get("available", "0")) + usdt_received
            asset_bal = Decimal(bal.get(order.asset, {}).get("available", "0")) - order.amount
            bal["USDT"] = {"available": str(usdt_bal), "locked": "0.00"}
            bal[order.asset] = {"available": str(asset_bal), "locked": "0.00"}

        order.status = OrderStatus.FILLED
        order.filled_at = datetime.utcnow()

        return ExecutionResult(
            success=True,
            order=order,
            exchange_order_id=f"mock_{uuid.uuid4().hex[:10]}",
            filled_amount=order.amount,
            avg_price=fill_price,
            fee=fee,
            fee_asset=order.quote,
        )

    async def cancel_order(
        self, exchange_order_id: str, symbol: str, api_key: str, api_secret: str
    ) -> bool:
        # Mock: always succeeds
        return True
