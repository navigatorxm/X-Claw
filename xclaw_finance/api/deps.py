"""FastAPI dependency injection — builds and caches the platform components."""
from __future__ import annotations
from functools import lru_cache

from approval_system.queue import ApprovalQueue
from approval_system.auto_approver import AutoApprover
from audit_logger.logger import AuditLogger
from execution_engine.adapters.mock import MockExchangeAdapter
from execution_engine.engine import ExecutionEngine
from policy_engine.engine import PolicyEngine
from policy_engine.store import PolicyStore
from wallet.manager import WalletManager


DB_PATH = "memory/finance.db"


@lru_cache(maxsize=1)
def get_wallet_manager() -> WalletManager:
    return WalletManager(db_path=DB_PATH)


@lru_cache(maxsize=1)
def get_policy_store() -> PolicyStore:
    return PolicyStore(db_path=DB_PATH)


@lru_cache(maxsize=1)
def get_policy_engine() -> PolicyEngine:
    return PolicyEngine(store=get_policy_store())


@lru_cache(maxsize=1)
def get_approval_queue() -> ApprovalQueue:
    return ApprovalQueue(db_path=DB_PATH)


@lru_cache(maxsize=1)
def get_audit_logger() -> AuditLogger:
    return AuditLogger(db_path=DB_PATH)


@lru_cache(maxsize=1)
def get_execution_engine() -> ExecutionEngine:
    engine = ExecutionEngine(
        wallet_manager=get_wallet_manager(),
        policy_engine=get_policy_engine(),
        approval_queue=get_approval_queue(),
        audit_logger=get_audit_logger(),
    )
    # Register available adapters — add real ones here
    engine.register_adapter(MockExchangeAdapter())
    return engine


@lru_cache(maxsize=1)
def get_auto_approver() -> AutoApprover:
    return AutoApprover(
        queue=get_approval_queue(),
        policy_engine=get_policy_engine(),
    )
