"""
SimulationAdapter — persistent virtual balances, static price feed.

Registered under exchange_id="simulation". Any wallet whose exchange field
equals "simulation" is automatically routed through this adapter.

Properties:
- Virtual balances persist across process restarts (SQLite-backed).
- Prices are static — no network calls, deterministic fills.
- Orders always fill instantly with 0.1 % simulated slippage + fee.
- Balances can be reset to their initial seeded values at any time.
- Trades tagged "simulation" in the audit log by the execution engine.
"""
from __future__ import annotations
import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from execution_engine.adapters.base import ExchangeAdapter
from execution_engine.models import BalanceResult, ExecutionResult, Order, OrderStatus

from .models import DEFAULT_SIM_BALANCES, SIMULATION_PRICES


class SimulationAdapter(ExchangeAdapter):
    """
    Exchange adapter operating on virtual (simulated) balances.

    Virtual balance table layout (sim_balances):
        wallet_id         — FK to the real wallets table
        asset             — e.g. "BTC", "USDT"
        available         — spendable amount
        locked            — reserved for open orders (always 0 for instant fills)
        initial_available — snapshot used by reset_balances()
    """

    SLIPPAGE = Decimal("0.001")     # 0.1 %
    FEE_RATE  = Decimal("0.001")    # 0.1 % maker / taker fee

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @property
    def exchange_id(self) -> str:
        return "simulation"

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sim_balances (
                    wallet_id         TEXT NOT NULL,
                    asset             TEXT NOT NULL,
                    available         TEXT NOT NULL DEFAULT '0',
                    locked            TEXT NOT NULL DEFAULT '0',
                    initial_available TEXT NOT NULL DEFAULT '0',
                    PRIMARY KEY (wallet_id, asset)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sim_wallet ON sim_balances(wallet_id)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ seed / reset
    def seed_balances(
        self,
        wallet_id: str,
        balances: Optional[dict[str, Decimal]] = None,
    ) -> None:
        """
        Seed a new simulation wallet with starting virtual balances.

        Idempotent — rows that already exist are left untouched, so calling
        seed_balances twice on the same wallet_id is safe.
        """
        starting = balances if balances is not None else DEFAULT_SIM_BALANCES
        with self._connect() as conn:
            for asset, amount in starting.items():
                conn.execute(
                    """INSERT OR IGNORE INTO sim_balances
                       (wallet_id, asset, available, locked, initial_available)
                       VALUES (?, ?, ?, '0', ?)""",
                    (wallet_id, asset.upper(), str(amount), str(amount)),
                )

    def reset_balances(self, wallet_id: str) -> None:
        """Restore all balances to the values they had when seed_balances() was called."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE sim_balances
                   SET available = initial_available, locked = '0'
                   WHERE wallet_id = ?""",
                (wallet_id,),
            )

    # ------------------------------------------------------------------ price
    async def get_price(self, symbol: str) -> Decimal:
        key = symbol.upper()
        if key not in SIMULATION_PRICES:
            raise ValueError(f"Unknown simulation symbol: {symbol}")
        return SIMULATION_PRICES[key]

    # ------------------------------------------------------------------ balance
    async def get_balance(
        self, wallet_id: str, api_key: str, api_secret: str
    ) -> BalanceResult:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT asset, available, locked FROM sim_balances WHERE wallet_id = ?",
                (wallet_id,),
            ).fetchall()
        balances = {
            row["asset"]: {
                "available": row["available"],
                "locked":    row["locked"],
            }
            for row in rows
        }
        return BalanceResult(
            wallet_id=wallet_id,
            exchange=self.exchange_id,
            balances=balances,
        )

    # ------------------------------------------------------------------ orders
    async def place_order(
        self, order: Order, api_key: str, api_secret: str
    ) -> ExecutionResult:
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

        with self._connect() as conn:
            def _avail(asset: str) -> Decimal:
                row = conn.execute(
                    "SELECT available FROM sim_balances WHERE wallet_id=? AND asset=?",
                    (order.wallet_id, asset),
                ).fetchone()
                return Decimal(row["available"]) if row else Decimal("0")

            if order.side.value == "buy":
                cost = order.amount * fill_price + fee
                new_quote = _avail(order.quote) - cost
                new_base  = _avail(order.asset) + order.amount
                if new_quote < Decimal("0"):
                    order.status = OrderStatus.FAILED
                    return ExecutionResult(
                        success=False, order=order,
                        exchange_order_id=None,
                        filled_amount=Decimal("0"), avg_price=Decimal("0"),
                        fee=Decimal("0"), fee_asset=order.quote,
                        error=f"Insufficient simulation balance ({order.quote}): "
                              f"need {cost}, have {_avail(order.quote)}",
                    )
                self._upsert(conn, order.wallet_id, order.quote, new_quote)
                self._upsert(conn, order.wallet_id, order.asset, new_base)
            else:
                received = order.amount * fill_price - fee
                new_base  = _avail(order.asset) - order.amount
                new_quote = _avail(order.quote) + received
                if new_base < Decimal("0"):
                    order.status = OrderStatus.FAILED
                    return ExecutionResult(
                        success=False, order=order,
                        exchange_order_id=None,
                        filled_amount=Decimal("0"), avg_price=Decimal("0"),
                        fee=Decimal("0"), fee_asset=order.quote,
                        error=f"Insufficient simulation balance ({order.asset}): "
                              f"need {order.amount}, have {_avail(order.asset)}",
                    )
                self._upsert(conn, order.wallet_id, order.asset, new_base)
                self._upsert(conn, order.wallet_id, order.quote, new_quote)

        order.status = OrderStatus.FILLED
        order.filled_at = datetime.utcnow()
        return ExecutionResult(
            success=True,
            order=order,
            exchange_order_id=f"sim_{uuid.uuid4().hex[:10]}",
            filled_amount=order.amount,
            avg_price=fill_price,
            fee=fee,
            fee_asset=order.quote,
        )

    async def cancel_order(
        self, exchange_order_id: str, symbol: str, api_key: str, api_secret: str
    ) -> bool:
        return True  # simulation: always succeeds

    # ------------------------------------------------------------------ portfolio
    def get_portfolio_value(self, wallet_id: str) -> dict:
        """
        Return each asset's balance and the estimated total portfolio value in USDT.
        Assets without a price entry are valued at 0.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT asset, available, locked FROM sim_balances WHERE wallet_id = ?",
                (wallet_id,),
            ).fetchall()

        total = Decimal("0")
        breakdown: list[dict] = []
        for row in rows:
            asset     = row["asset"]
            available = Decimal(row["available"])
            locked    = Decimal(row["locked"])
            qty       = available + locked

            if asset == "USDT":
                usd_value = qty
            else:
                price     = SIMULATION_PRICES.get(f"{asset}USDT", Decimal("0"))
                usd_value = qty * price

            total += usd_value
            breakdown.append({
                "asset":     asset,
                "available": str(available),
                "locked":    str(locked),
                "usd_value": str(usd_value.quantize(Decimal("0.01"))),
            })

        return {
            "wallet_id":       wallet_id,
            "breakdown":       sorted(breakdown, key=lambda x: x["asset"]),
            "total_usd_value": str(total.quantize(Decimal("0.01"))),
        }

    # ------------------------------------------------------------------ helpers
    def _upsert(
        self,
        conn: sqlite3.Connection,
        wallet_id: str,
        asset: str,
        amount: Decimal,
    ) -> None:
        conn.execute(
            """INSERT INTO sim_balances (wallet_id, asset, available, locked, initial_available)
               VALUES (?, ?, ?, '0', ?)
               ON CONFLICT(wallet_id, asset)
               DO UPDATE SET available = excluded.available""",
            (wallet_id, asset.upper(), str(amount), str(amount)),
        )
