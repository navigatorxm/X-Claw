"""
XClaw Finance — end-to-end example

Demonstrates the full platform lifecycle:
  1. Register an agent + wallet
  2. Configure a policy
  3. Execute a small trade (auto-approved by policy)
  4. Execute a large trade (requires manual approval)
  5. Approve the pending request
  6. Query the audit log

Run from xclaw_finance/:
    python example_usage.py
"""
from __future__ import annotations
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

# Make modules importable when run directly
sys.path.insert(0, str(Path(__file__).parent))

from approval_system.queue import ApprovalQueue
from audit_logger.logger import AuditLogger
from execution_engine.adapters.mock import MockExchangeAdapter
from execution_engine.engine import (
    ExecutionDeniedError,
    ExecutionEngine,
    ExecutionError,
    ExecutionPendingError,
)
from policy_engine.engine import PolicyEngine
from policy_engine.models import Rule, RuleType
from policy_engine.store import PolicyStore
from wallet.manager import WalletManager

DB = "memory/example_finance.db"
sep = "─" * 60


async def main() -> None:
    # ── Setup ───────────────────────────────────────────────────
    print(f"\n{sep}")
    print("  XClaw Finance — Example Usage")
    print(sep)

    wallet_manager = WalletManager(db_path=DB)
    policy_store = PolicyStore(db_path=DB)
    policy_engine = PolicyEngine(store=policy_store)
    approval_queue = ApprovalQueue(db_path=DB)
    audit_logger = AuditLogger(db_path=DB)

    engine = ExecutionEngine(wallet_manager, policy_engine, approval_queue, audit_logger)
    engine.register_adapter(MockExchangeAdapter())

    # ── 1. Register agent ────────────────────────────────────────
    print("\n[1] Registering agent + wallet...")
    existing = wallet_manager.list_for_agent("demo_agent")
    if existing:
        wallet = existing[0]
        print(f"    ↳ Reusing wallet {wallet.wallet_id}")
    else:
        wallet = wallet_manager.register(
            agent_id="demo_agent",
            label="Demo Trading Wallet",
            exchange="mock",
            api_key="demo_key",
            api_secret="demo_secret",
        )
        print(f"    ↳ Created wallet {wallet.wallet_id}")

    # ── 2. Configure policy ──────────────────────────────────────
    print("\n[2] Configuring policy...")
    existing_policies = policy_store.list_for_agent("demo_agent")
    if existing_policies:
        policy = existing_policies[0]
        print(f"    ↳ Reusing policy {policy.policy_id}")
    else:
        policy = policy_store.create(
            agent_id="demo_agent",
            name="Demo Policy",
            rules=[
                Rule(RuleType.ALLOWED_ASSETS, ["BTC", "ETH", "SOL"],
                     description="Only major assets"),
                Rule(RuleType.MAX_TRADE_SIZE, Decimal("10000"),
                     description="Max $10k per trade"),
                Rule(RuleType.APPROVAL_THRESHOLD, Decimal("1000"),
                     description="Trades >= $1000 need approval"),
                Rule(RuleType.DAILY_LIMIT, Decimal("25000"),
                     description="Max $25k per day"),
                Rule(RuleType.ALLOWED_EXCHANGES, ["mock", "binance"],
                     description="Approved exchanges"),
            ],
        )
        print(f"    ↳ Created policy {policy.policy_id}")

    print(f"    Rules:")
    for r in policy.rules:
        print(f"      • {r.rule_type.value}: {r.value}")

    # ── 3. Small trade (auto-allowed) ────────────────────────────
    print(f"\n[3] Small BUY — $500 of BTC (should auto-allow)...")
    try:
        result = await engine.execute_trade(
            agent_id="demo_agent",
            wallet_id=wallet.wallet_id,
            side="buy",
            asset="BTC",
            amount=Decimal("0.007"),    # ≈ $472 @ mock price
        )
        print(f"    ✓ Executed — order {result.order.order_id}")
        print(f"      Filled: {result.filled_amount} BTC @ ${result.avg_price}")
        print(f"      Fee:    {result.fee} {result.fee_asset}")
    except ExecutionDeniedError as e:
        print(f"    ✗ Denied: {e.reason}")
    except ExecutionPendingError as e:
        print(f"    ⏳ Pending: {e.request_id}")

    # ── 4. Large trade (requires approval) ──────────────────────
    print(f"\n[4] Large BUY — $2000 of ETH (should require approval)...")
    pending_id: str | None = None
    try:
        result = await engine.execute_trade(
            agent_id="demo_agent",
            wallet_id=wallet.wallet_id,
            side="buy",
            asset="ETH",
            amount=Decimal("0.58"),     # ≈ $2001 @ mock price
        )
        print(f"    ✓ Executed (unexpected!)")
    except ExecutionDeniedError as e:
        print(f"    ✗ Denied: {e.reason}")
    except ExecutionPendingError as e:
        pending_id = e.request_id
        print(f"    ⏳ Pending approval: {pending_id}")
        print(f"      Reason: {e.reason}")

    # ── 5. Blocked asset ─────────────────────────────────────────
    print(f"\n[5] DOGE trade (blocked asset — should deny)...")
    try:
        await engine.execute_trade(
            agent_id="demo_agent",
            wallet_id=wallet.wallet_id,
            side="buy",
            asset="DOGE",
            amount=Decimal("10000"),
        )
    except ExecutionDeniedError as e:
        print(f"    ✓ Denied by policy: {e.reason}")
    except ExecutionError as e:
        print(f"    ✓ Blocked: {e}")

    # ── 6. Approve and execute pending ──────────────────────────
    if pending_id:
        print(f"\n[6] Approving pending request {pending_id}...")
        approval_queue.approve(pending_id, decided_by="admin", note="Reviewed and approved")
        print(f"    ✓ Approved by admin")

        print(f"    Executing approved order...")
        result = await engine.execute_approved(pending_id)
        print(f"    ✓ Executed — {result.filled_amount} ETH @ ${result.avg_price}")

    # ── 7. Balance check ─────────────────────────────────────────
    print(f"\n[7] Checking wallet balance...")
    bal = await engine.get_balance(wallet.wallet_id)
    print(f"    Wallet: {bal.wallet_id} on {bal.exchange}")
    for asset, b in bal.balances.items():
        print(f"      {asset}: {b['available']} available, {b['locked']} locked")

    # ── 8. Audit log ─────────────────────────────────────────────
    print(f"\n[8] Audit log (last 10 entries for demo_agent)...")
    entries = audit_logger.get_history(agent_id="demo_agent", limit=10)
    for e in entries:
        icon = "✓" if e.policy_decision == "allow" else ("⏳" if "approval" in e.policy_decision else "✗")
        print(f"    {icon} [{e.timestamp.strftime('%H:%M:%S')}] {e.action} → {e.policy_decision}")

    print(f"\n{sep}")
    print(f"  Total audit entries for demo_agent: {audit_logger.count('demo_agent')}")
    print(f"  Pending approvals: {len(approval_queue.list_pending())}")
    print(sep + "\n")


if __name__ == "__main__":
    asyncio.run(main())
