"""
Analytics module tests.

TestPnLTracker        (unit)  — 12 tests   reads risk_fills + risk_positions
TestMetricsAggregator (unit)  — 10 tests   reads audit_log
TestAnalyticsAPI      (HTTP)  — 10 tests   GET /analytics/pnl and /metrics
                                     total  32 tests
"""
from __future__ import annotations
import json
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from analytics.metrics_aggregator import MetricsAggregator
from analytics.pnl_tracker import PnLTracker
from audit_logger.logger import AuditLogger
from auth.models import Role
from auth.store import AgentStore
from auth.dependencies import _get_agent_store
from risk_engine.exposure_tracker import ExposureTracker


# ─────────────────────────────────────────────────────────── helpers

def _ts(offset_seconds: int = 0) -> str:
    """Return a UTC ISO string offset from now."""
    return (datetime.utcnow() + timedelta(seconds=offset_seconds)).isoformat()

def _headers(key: str) -> dict:
    return {"X-API-Key": key}


# ═══════════════════════════════════════════════════════════════════════════════
# PnLTracker unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestPnLTracker:

    @pytest.fixture
    def tracker_and_et(self, tmp_path):
        db = str(tmp_path / "pnl.db")
        et = ExposureTracker(db_path=db)
        pt = PnLTracker(db_path=db)
        return pt, et

    # ── empty state ──────────────────────────────────────────────────────────
    def test_empty_agent_returns_zero_report(self, tracker_and_et):
        pt, _ = tracker_and_et
        report = pt.get_pnl("nobody")
        assert report.total_realized_pnl == Decimal("0")
        assert report.total_volume_usd   == Decimal("0")
        assert report.total_fills        == 0
        assert report.per_asset          == {}

    def test_empty_fills_returns_empty_list(self, tracker_and_et):
        pt, _ = tracker_and_et
        assert pt.get_fills("nobody") == []

    # ── single buy fill ───────────────────────────────────────────────────────
    def test_single_buy_volume_increases(self, tracker_and_et):
        pt, et = tracker_and_et
        et.record_fill("a1", "buy", "BTC", Decimal("0.1"), Decimal("67500"))
        report = pt.get_pnl("a1")
        assert report.total_fills       == 1
        assert report.per_asset["BTC"].buy_fills  == 1
        assert report.per_asset["BTC"].sell_fills == 0
        # Buy fills have 0 realized P&L
        assert report.per_asset["BTC"].realized_pnl == Decimal("0")
        # Volume = 0.1 * 67500
        assert report.per_asset["BTC"].volume_usd == Decimal("6750.00")

    # ── buy + sell realized P&L ───────────────────────────────────────────────
    def test_buy_sell_realized_pnl_profitable(self, tracker_and_et):
        pt, et = tracker_and_et
        # Buy 1 BTC at $67500
        et.record_fill("a1", "buy",  "BTC", Decimal("1.0"), Decimal("67500"))
        # Sell 1 BTC at $70000 → profit = (70000 - 67500) * 1 = $2500
        et.record_fill("a1", "sell", "BTC", Decimal("1.0"), Decimal("70000"))
        report = pt.get_pnl("a1")
        assert report.per_asset["BTC"].realized_pnl == Decimal("2500.00")
        assert report.total_realized_pnl            == Decimal("2500.00")

    def test_buy_sell_realized_pnl_loss(self, tracker_and_et):
        pt, et = tracker_and_et
        et.record_fill("a1", "buy",  "ETH", Decimal("2.0"), Decimal("3450"))
        et.record_fill("a1", "sell", "ETH", Decimal("2.0"), Decimal("3000"))
        report = pt.get_pnl("a1")
        # (3000 - 3450) * 2 = -$900
        assert report.per_asset["ETH"].realized_pnl == Decimal("-900.00")

    # ── per-asset isolation ───────────────────────────────────────────────────
    def test_multiple_assets_independent(self, tracker_and_et):
        pt, et = tracker_and_et
        et.record_fill("a1", "buy",  "BTC", Decimal("1"), Decimal("67500"))
        et.record_fill("a1", "sell", "BTC", Decimal("1"), Decimal("70000"))
        et.record_fill("a1", "buy",  "ETH", Decimal("5"), Decimal("3450"))
        report = pt.get_pnl("a1")
        assert "BTC" in report.per_asset
        assert "ETH" in report.per_asset
        assert report.per_asset["BTC"].realized_pnl == Decimal("2500.00")
        assert report.per_asset["ETH"].realized_pnl == Decimal("0.00")   # no sell yet

    # ── asset filter ─────────────────────────────────────────────────────────
    def test_asset_filter_isolates_one_asset(self, tracker_and_et):
        pt, et = tracker_and_et
        et.record_fill("a1", "buy", "BTC", Decimal("1"), Decimal("67500"))
        et.record_fill("a1", "buy", "ETH", Decimal("5"), Decimal("3450"))
        report = pt.get_pnl("a1", asset="BTC")
        assert "BTC" in report.per_asset
        assert "ETH" not in report.per_asset

    # ── time window filter ────────────────────────────────────────────────────
    def test_time_filter_excludes_old_fills(self, tracker_and_et):
        pt, et = tracker_and_et
        # Record 2 fills — both happen "now"
        et.record_fill("a1", "buy", "BTC", Decimal("1"),   Decimal("60000"))
        et.record_fill("a1", "buy", "BTC", Decimal("0.5"), Decimal("67500"))
        # A start set in the future should exclude all fills
        future_start = _ts(60)      # 60 s ahead — safely in the future
        report = pt.get_pnl("a1", start=future_start)
        assert report.total_fills == 0
        # No filter returns both
        report_all = pt.get_pnl("a1")
        assert report_all.total_fills == 2

    # ── agent isolation ───────────────────────────────────────────────────────
    def test_agents_are_isolated(self, tracker_and_et):
        pt, et = tracker_and_et
        et.record_fill("agent_A", "buy", "BTC", Decimal("1"), Decimal("67500"))
        et.record_fill("agent_B", "buy", "ETH", Decimal("5"), Decimal("3450"))
        report_a = pt.get_pnl("agent_A")
        report_b = pt.get_pnl("agent_B")
        assert "BTC" in report_a.per_asset
        assert "BTC" not in report_b.per_asset

    # ── open position overlay ─────────────────────────────────────────────────
    def test_open_position_reflected(self, tracker_and_et):
        pt, et = tracker_and_et
        et.record_fill("a1", "buy", "BTC", Decimal("0.5"), Decimal("67500"))
        report = pt.get_pnl("a1")
        btc = report.per_asset["BTC"]
        assert btc.open_amount   == Decimal("0.5")
        assert btc.avg_cost_usd  == Decimal("67500.00000")
        assert btc.open_value_usd > Decimal("0")

    # ── fills endpoint ────────────────────────────────────────────────────────
    def test_fills_returned_newest_first(self, tracker_and_et):
        pt, et = tracker_and_et
        et.record_fill("a1", "buy", "BTC", Decimal("0.1"), Decimal("67500"))
        et.record_fill("a1", "buy", "ETH", Decimal("1.0"), Decimal("3450"))
        fills = pt.get_fills("a1")
        assert len(fills) == 2
        # Should have fill_id, asset, side, etc.
        assert "fill_id" in fills[0]
        assert "realized_pnl" in fills[0]

    # ── volume aggregation ────────────────────────────────────────────────────
    def test_total_volume_sums_all_fills(self, tracker_and_et):
        pt, et = tracker_and_et
        et.record_fill("a1", "buy",  "BTC", Decimal("1"), Decimal("67500"))
        et.record_fill("a1", "sell", "BTC", Decimal("1"), Decimal("70000"))
        report = pt.get_pnl("a1")
        # Volume = 67500 + 70000 (both fills count)
        assert report.total_volume_usd == Decimal("137500.00")

    # ── profitable_assets count ───────────────────────────────────────────────
    def test_profitable_assets_count(self, tracker_and_et):
        pt, et = tracker_and_et
        # BTC: profitable
        et.record_fill("a1", "buy",  "BTC", Decimal("1"), Decimal("60000"))
        et.record_fill("a1", "sell", "BTC", Decimal("1"), Decimal("70000"))
        # ETH: loss
        et.record_fill("a1", "buy",  "ETH", Decimal("2"), Decimal("3500"))
        et.record_fill("a1", "sell", "ETH", Decimal("2"), Decimal("3000"))
        report = pt.get_pnl("a1")
        assert report.profitable_assets == 1
        assert report.losing_assets     == 1


# ═══════════════════════════════════════════════════════════════════════════════
# MetricsAggregator unit tests
# ═══════════════════════════════════════════════════════════════════════════════

def _audit_entry(
    al: AuditLogger,
    agent_id: str,
    action: str,
    decision: str,
    success: bool = True,
    simulation: bool = False,
    exec_result: dict | None = None,
) -> None:
    """Helper: write a minimal audit entry."""
    if exec_result is None and decision == "allow":
        exec_result = {
            "success": success,
            "order": {
                "created_at": datetime.utcnow().isoformat(),
                "filled_at":  (datetime.utcnow() + timedelta(milliseconds=50)).isoformat(),
            },
            "filled_amount": "0.1",
            "avg_price": "67500",
        }
    al.log(
        agent_id=agent_id,
        action=action,
        policy_decision=decision,
        approval_chain=None,
        execution_result=exec_result,
        metadata={"simulation": simulation, "amount_usd": "6750"},
    )


class TestMetricsAggregator:

    @pytest.fixture
    def agg_and_logger(self, tmp_path):
        db = str(tmp_path / "metrics.db")
        al  = AuditLogger(db_path=db)
        agg = MetricsAggregator(db_path=db)
        return agg, al

    # ── empty state ──────────────────────────────────────────────────────────
    def test_empty_agent_returns_zeros(self, agg_and_logger):
        agg, _ = agg_and_logger
        m = agg.get_metrics("nobody")
        assert m.total_actions    == 0
        assert m.trades_executed  == 0
        assert m.denial_rate      == Decimal("0")
        assert m.total_volume_usd == Decimal("0")

    # ── executed trades ───────────────────────────────────────────────────────
    def test_executed_trade_counted(self, agg_and_logger):
        agg, al = agg_and_logger
        _audit_entry(al, "a1", "buy:BTC", "allow", success=True)
        m = agg.get_metrics("a1")
        assert m.total_actions   == 1
        assert m.trades_executed == 1
        assert m.trades_denied   == 0

    def test_failed_execution_counted_separately(self, agg_and_logger):
        agg, al = agg_and_logger
        _audit_entry(al, "a1", "buy:BTC", "allow", success=False,
                     exec_result={"success": False, "order": {"created_at": datetime.utcnow().isoformat(), "filled_at": None}})
        m = agg.get_metrics("a1")
        assert m.trades_executed == 0
        assert m.trades_failed   == 1

    # ── denial rate ───────────────────────────────────────────────────────────
    def test_policy_deny_counted(self, agg_and_logger):
        agg, al = agg_and_logger
        _audit_entry(al, "a1", "buy:BTC", "policy:deny",  exec_result=None)
        _audit_entry(al, "a1", "buy:ETH", "risk:deny",    exec_result=None)
        _audit_entry(al, "a1", "sell:BTC", "allow", success=True)
        m = agg.get_metrics("a1")
        assert m.trades_denied == 2
        # denial_rate = 2/3
        assert m.denial_rate == Decimal("0.6667")

    # ── approval required rate ────────────────────────────────────────────────
    def test_approval_required_counted(self, agg_and_logger):
        agg, al = agg_and_logger
        _audit_entry(al, "a1", "buy:BTC", "policy:require_approval", exec_result=None)
        _audit_entry(al, "a1", "buy:ETH", "risk:require_approval",   exec_result=None)
        _audit_entry(al, "a1", "sell:BTC", "allow", success=True)
        m = agg.get_metrics("a1")
        assert m.trades_pending        == 2
        assert m.approval_required_rate == Decimal("0.6667")

    # ── simulation split ──────────────────────────────────────────────────────
    def test_simulation_trades_split(self, agg_and_logger):
        agg, al = agg_and_logger
        _audit_entry(al, "a1", "buy:BTC", "allow", simulation=True)
        _audit_entry(al, "a1", "buy:ETH", "allow", simulation=False)
        _audit_entry(al, "a1", "buy:SOL", "allow", simulation=True)
        m = agg.get_metrics("a1")
        assert m.simulation_trades == 2
        assert m.real_trades       == 1

    # ── agent isolation ───────────────────────────────────────────────────────
    def test_agents_isolated(self, agg_and_logger):
        agg, al = agg_and_logger
        _audit_entry(al, "agent_A", "buy:BTC", "allow")
        _audit_entry(al, "agent_A", "buy:BTC", "policy:deny", exec_result=None)
        _audit_entry(al, "agent_B", "buy:ETH", "allow")
        m_a = agg.get_metrics("agent_A")
        m_b = agg.get_metrics("agent_B")
        assert m_a.total_actions == 2
        assert m_b.total_actions == 1

    # ── avg execution time ────────────────────────────────────────────────────
    def test_avg_execution_time_computed(self, agg_and_logger):
        agg, al = agg_and_logger
        # Entry with ~50ms fill time baked in via _audit_entry helper
        _audit_entry(al, "a1", "buy:BTC", "allow", success=True)
        m = agg.get_metrics("a1")
        # Should be > 0 (50ms injected) and < 1000ms
        assert m.avg_execution_time_ms > Decimal("0")
        assert m.avg_execution_time_ms < Decimal("1000")

    # ── time window filter ────────────────────────────────────────────────────
    def test_time_filter_excludes_old_entries(self, agg_and_logger):
        agg, al = agg_and_logger
        _audit_entry(al, "a1", "buy:BTC", "allow")
        _audit_entry(al, "a1", "buy:ETH", "allow")
        # Future start → all entries excluded
        future_start = _ts(60)
        m = agg.get_metrics("a1", start=future_start)
        assert m.total_actions == 0
        # No filter → both included
        m_all = agg.get_metrics("a1")
        assert m_all.total_actions == 2

    # ── rates are zero when no actions ───────────────────────────────────────
    def test_rates_are_zero_with_no_actions(self, agg_and_logger):
        agg, _ = agg_and_logger
        m = agg.get_metrics("ghost")
        assert m.denial_rate            == Decimal("0")
        assert m.approval_required_rate == Decimal("0")

    # ── combined deny + execute ───────────────────────────────────────────────
    def test_combined_mix_of_decisions(self, agg_and_logger):
        agg, al = agg_and_logger
        _audit_entry(al, "a1", "buy:BTC", "allow")
        _audit_entry(al, "a1", "buy:BTC", "allow")
        _audit_entry(al, "a1", "buy:BTC", "policy:deny", exec_result=None)
        _audit_entry(al, "a1", "buy:BTC", "risk:require_approval", exec_result=None)
        m = agg.get_metrics("a1")
        assert m.total_actions   == 4
        assert m.trades_executed == 2
        assert m.trades_denied   == 1
        assert m.trades_pending  == 1
        # denial_rate = 1/4
        assert m.denial_rate == Decimal("0.2500")


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP API tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnalyticsAPI:

    @pytest.fixture
    def client_and_keys(self, tmp_path):
        from api.app import app
        db = str(tmp_path / "analytics_api.db")

        agent_store = AgentStore(db_path=db)
        al  = AuditLogger(db_path=db)
        et  = ExposureTracker(db_path=db)
        pt  = PnLTracker(db_path=db)
        agg = MetricsAggregator(db_path=db)

        from api.deps import (
            get_agent_store, get_pnl_tracker, get_metrics_aggregator,
        )
        from auth.dependencies import _get_agent_store

        app.dependency_overrides[_get_agent_store]        = lambda: agent_store
        app.dependency_overrides[get_agent_store]         = lambda: agent_store
        app.dependency_overrides[get_pnl_tracker]         = lambda: pt
        app.dependency_overrides[get_metrics_aggregator]  = lambda: agg

        _, admin_key  = agent_store.register("admin",   Role.ADMIN)
        _, trader_key = agent_store.register("trader1", Role.TRADER)
        _, ro_key     = agent_store.register("reader",  Role.READONLY)

        client = TestClient(app)
        yield client, admin_key, trader_key, ro_key, al, et, pt, agg

        for dep in [_get_agent_store, get_agent_store,
                    get_pnl_tracker, get_metrics_aggregator]:
            app.dependency_overrides.pop(dep, None)

    # ── /analytics/pnl ────────────────────────────────────────────────────────
    def test_pnl_empty_returns_200(self, client_and_keys):
        client, admin_key, *_ = client_and_keys
        resp = client.get("/analytics/pnl/admin", headers=_headers(admin_key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"]          == "admin"
        assert Decimal(body["total_realized_pnl"]) == Decimal("0")
        assert body["total_fills"]        == 0

    def test_pnl_with_fills(self, client_and_keys):
        client, admin_key, _, _, al, et, *_ = client_and_keys
        et.record_fill("admin", "buy",  "BTC", Decimal("1"),   Decimal("67500"))
        et.record_fill("admin", "sell", "BTC", Decimal("0.5"), Decimal("70000"))
        resp = client.get("/analytics/pnl/admin", headers=_headers(admin_key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_fills"] == 2
        assert Decimal(body["per_asset"]["BTC"]["realized_pnl"]) == Decimal("1250.00")

    def test_pnl_asset_filter(self, client_and_keys):
        client, admin_key, _, _, al, et, *_ = client_and_keys
        et.record_fill("admin", "buy", "BTC", Decimal("1"), Decimal("67500"))
        et.record_fill("admin", "buy", "ETH", Decimal("5"), Decimal("3450"))
        resp = client.get("/analytics/pnl/admin?asset=BTC", headers=_headers(admin_key))
        body = resp.json()
        assert "BTC" in body["per_asset"]
        assert "ETH" not in body["per_asset"]

    def test_pnl_forbidden_for_other_agent(self, client_and_keys):
        client, _, trader_key, *_ = client_and_keys
        resp = client.get("/analytics/pnl/admin", headers=_headers(trader_key))
        assert resp.status_code == 403

    def test_pnl_admin_can_query_any_agent(self, client_and_keys):
        client, admin_key, *_ = client_and_keys
        resp = client.get("/analytics/pnl/trader1", headers=_headers(admin_key))
        assert resp.status_code == 200

    # ── /analytics/pnl/{agent_id}/fills ──────────────────────────────────────
    def test_fills_endpoint_returns_list(self, client_and_keys):
        client, admin_key, _, _, al, et, *_ = client_and_keys
        et.record_fill("admin", "buy", "BTC", Decimal("0.1"), Decimal("67500"))
        resp = client.get("/analytics/pnl/admin/fills", headers=_headers(admin_key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert len(body["fills"]) == 1
        assert body["fills"][0]["asset"] == "BTC"

    # ── /analytics/metrics ───────────────────────────────────────────────────
    def test_metrics_empty_returns_200(self, client_and_keys):
        client, admin_key, *_ = client_and_keys
        resp = client.get("/analytics/metrics/admin", headers=_headers(admin_key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_actions"]  == 0
        assert body["denial_rate"]    == "0"

    def test_metrics_reflect_audit_entries(self, client_and_keys):
        client, admin_key, _, _, al, *_ = client_and_keys
        _audit_entry(al, "admin", "buy:BTC", "allow")
        _audit_entry(al, "admin", "buy:ETH", "policy:deny", exec_result=None)
        resp = client.get("/analytics/metrics/admin", headers=_headers(admin_key))
        body = resp.json()
        assert body["total_actions"]  == 2
        assert body["trades_executed"] == 1
        assert body["trades_denied"]   == 1

    def test_metrics_forbidden_for_other_agent(self, client_and_keys):
        client, _, trader_key, *_ = client_and_keys
        resp = client.get("/analytics/metrics/admin", headers=_headers(trader_key))
        assert resp.status_code == 403

    def test_readonly_can_read_own_metrics(self, client_and_keys):
        client, _, _, ro_key, *_ = client_and_keys
        resp = client.get("/analytics/metrics/reader", headers=_headers(ro_key))
        assert resp.status_code == 200

    def test_metrics_unauthenticated_returns_401(self, client_and_keys):
        client, *_ = client_and_keys
        resp = client.get("/analytics/metrics/admin")
        assert resp.status_code == 401
