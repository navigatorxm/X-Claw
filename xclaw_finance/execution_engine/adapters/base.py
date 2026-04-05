"""Abstract exchange adapter interface."""
from __future__ import annotations
from abc import ABC, abstractmethod
from decimal import Decimal

from ..models import BalanceResult, ExecutionResult, Order


class ExchangeAdapter(ABC):
    """
    Unified interface for all exchange integrations.

    Every exchange (Binance, Coinbase, Kraken, DEX…) must implement this.
    The execution engine calls only these methods — zero exchange-specific
    logic leaks into the rest of the system.
    """

    @property
    @abstractmethod
    def exchange_id(self) -> str:
        """Unique identifier for this exchange e.g. 'binance', 'coinbase'."""
        ...

    @abstractmethod
    async def get_balance(self, wallet_id: str, api_key: str, api_secret: str) -> BalanceResult:
        """Fetch all balances for the given wallet credentials."""
        ...

    @abstractmethod
    async def place_order(self, order: Order, api_key: str, api_secret: str) -> ExecutionResult:
        """Submit an order and return the execution result."""
        ...

    @abstractmethod
    async def cancel_order(
        self, exchange_order_id: str, symbol: str, api_key: str, api_secret: str
    ) -> bool:
        """Cancel an open order. Returns True on success."""
        ...

    @abstractmethod
    async def get_price(self, symbol: str) -> Decimal:
        """Fetch current mid-market price for a symbol (e.g. 'BTCUSDT')."""
        ...
