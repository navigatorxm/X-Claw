"""
Binance exchange adapter (stub).

Extend this with `python-binance` or raw HMAC-signed REST calls.
The interface is fully defined — only the implementation body needs filling.
"""
from __future__ import annotations
import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal
from typing import Optional

import httpx

from ..models import BalanceResult, ExecutionResult, Order, OrderStatus
from .base import ExchangeAdapter


class BinanceAdapter(ExchangeAdapter):
    """
    Production-ready Binance REST adapter skeleton.

    Implements the ExchangeAdapter interface. Fill in the HTTP call bodies
    to make this fully live. All Binance-specific logic stays inside this class.
    """

    BASE_URL = "https://api.binance.com"

    @property
    def exchange_id(self) -> str:
        return "binance"

    def _sign(self, params: dict, secret: str) -> str:
        query = urllib.parse.urlencode(params)
        return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    async def get_price(self, symbol: str) -> Decimal:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{self.BASE_URL}/api/v3/ticker/price",
                params={"symbol": symbol.upper()},
            )
            resp.raise_for_status()
            return Decimal(resp.json()["price"])

    async def get_balance(self, wallet_id: str, api_key: str, api_secret: str) -> BalanceResult:
        params = {"timestamp": int(time.time() * 1000)}
        params["signature"] = self._sign(params, api_secret)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.BASE_URL}/api/v3/account",
                params=params,
                headers={"X-MBX-APIKEY": api_key},
            )
            resp.raise_for_status()
            data = resp.json()

        balances = {
            b["asset"]: {"available": b["free"], "locked": b["locked"]}
            for b in data["balances"]
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        }
        return BalanceResult(wallet_id=wallet_id, exchange=self.exchange_id, balances=balances)

    async def place_order(self, order: Order, api_key: str, api_secret: str) -> ExecutionResult:
        params: dict = {
            "symbol": order.symbol.upper(),
            "side": order.side.value.upper(),
            "type": order.order_type.value.upper(),
            "quantity": str(order.amount),
            "timestamp": int(time.time() * 1000),
        }
        if order.order_type.value == "limit" and order.price:
            params["price"] = str(order.price)
            params["timeInForce"] = "GTC"

        params["signature"] = self._sign(params, api_secret)

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.BASE_URL}/api/v3/order",
                params=params,
                headers={"X-MBX-APIKEY": api_key},
            )
            if resp.status_code != 200:
                order.status = OrderStatus.FAILED
                return ExecutionResult(
                    success=False,
                    order=order,
                    exchange_order_id=None,
                    filled_amount=Decimal("0"),
                    avg_price=Decimal("0"),
                    fee=Decimal("0"),
                    fee_asset=order.quote,
                    error=resp.text,
                    raw_response=resp.json(),
                )
            data = resp.json()

        order.status = OrderStatus.FILLED
        fills = data.get("fills", [])
        avg_price = Decimal(data.get("price") or (fills[0]["price"] if fills else "0"))
        filled_qty = Decimal(data.get("executedQty", str(order.amount)))
        fee = sum(Decimal(f["commission"]) for f in fills) if fills else Decimal("0")
        fee_asset = fills[0]["commissionAsset"] if fills else order.quote

        return ExecutionResult(
            success=True,
            order=order,
            exchange_order_id=str(data["orderId"]),
            filled_amount=filled_qty,
            avg_price=avg_price,
            fee=fee,
            fee_asset=fee_asset,
            raw_response=data,
        )

    async def cancel_order(
        self, exchange_order_id: str, symbol: str, api_key: str, api_secret: str
    ) -> bool:
        params = {
            "symbol": symbol.upper(),
            "orderId": exchange_order_id,
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = self._sign(params, api_secret)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(
                f"{self.BASE_URL}/api/v3/order",
                params=params,
                headers={"X-MBX-APIKEY": api_key},
            )
        return resp.status_code == 200
