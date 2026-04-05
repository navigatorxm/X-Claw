"""Wallet manager — SQLite-backed multi-wallet CRUD."""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .models import Balance, Wallet, WalletStatus


class WalletManager:
    """Manages wallet lifecycle and balance queries for all registered agents."""

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wallets (
                    wallet_id   TEXT PRIMARY KEY,
                    agent_id    TEXT NOT NULL,
                    label       TEXT NOT NULL,
                    exchange    TEXT NOT NULL,
                    api_key     TEXT NOT NULL,
                    api_secret  TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'active',
                    balances    TEXT NOT NULL DEFAULT '{}',
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_agent ON wallets(agent_id)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ CRUD
    def register(
        self,
        agent_id: str,
        label: str,
        exchange: str,
        api_key: str,
        api_secret: str,
    ) -> Wallet:
        wallet = Wallet(
            wallet_id=f"w_{uuid.uuid4().hex[:12]}",
            agent_id=agent_id,
            label=label,
            exchange=exchange,
            api_key=api_key,
            api_secret=api_secret,
        )
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO wallets
                   (wallet_id, agent_id, label, exchange, api_key, api_secret,
                    status, balances, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    wallet.wallet_id,
                    agent_id,
                    label,
                    exchange,
                    api_key,
                    api_secret,
                    WalletStatus.ACTIVE.value,
                    "{}",
                    now,
                    now,
                ),
            )
        return wallet

    def get(self, wallet_id: str) -> Optional[Wallet]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM wallets WHERE wallet_id = ?", (wallet_id,)
            ).fetchone()
        return self._row_to_wallet(row) if row else None

    def list_for_agent(self, agent_id: str) -> list[Wallet]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wallets WHERE agent_id = ? ORDER BY created_at",
                (agent_id,),
            ).fetchall()
        return [self._row_to_wallet(r) for r in rows]

    def update_balances(self, wallet_id: str, balances: dict[str, Balance]) -> None:
        payload = {k: v.to_dict() for k, v in balances.items()}
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE wallets SET balances = ?, updated_at = ? WHERE wallet_id = ?",
                (json.dumps(payload), now, wallet_id),
            )

    def suspend(self, wallet_id: str) -> None:
        self._set_status(wallet_id, WalletStatus.SUSPENDED)

    def activate(self, wallet_id: str) -> None:
        self._set_status(wallet_id, WalletStatus.ACTIVE)

    def _set_status(self, wallet_id: str, status: WalletStatus) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE wallets SET status = ?, updated_at = ? WHERE wallet_id = ?",
                (status.value, datetime.utcnow().isoformat(), wallet_id),
            )

    # -------------------------------------------------------------- serialise
    def _row_to_wallet(self, row: sqlite3.Row) -> Wallet:
        raw_balances: dict = json.loads(row["balances"])
        balances = {
            k: Balance(
                asset=k,
                available=Decimal(v["available"]),
                locked=Decimal(v["locked"]),
            )
            for k, v in raw_balances.items()
        }
        return Wallet(
            wallet_id=row["wallet_id"],
            agent_id=row["agent_id"],
            label=row["label"],
            exchange=row["exchange"],
            api_key=row["api_key"],
            api_secret=row["api_secret"],
            status=WalletStatus(row["status"]),
            balances=balances,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
