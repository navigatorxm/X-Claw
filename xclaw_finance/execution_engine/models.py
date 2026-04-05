"""Execution engine domain models."""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from datetime import datetime
from typing import Optional


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    order_id: str
    agent_id: str
    wallet_id: str
    exchange: str
    side: OrderSide
    asset: str                      # base asset e.g. "BTC"
    quote: str                      # quote asset e.g. "USDT"
    amount: Decimal                 # base asset amount
    price: Optional[Decimal]        # None for MARKET orders
    order_type: OrderType = OrderType.MARKET
    status: OrderStatus = OrderStatus.PENDING
    approval_request_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    filled_at: Optional[datetime] = None

    @property
    def symbol(self) -> str:
        return f"{self.asset}{self.quote}"

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "agent_id": self.agent_id,
            "wallet_id": self.wallet_id,
            "exchange": self.exchange,
            "side": self.side.value,
            "asset": self.asset,
            "quote": self.quote,
            "symbol": self.symbol,
            "amount": str(self.amount),
            "price": str(self.price) if self.price else None,
            "order_type": self.order_type.value,
            "status": self.status.value,
            "approval_request_id": self.approval_request_id,
            "created_at": self.created_at.isoformat(),
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
        }


@dataclass
class ExecutionResult:
    success: bool
    order: Order
    exchange_order_id: Optional[str]
    filled_amount: Decimal
    avg_price: Decimal
    fee: Decimal
    fee_asset: str
    error: Optional[str] = None
    raw_response: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "order": self.order.to_dict(),
            "exchange_order_id": self.exchange_order_id,
            "filled_amount": str(self.filled_amount),
            "avg_price": str(self.avg_price),
            "fee": str(self.fee),
            "fee_asset": self.fee_asset,
            "error": self.error,
        }


@dataclass
class BalanceResult:
    wallet_id: str
    exchange: str
    balances: dict[str, dict]       # {"BTC": {"available": "0.5", "locked": "0.1"}}
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "wallet_id": self.wallet_id,
            "exchange": self.exchange,
            "balances": self.balances,
            "fetched_at": self.fetched_at.isoformat(),
        }
