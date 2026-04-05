"""
Simulation mode tests.

Coverage:
 SimulationAdapter unit tests (TestSimulationAdapter)  — 13 tests
 Execution engine integration (TestSimulationExecution) — 8 tests
 HTTP API layer (TestSimulationAPI)                     — 11 tests
                                                  total   32 tests
"""
from __future__ import annotations
import asyncio
import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from approval_system.queue import ApprovalQueue
from audit_logger.logger import AuditLogger
from auth.models import Role
from auth.store import AgentStore
from auth.dependencies import _get_agent_store
from execution_engine.engine import ExecutionEngine
from policy_engine.engine import PolicyEngine
from policy_engine.models import PolicyDecision, PolicyEvalResult
from policy_engine.store import PolicyStore
from simulation.adapter import SimulationAdapter
from simulation.models import DEFAULT_SIM_BALANCES, SIMULATION_PRICES
from wallet.manager import WalletManager


def _allow_policy_engine() -> PolicyEngine:
    """Return a mock PolicyEngine that always ALLOWs."""
    pe = MagicMock(spec=PolicyEngine)
    pe.evaluate.return_value = PolicyEvalResult(
        decision=PolicyDecision.ALLOW,
        policy_id="global",
        violated_rule=None,
        reason="sim allow-all",
    )
    return pe


# ─────────────────────────────────────────── helpers
def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _headers(key: str) -> dict:
    return {"X-API-Key": key}


# ═══════════════════════════════════════════════════════════════════════════════
# Unit tests — SimulationAdapter
# ═══════════════════════════════════════════════════════════════════════════════

class TestSimulationAdapter:

    @pytest.fixture
    def sim(self, tmp_path) -> SimulationAdapter:
        return SimulationAdapter(db_path=str(tmp_path / "sim.db"))

    # ── seeding ──────────────────────────────────────────────────────────────
    def test_seed_default_balances(self, sim):
        sim.seed_balances("w1")
        result = run(sim.get_balance("w1", "", ""))
        assert Decimal(result.balances["USDT"]["available"]) == DEFAULT_SIM_BALANCES["USDT"]
        assert Decimal(result.balances["BTC"]["available"])  == DEFAULT_SIM_BALANCES["BTC"]
        assert Decimal(result.balances["ETH"]["available"])  == DEFAULT_SIM_BALANCES["ETH"]

    def test_seed_custom_balances(self, sim):
        sim.seed_balances("w1", {"USDT": Decimal("5000"), "SOL": Decimal("100")})
        result = run(sim.get_balance("w1", "", ""))
        assert Decimal(result.balances["USDT"]["available"]) == Decimal("5000")
        assert Decimal(result.balances["SOL"]["available"])  == Decimal("100")
        assert "BTC" not in result.balances  # not seeded

    def test_seed_idempotent(self, sim):
        sim.seed_balances("w1")
        sim.seed_balances("w1")  # second call should not overwrite
        result = run(sim.get_balance("w1", "", ""))
        assert Decimal(result.balances["USDT"]["available"]) == DEFAULT_SIM_BALANCES["USDT"]

    # ── prices ───────────────────────────────────────────────────────────────
    def test_get_known_price(self, sim):
        price = run(sim.get_price("BTCUSDT"))
        assert price == SIMULATION_PRICES["BTCUSDT"]

    def test_get_unknown_price_raises(self, sim):
        with pytest.raises(ValueError, match="Unknown simulation symbol"):
            run(sim.get_price("DOGEUSDT"))

    def test_prices_are_deterministic(self, sim):
        """Same call twice returns the same price — no randomness."""
        p1 = run(sim.get_price("ETHUSDT"))
        p2 = run(sim.get_price("ETHUSDT"))
        assert p1 == p2

    # ── buy fills ────────────────────────────────────────────────────────────
    def test_buy_decreases_usdt_increases_asset(self, sim):
        sim.seed_balances("w1")
        from execution_engine.models import Order, OrderSide, OrderType
        order = Order(
            order_id="o1", agent_id="a1", wallet_id="w1",
            exchange="simulation", side=OrderSide.BUY,
            asset="BTC", quote="USDT", amount=Decimal("0.1"),
            price=None, order_type=OrderType.MARKET,
        )
        result = run(sim.place_order(order, "", ""))
        assert result.success is True
        assert result.filled_amount == Decimal("0.1")

        bal = run(sim.get_balance("w1", "", ""))
        usdt_after = Decimal(bal.balances["USDT"]["available"])
        btc_after  = Decimal(bal.balances["BTC"]["available"])

        # BTC increased by 0.1
        assert btc_after == DEFAULT_SIM_BALANCES["BTC"] + Decimal("0.1")
        # USDT decreased by fill cost (0.1 * price * (1 + slippage) + fee)
        assert usdt_after < DEFAULT_SIM_BALANCES["USDT"]

    def test_sell_increases_usdt_decreases_asset(self, sim):
        sim.seed_balances("w1")
        from execution_engine.models import Order, OrderSide, OrderType
        order = Order(
            order_id="o2", agent_id="a1", wallet_id="w1",
            exchange="simulation", side=OrderSide.SELL,
            asset="BTC", quote="USDT", amount=Decimal("0.5"),
            price=None, order_type=OrderType.MARKET,
        )
        result = run(sim.place_order(order, "", ""))
        assert result.success is True

        bal = run(sim.get_balance("w1", "", ""))
        usdt_after = Decimal(bal.balances["USDT"]["available"])
        btc_after  = Decimal(bal.balances["BTC"]["available"])

        assert btc_after  == DEFAULT_SIM_BALANCES["BTC"] - Decimal("0.5")
        assert usdt_after > DEFAULT_SIM_BALANCES["USDT"]

    def test_buy_insufficient_usdt_fails(self, sim):
        sim.seed_balances("w1", {"USDT": Decimal("1.00"), "BTC": Decimal("0")})
        from execution_engine.models import Order, OrderSide, OrderType
        order = Order(
            order_id="o3", agent_id="a1", wallet_id="w1",
            exchange="simulation", side=OrderSide.BUY,
            asset="BTC", quote="USDT", amount=Decimal("1.0"),
            price=None, order_type=OrderType.MARKET,
        )
        result = run(sim.place_order(order, "", ""))
        assert result.success is False
        assert "Insufficient" in result.error

    def test_sell_insufficient_asset_fails(self, sim):
        sim.seed_balances("w1", {"USDT": Decimal("100000"), "BTC": Decimal("0.001")})
        from execution_engine.models import Order, OrderSide, OrderType
        order = Order(
            order_id="o4", agent_id="a1", wallet_id="w1",
            exchange="simulation", side=OrderSide.SELL,
            asset="BTC", quote="USDT", amount=Decimal("1.0"),
            price=None, order_type=OrderType.MARKET,
        )
        result = run(sim.place_order(order, "", ""))
        assert result.success is False
        assert "Insufficient" in result.error

    # ── reset ─────────────────────────────────────────────────────────────────
    def test_reset_restores_initial_balances(self, sim):
        sim.seed_balances("w1")
        # Execute a buy to change balances
        from execution_engine.models import Order, OrderSide, OrderType
        order = Order(
            order_id="o5", agent_id="a1", wallet_id="w1",
            exchange="simulation", side=OrderSide.BUY,
            asset="BTC", quote="USDT", amount=Decimal("0.1"),
            price=None, order_type=OrderType.MARKET,
        )
        run(sim.place_order(order, "", ""))

        # Balances changed — reset
        sim.reset_balances("w1")
        bal = run(sim.get_balance("w1", "", ""))
        assert Decimal(bal.balances["USDT"]["available"]) == DEFAULT_SIM_BALANCES["USDT"]
        assert Decimal(bal.balances["BTC"]["available"])  == DEFAULT_SIM_BALANCES["BTC"]

    # ── portfolio ─────────────────────────────────────────────────────────────
    def test_portfolio_value_calculation(self, sim):
        sim.seed_balances("w1", {"USDT": Decimal("10000"), "BTC": Decimal("1.0")})
        pv = sim.get_portfolio_value("w1")
        expected_total = Decimal("10000") + Decimal("1.0") * SIMULATION_PRICES["BTCUSDT"]
        assert Decimal(pv["total_usd_value"]) == expected_total.quantize(Decimal("0.01"))

    def test_portfolio_empty_wallet_is_zero(self, sim):
        pv = sim.get_portfolio_value("nonexistent")
        assert pv["total_usd_value"] == "0.00"
        assert pv["breakdown"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# Integration tests — ExecutionEngine with SimulationAdapter
# ═══════════════════════════════════════════════════════════════════════════════

class TestSimulationExecution:
    """Tests that the execution engine routes simulation wallets correctly."""

    @pytest.fixture
    def setup(self, tmp_path):
        db = str(tmp_path / "exec.db")
        wm = WalletManager(db_path=db)
        aq = ApprovalQueue(db_path=db)
        al = AuditLogger(db_path=db)
        sim = SimulationAdapter(db_path=db)

        engine = ExecutionEngine(
            wallet_manager=wm,
            policy_engine=_allow_policy_engine(),
            approval_queue=aq,
            audit_logger=al,
        )
        engine.register_adapter(sim)

        # Create a simulation wallet
        wallet = wm.register(
            agent_id="agent1",
            label="Test Sim Wallet",
            exchange="simulation",
            api_key="sim",
            api_secret="sim",
        )
        sim.seed_balances(wallet.wallet_id)

        return engine, wm, al, sim, wallet

    def test_sim_buy_succeeds(self, setup):
        engine, wm, al, sim, wallet = setup
        result = run(engine.execute_trade(
            agent_id="agent1",
            wallet_id=wallet.wallet_id,
            side="buy",
            asset="BTC",
            amount=Decimal("0.1"),
        ))
        assert result.success is True
        assert result.exchange_order_id.startswith("sim_")

    def test_sim_sell_succeeds(self, setup):
        engine, wm, al, sim, wallet = setup
        result = run(engine.execute_trade(
            agent_id="agent1",
            wallet_id=wallet.wallet_id,
            side="sell",
            asset="ETH",
            amount=Decimal("1.0"),
        ))
        assert result.success is True

    def test_sim_buy_updates_wallet_balances(self, setup):
        engine, wm, al, sim, wallet = setup
        run(engine.execute_trade(
            agent_id="agent1",
            wallet_id=wallet.wallet_id,
            side="buy",
            asset="BTC",
            amount=Decimal("0.5"),
        ))
        bal = run(sim.get_balance(wallet.wallet_id, "", ""))
        btc_after = Decimal(bal.balances["BTC"]["available"])
        # Started with 1.0, bought 0.5 → should have 1.5
        assert btc_after == DEFAULT_SIM_BALANCES["BTC"] + Decimal("0.5")

    def test_sim_audit_log_marked_simulation_true(self, setup):
        engine, wm, al, sim, wallet = setup
        run(engine.execute_trade(
            agent_id="agent1",
            wallet_id=wallet.wallet_id,
            side="buy",
            asset="BTC",
            amount=Decimal("0.01"),
        ))
        entries = al.get_history(agent_id="agent1")
        assert len(entries) == 1
        assert entries[0].metadata.get("simulation") is True

    def test_real_wallet_audit_log_marked_simulation_false(self, setup, tmp_path):
        engine, wm, al, sim, wallet = setup
        from execution_engine.adapters.mock import MockExchangeAdapter
        engine.register_adapter(MockExchangeAdapter())
        real_wallet = wm.register(
            agent_id="agent1",
            label="Real Wallet",
            exchange="mock",
            api_key="k",
            api_secret="s",
        )
        run(engine.execute_trade(
            agent_id="agent1",
            wallet_id=real_wallet.wallet_id,
            side="buy",
            asset="BTC",
            amount=Decimal("0.01"),
        ))
        entries = al.get_history(agent_id="agent1")
        # Most recent entry is the real trade
        assert entries[0].metadata.get("simulation") is False

    def test_sim_order_id_has_sim_prefix(self, setup):
        engine, wm, al, sim, wallet = setup
        result = run(engine.execute_trade(
            agent_id="agent1",
            wallet_id=wallet.wallet_id,
            side="buy",
            asset="ETH",
            amount=Decimal("1.0"),
        ))
        assert result.exchange_order_id.startswith("sim_")

    def test_sim_unknown_symbol_fails_gracefully(self, setup):
        from execution_engine.engine import ExecutionError
        engine, wm, al, sim, wallet = setup
        with pytest.raises(ExecutionError, match="Unknown symbol"):
            run(engine.execute_trade(
                agent_id="agent1",
                wallet_id=wallet.wallet_id,
                side="buy",
                asset="DOGE",
                amount=Decimal("100"),
            ))

    def test_sim_wallets_isolated_per_wallet_id(self, setup):
        engine, wm, al, sim, wallet = setup
        # Create a second simulation wallet
        wallet2 = setup[1].register(
            agent_id="agent2",
            label="Another Sim",
            exchange="simulation",
            api_key="sim",
            api_secret="sim",
        )
        sim.seed_balances(wallet2.wallet_id)

        # Trade on wallet1
        run(engine.execute_trade(
            agent_id="agent1",
            wallet_id=wallet.wallet_id,
            side="buy",
            asset="BTC",
            amount=Decimal("0.1"),
        ))

        # wallet2 balances should be unchanged
        bal2 = run(sim.get_balance(wallet2.wallet_id, "", ""))
        assert Decimal(bal2.balances["BTC"]["available"]) == DEFAULT_SIM_BALANCES["BTC"]


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP API tests (TestClient)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSimulationAPI:

    @pytest.fixture
    def client_and_keys(self, tmp_path):
        from api.app import app
        db = str(tmp_path / "api_sim.db")

        agent_store = AgentStore(db_path=db)
        wm   = WalletManager(db_path=db)
        sim  = SimulationAdapter(db_path=db)

        # Wire dependencies
        from api.deps import (
            get_agent_store, get_wallet_manager, get_sim_adapter,
            get_execution_engine,
        )
        from auth.dependencies import _get_agent_store

        app.dependency_overrides[_get_agent_store]    = lambda: agent_store
        app.dependency_overrides[get_agent_store]     = lambda: agent_store
        app.dependency_overrides[get_wallet_manager]  = lambda: wm
        app.dependency_overrides[get_sim_adapter]     = lambda: sim

        # Build a fresh execution engine with both adapters
        from execution_engine.adapters.mock import MockExchangeAdapter
        from approval_system.queue import ApprovalQueue
        from audit_logger.logger import AuditLogger
        aq  = ApprovalQueue(db_path=db)
        al  = AuditLogger(db_path=db)
        engine = ExecutionEngine(
            wallet_manager=wm,
            policy_engine=_allow_policy_engine(),
            approval_queue=aq,
            audit_logger=al,
        )
        engine.register_adapter(MockExchangeAdapter())
        engine.register_adapter(sim)
        app.dependency_overrides[get_execution_engine] = lambda: engine

        # Register test agents
        _, admin_key   = agent_store.register("admin",  Role.ADMIN)
        _, trader_key  = agent_store.register("trader1", Role.TRADER)
        _, sim_key     = agent_store.register("simbot", Role.TRADER, simulation=True)

        client = TestClient(app)
        yield client, admin_key, trader_key, sim_key, wm, sim, agent_store

        # cleanup overrides
        for dep in [
            _get_agent_store, get_agent_store, get_wallet_manager,
            get_sim_adapter, get_execution_engine,
        ]:
            app.dependency_overrides.pop(dep, None)

    # ── wallet creation ───────────────────────────────────────────────────────
    def test_create_sim_wallet_default_balances(self, client_and_keys):
        client, admin_key, _, _, _, _, _ = client_and_keys
        resp = client.post(
            "/simulation/wallets",
            json={"agent_id": "admin", "label": "Test Sim"},
            headers=_headers(admin_key),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["exchange"] == "simulation"
        assert Decimal(body["balances"]["USDT"]["available"]) == DEFAULT_SIM_BALANCES["USDT"]

    def test_create_sim_wallet_custom_balances(self, client_and_keys):
        client, admin_key, _, _, _, _, _ = client_and_keys
        resp = client.post(
            "/simulation/wallets",
            json={
                "agent_id": "admin",
                "label": "Custom Sim",
                "initial_balances": {"USDT": "5000", "ETH": "2.5"},
            },
            headers=_headers(admin_key),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert Decimal(body["balances"]["USDT"]["available"]) == Decimal("5000")
        assert Decimal(body["balances"]["ETH"]["available"])  == Decimal("2.5")

    def test_create_sim_wallet_forbidden_for_other_agent(self, client_and_keys):
        client, _, trader_key, _, _, _, _ = client_and_keys
        resp = client.post(
            "/simulation/wallets",
            json={"agent_id": "admin"},       # trader1 trying to create for admin
            headers=_headers(trader_key),
        )
        assert resp.status_code == 403

    # ── get wallet ────────────────────────────────────────────────────────────
    def test_get_sim_wallet_returns_balances(self, client_and_keys):
        client, admin_key, _, _, wm, sim, _ = client_and_keys
        wallet = wm.register("admin", "My Sim", "simulation", "sim", "sim")
        sim.seed_balances(wallet.wallet_id)

        resp = client.get(f"/simulation/wallets/{wallet.wallet_id}", headers=_headers(admin_key))
        assert resp.status_code == 200
        assert "total_usd_value" in resp.json()

    def test_get_real_wallet_via_sim_endpoint_returns_400(self, client_and_keys):
        client, admin_key, _, _, wm, _, _ = client_and_keys
        real_wallet = wm.register("admin", "Real", "mock", "k", "s")
        resp = client.get(f"/simulation/wallets/{real_wallet.wallet_id}", headers=_headers(admin_key))
        assert resp.status_code == 400

    # ── reset ─────────────────────────────────────────────────────────────────
    def test_reset_restores_initial_balances(self, client_and_keys):
        client, admin_key, _, _, wm, sim, _ = client_and_keys
        wallet = wm.register("admin", "Reset Test", "simulation", "sim", "sim")
        sim.seed_balances(wallet.wallet_id, {"USDT": Decimal("1000")})

        # Modify balance manually to simulate trading
        from execution_engine.models import Order, OrderSide, OrderType
        order = Order(
            order_id="x1", agent_id="admin", wallet_id=wallet.wallet_id,
            exchange="simulation", side=OrderSide.SELL,
            asset="USDT", quote="USDT", amount=Decimal("0"),  # dummy
            price=None, order_type=OrderType.MARKET,
        )
        # Force a direct balance change via DB
        sim._upsert(sim._connect(), wallet.wallet_id, "USDT", Decimal("500"))

        resp = client.post(
            f"/simulation/wallets/{wallet.wallet_id}/reset",
            headers=_headers(admin_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert Decimal(body["balances"]["USDT"]["available"]) == Decimal("1000")

    # ── portfolio ──────────────────────────────────────────────────────────────
    def test_portfolio_aggregates_all_sim_wallets(self, client_and_keys):
        client, admin_key, _, _, wm, sim, _ = client_and_keys
        w1 = wm.register("admin", "Sim 1", "simulation", "sim", "sim")
        w2 = wm.register("admin", "Sim 2", "simulation", "sim", "sim")
        sim.seed_balances(w1.wallet_id, {"USDT": Decimal("10000")})
        sim.seed_balances(w2.wallet_id, {"USDT": Decimal("5000")})

        resp = client.get("/simulation/portfolio/admin", headers=_headers(admin_key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["simulation_wallets"] == 2
        assert Decimal(body["grand_total_usd_value"]) == Decimal("15000.00")

    # ── simulation agent restriction ──────────────────────────────────────────
    def test_sim_agent_cannot_trade_real_wallet(self, client_and_keys):
        client, _, _, sim_key, wm, _, _ = client_and_keys
        # Create a real (mock exchange) wallet for simbot
        real_wallet = wm.register("simbot", "Real", "mock", "k", "s")
        resp = client.post(
            "/execute",
            json={
                "agent_id": "simbot",
                "wallet_id": real_wallet.wallet_id,
                "side": "buy",
                "asset": "BTC",
                "amount": "0.01",
            },
            headers=_headers(sim_key),
        )
        assert resp.status_code == 403
        assert "simulation" in resp.json()["detail"].lower()

    def test_sim_agent_can_trade_simulation_wallet(self, client_and_keys):
        client, _, _, sim_key, wm, sim, _ = client_and_keys
        sim_wallet = wm.register("simbot", "Bot Sim", "simulation", "sim", "sim")
        sim.seed_balances(sim_wallet.wallet_id)

        resp = client.post(
            "/execute",
            json={
                "agent_id": "simbot",
                "wallet_id": sim_wallet.wallet_id,
                "side": "buy",
                "asset": "BTC",
                "amount": "0.01",
            },
            headers=_headers(sim_key),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "executed"

    def test_non_sim_agent_can_use_simulation_wallet(self, client_and_keys):
        """Regular trader can also use a simulation wallet (useful for testing)."""
        client, _, trader_key, _, wm, sim, _ = client_and_keys
        sim_wallet = wm.register("trader1", "Trader Sim", "simulation", "sim", "sim")
        sim.seed_balances(sim_wallet.wallet_id)

        resp = client.post(
            "/execute",
            json={
                "agent_id": "trader1",
                "wallet_id": sim_wallet.wallet_id,
                "side": "sell",
                "asset": "ETH",
                "amount": "1.0",
            },
            headers=_headers(trader_key),
        )
        assert resp.status_code == 200

    def test_simulation_flag_returned_in_agent_identity(self, client_and_keys):
        client, _, _, sim_key, _, _, _ = client_and_keys
        resp = client.get("/auth/agents/me", headers=_headers(sim_key))
        assert resp.status_code == 200
        assert resp.json()["simulation"] is True
