"""Wallet domain models."""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from datetime import datetime
from typing import Optional


class WalletStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class AssetSymbol(str, Enum):
    BTC = "BTC"
    ETH = "ETH"
    USDT = "USDT"
    USDC = "USDC"
    SOL = "SOL"
    BNB = "BNB"


@dataclass
class Balance:
    asset: str
    available: Decimal
    locked: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return self.available + self.locked

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "available": str(self.available),
            "locked": str(self.locked),
            "total": str(self.total),
        }


@dataclass
class Wallet:
    wallet_id: str
    agent_id: str
    label: str
    exchange: str
    api_key: str                    # encrypted at rest in production
    api_secret: str                 # encrypted at rest in production
    status: WalletStatus = WalletStatus.ACTIVE
    balances: dict[str, Balance] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def get_balance(self, asset: str) -> Balance:
        return self.balances.get(asset, Balance(asset=asset, available=Decimal("0")))

    def is_active(self) -> bool:
        return self.status == WalletStatus.ACTIVE

    def to_dict(self) -> dict:
        return {
            "wallet_id": self.wallet_id,
            "agent_id": self.agent_id,
            "label": self.label,
            "exchange": self.exchange,
            "status": self.status.value,
            "balances": {k: v.to_dict() for k, v in self.balances.items()},
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
