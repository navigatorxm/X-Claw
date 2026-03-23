"""
Tests for authentication and permission enforcement.

Tests the full HTTP layer via FastAPI TestClient with dependency overrides,
so no server needs to be running.

Coverage:
- AgentStore: register, authenticate, rotate, revoke, duplicate detection
- Auth dependencies: missing key → 401, invalid key → 401, inactive → 401
- Permission enforcement per role: trader, approver, readonly, admin
- Agent scoping: non-admin cannot act on other agents' data
- Bootstrap: first agent registration is open; subsequent require admin
- Key hashing: raw key is never stored, verify only via hash
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from auth.models import Permission, Role, ROLE_PERMISSIONS
from auth.store import AgentStore, _hash_key, _verify_key


# ─────────────────────────────────────────────────────── AgentStore unit tests

@pytest.fixture
def store(tmp_path):
    return AgentStore(db_path=str(tmp_path / "auth.db"))


class TestAgentStore:

    def test_register_returns_raw_key(self, store):
        identity, raw_key = store.register("agent_1", Role.TRADER)
        assert raw_key.startswith("xclaw_agent_1_")
        assert len(raw_key) > 20

    def test_raw_key_not_stored(self, store):
        identity, raw_key = store.register("agent_1", Role.TRADER)
        assert identity.key_hash != raw_key
        assert identity.key_hash == _hash_key(raw_key)

    def test_authenticate_success(self, store):
        identity, raw_key = store.register("agent_1", Role.TRADER)
        result = store.authenticate(raw_key)
        assert result is not None
        assert result.agent_id == "agent_1"

    def test_authenticate_wrong_key_fails(self, store):
        store.register("agent_1", Role.TRADER)
        result = store.authenticate("xclaw_agent_1_wrongkey")
        assert result is None

    def test_authenticate_missing_prefix_fails(self, store):
        result = store.authenticate("no_prefix_key")
        assert result is None

    def test_authenticate_empty_fails(self, store):
        assert store.authenticate("") is None
        assert store.authenticate(None) is None

    def test_duplicate_registration_raises(self, store):
        store.register("agent_1", Role.TRADER)
        with pytest.raises(ValueError, match="already registered"):
            store.register("agent_1", Role.ADMIN)

    def test_role_permissions_assigned(self, store):
        _, _ = store.register("admin_1", Role.ADMIN)
        identity = store.get("admin_1")
        assert Permission.ADMIN in identity.permissions
        assert Permission.EXECUTE in identity.permissions

    def test_readonly_role_has_only_read(self, store):
        store.register("ro_1", Role.READONLY)
        identity = store.get("ro_1")
        assert identity.permissions == frozenset({Permission.READ})

    def test_custom_permissions(self, store):
        custom = frozenset({Permission.EXECUTE})
        identity, _ = store.register("custom_1", Role.TRADER, custom_permissions=custom)
        assert identity.permissions == custom

    def test_rotate_key_invalidates_old(self, store):
        identity, old_key = store.register("agent_1", Role.TRADER)
        _, new_key = store.rotate_key("agent_1")
        assert store.authenticate(old_key) is None
        assert store.authenticate(new_key) is not None

    def test_rotate_key_different_from_old(self, store):
        _, old_key = store.register("agent_1", Role.TRADER)
        _, new_key = store.rotate_key("agent_1")
        assert old_key != new_key

    def test_revoke_blocks_authentication(self, store):
        _, raw_key = store.register("agent_1", Role.TRADER)
        store.revoke("agent_1")
        assert store.authenticate(raw_key) is None

    def test_update_role(self, store):
        store.register("agent_1", Role.READONLY)
        store.update_role("agent_1", Role.TRADER)
        identity = store.get("agent_1")
        assert identity.role == Role.TRADER
        assert Permission.EXECUTE in identity.permissions

    def test_count(self, store):
        assert store.count() == 0
        store.register("a1", Role.TRADER)
        store.register("a2", Role.READONLY)
        assert store.count() == 2

    def test_key_verify_helper(self, store):
        _, raw_key = store.register("agent_1", Role.TRADER)
        identity = store.get("agent_1")
        assert _verify_key(raw_key, identity.key_hash)
        assert not _verify_key("wrong_key", identity.key_hash)


# ─────────────────────────────────────────── HTTP layer tests via TestClient

@pytest.fixture
def app_with_auth(tmp_path):
    """Returns a TestClient backed by an isolated AgentStore."""
    from api.app import app
    from auth.dependencies import _get_agent_store

    db = str(tmp_path / "test_auth.db")
    agent_store = AgentStore(db_path=db)

    # Override the auth store dependency used by all auth-gated routes
    app.dependency_overrides[_get_agent_store] = lambda: agent_store

    yield TestClient(app), agent_store

    app.dependency_overrides.pop(_get_agent_store, None)


def _headers(key: str) -> dict:
    return {"X-API-Key": key}


class TestBootstrap:

    def test_first_registration_open(self, app_with_auth):
        client, store = app_with_auth
        resp = client.post("/auth/agents", json={"agent_id": "admin", "role": "admin"})
        assert resp.status_code == 201
        assert "api_key" in resp.json()

    def test_second_registration_requires_admin_key(self, app_with_auth):
        client, store = app_with_auth
        # Register first admin
        r1 = client.post("/auth/agents", json={"agent_id": "admin", "role": "admin"})
        admin_key = r1.json()["api_key"]

        # Without key → 401
        r2 = client.post("/auth/agents/register", json={"agent_id": "trader1", "role": "trader"})
        assert r2.status_code == 401

        # With admin key → 201
        r3 = client.post(
            "/auth/agents/register",
            json={"agent_id": "trader1", "role": "trader"},
            headers=_headers(admin_key),
        )
        assert r3.status_code == 201

    def test_duplicate_agent_returns_409(self, app_with_auth):
        client, store = app_with_auth
        r1 = client.post("/auth/agents", json={"agent_id": "admin", "role": "admin"})
        admin_key = r1.json()["api_key"]

        client.post(
            "/auth/agents/register",
            json={"agent_id": "trader1", "role": "trader"},
            headers=_headers(admin_key),
        )
        r2 = client.post(
            "/auth/agents/register",
            json={"agent_id": "trader1", "role": "trader"},
            headers=_headers(admin_key),
        )
        assert r2.status_code == 409


class TestUnauthorizedRequests:

    def test_missing_key_returns_401(self, app_with_auth):
        client, _ = app_with_auth
        resp = client.get("/history")
        assert resp.status_code == 401

    def test_invalid_key_returns_401(self, app_with_auth):
        client, _ = app_with_auth
        resp = client.get("/history", headers=_headers("xclaw_fake_badkey"))
        assert resp.status_code == 401

    def test_wrong_prefix_returns_401(self, app_with_auth):
        client, _ = app_with_auth
        resp = client.get("/history", headers=_headers("Bearer sometoken"))
        assert resp.status_code == 401

    def test_revoked_key_returns_401(self, app_with_auth):
        client, store = app_with_auth
        _, key = store.register("agent_1", Role.READONLY)
        store.revoke("agent_1")
        resp = client.get("/history", headers=_headers(key))
        assert resp.status_code == 401


class TestPermissionEnforcement:

    def _setup(self, store):
        _, admin_key = store.register("admin", Role.ADMIN)
        _, trader_key = store.register("trader", Role.TRADER)
        _, approver_key = store.register("approver", Role.APPROVER)
        _, readonly_key = store.register("readonly", Role.READONLY)
        return admin_key, trader_key, approver_key, readonly_key

    def test_readonly_cannot_execute(self, app_with_auth):
        client, store = app_with_auth
        _, _, _, readonly_key = self._setup(store)
        resp = client.post(
            "/execute",
            json={"agent_id": "readonly", "wallet_id": "w_1", "side": "buy",
                  "asset": "BTC", "amount": "0.01"},
            headers=_headers(readonly_key),
        )
        assert resp.status_code == 403

    def test_readonly_cannot_approve(self, app_with_auth):
        client, store = app_with_auth
        _, _, _, readonly_key = self._setup(store)
        resp = client.post(
            "/approve",
            json={"request_id": "apr_123", "decision": "approve"},
            headers=_headers(readonly_key),
        )
        assert resp.status_code == 403

    def test_readonly_cannot_create_policy(self, app_with_auth):
        client, store = app_with_auth
        _, _, _, readonly_key = self._setup(store)
        resp = client.post(
            "/policies",
            json={"agent_id": "readonly", "name": "p", "rules": []},
            headers=_headers(readonly_key),
        )
        assert resp.status_code == 403

    def test_trader_cannot_approve(self, app_with_auth):
        client, store = app_with_auth
        _, trader_key, _, _ = self._setup(store)
        resp = client.post(
            "/approve",
            json={"request_id": "apr_123", "decision": "approve"},
            headers=_headers(trader_key),
        )
        assert resp.status_code == 403

    def test_approver_cannot_execute(self, app_with_auth):
        client, store = app_with_auth
        _, _, approver_key, _ = self._setup(store)
        resp = client.post(
            "/execute",
            json={"agent_id": "approver", "wallet_id": "w_1", "side": "buy",
                  "asset": "BTC", "amount": "0.01"},
            headers=_headers(approver_key),
        )
        assert resp.status_code == 403

    def test_readonly_can_read_history(self, app_with_auth):
        client, store = app_with_auth
        _, _, _, readonly_key = self._setup(store)
        resp = client.get("/history", headers=_headers(readonly_key))
        # 200 — has read permission, just gets empty results
        assert resp.status_code == 200

    def test_trader_cannot_register_new_agent(self, app_with_auth):
        client, store = app_with_auth
        _, trader_key, _, _ = self._setup(store)
        resp = client.post(
            "/auth/agents/register",
            json={"agent_id": "newagent", "role": "readonly"},
            headers=_headers(trader_key),
        )
        assert resp.status_code == 403

    def test_admin_can_register_new_agent(self, app_with_auth):
        client, store = app_with_auth
        admin_key, _, _, _ = self._setup(store)
        resp = client.post(
            "/auth/agents/register",
            json={"agent_id": "newagent", "role": "readonly"},
            headers=_headers(admin_key),
        )
        assert resp.status_code == 201

    def test_admin_can_list_all_agents(self, app_with_auth):
        client, store = app_with_auth
        admin_key, _, _, _ = self._setup(store)
        resp = client.get("/auth/agents", headers=_headers(admin_key))
        assert resp.status_code == 200
        assert resp.json()["count"] == 4

    def test_non_admin_cannot_list_all_agents(self, app_with_auth):
        client, store = app_with_auth
        _, trader_key, _, _ = self._setup(store)
        resp = client.get("/auth/agents", headers=_headers(trader_key))
        assert resp.status_code == 403


class TestAgentScoping:

    def _two_traders(self, store):
        _, key1 = store.register("trader1", Role.TRADER)
        _, key2 = store.register("trader2", Role.TRADER)
        return key1, key2

    def test_trader_cannot_execute_for_other_agent(self, app_with_auth):
        client, store = app_with_auth
        key1, _ = self._two_traders(store)
        resp = client.post(
            "/execute",
            json={"agent_id": "trader2", "wallet_id": "w_1", "side": "buy",
                  "asset": "BTC", "amount": "0.01"},
            headers=_headers(key1),
        )
        assert resp.status_code == 403
        assert "trader1" in resp.json()["detail"]

    def test_trader_cannot_read_other_agents_history(self, app_with_auth):
        client, store = app_with_auth
        key1, _ = self._two_traders(store)
        resp = client.get("/history?agent_id=trader2", headers=_headers(key1))
        assert resp.status_code == 403

    def test_trader_can_read_own_history(self, app_with_auth):
        client, store = app_with_auth
        key1, _ = self._two_traders(store)
        resp = client.get("/history?agent_id=trader1", headers=_headers(key1))
        assert resp.status_code == 200

    def test_trader_can_only_rotate_own_key(self, app_with_auth):
        client, store = app_with_auth
        key1, _ = self._two_traders(store)
        resp = client.post("/auth/agents/trader2/rotate", headers=_headers(key1))
        assert resp.status_code == 403

    def test_trader_can_rotate_own_key(self, app_with_auth):
        client, store = app_with_auth
        key1, _ = self._two_traders(store)
        resp = client.post("/auth/agents/trader1/rotate", headers=_headers(key1))
        assert resp.status_code == 200
        assert "api_key" in resp.json()

    def test_me_endpoint_returns_own_identity(self, app_with_auth):
        client, store = app_with_auth
        key1, _ = self._two_traders(store)
        resp = client.get("/auth/agents/me", headers=_headers(key1))
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == "trader1"

    def test_admin_can_query_any_agent_history(self, app_with_auth):
        client, store = app_with_auth
        _, admin_key = store.register("admin", Role.ADMIN)
        self._two_traders(store)
        resp = client.get("/history?agent_id=trader1", headers=_headers(admin_key))
        assert resp.status_code == 200
