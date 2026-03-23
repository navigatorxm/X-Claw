"""
Agent auth store — SQLite-backed identity and API key management.

Security properties:
- Raw API keys are NEVER written to disk.
- Only SHA-256(raw_key) is stored, as a hex digest.
- Key comparison uses hmac.compare_digest to prevent timing attacks.
- Key format: xclaw_<agent_id>_<64 random hex chars>
  (64 hex = 256 bits of entropy — brute-force is computationally infeasible)
"""
from __future__ import annotations
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import AgentIdentity, Permission, Role, ROLE_PERMISSIONS


def _generate_raw_key(agent_id: str) -> str:
    """Generate a new raw API key. Call once; the result is not recoverable."""
    return f"xclaw_{agent_id}_{secrets.token_hex(32)}"


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _verify_key(raw_key: str, stored_hash: str) -> bool:
    """Constant-time comparison — prevents timing side-channel attacks."""
    return hmac.compare_digest(_hash_key(raw_key), stored_hash)


class AgentStore:
    """
    Manages agent identities and API key lifecycle.

    Bootstrap rule:
      When zero agents exist, any caller may register the first agent
      (so an initial admin can be created without needing a key).
      After that, registration requires an existing admin key.
    """

    def __init__(self, db_path: str = "memory/finance.db") -> None:
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_identities (
                    agent_id        TEXT PRIMARY KEY,
                    role            TEXT NOT NULL,
                    permissions     TEXT NOT NULL,
                    key_hash        TEXT NOT NULL UNIQUE,
                    key_prefix      TEXT NOT NULL,
                    active          INTEGER NOT NULL DEFAULT 1,
                    created_at      TEXT NOT NULL,
                    last_used_at    TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_identity_hash ON agent_identities(key_hash)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------------------------------------------------------- register
    def register(
        self,
        agent_id: str,
        role: Role,
        custom_permissions: Optional[frozenset[Permission]] = None,
    ) -> tuple[AgentIdentity, str]:
        """
        Create a new agent identity.

        Returns (AgentIdentity, raw_api_key). The raw key is shown ONCE — it
        cannot be retrieved again. Store it securely on the caller's side.

        Raises ValueError if agent_id already exists.
        """
        if self.get(agent_id) is not None:
            raise ValueError(f"Agent '{agent_id}' already registered.")

        perms = custom_permissions or ROLE_PERMISSIONS[role]
        raw_key = _generate_raw_key(agent_id)
        key_hash = _hash_key(raw_key)
        now = datetime.utcnow()

        identity = AgentIdentity(
            agent_id=agent_id,
            role=role,
            permissions=perms,
            key_hash=key_hash,
            key_prefix=raw_key[:12],
            created_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO agent_identities
                   (agent_id, role, permissions, key_hash, key_prefix, active, created_at)
                   VALUES (?,?,?,?,?,1,?)""",
                (
                    agent_id,
                    role.value,
                    json.dumps(sorted(p.value for p in perms)),
                    key_hash,
                    identity.key_prefix,
                    now.isoformat(),
                ),
            )
        return identity, raw_key

    # -------------------------------------------------------------- authenticate
    def authenticate(self, raw_key: str) -> Optional[AgentIdentity]:
        """
        Validate a raw API key and return the identity if valid and active.
        Updates last_used_at on success.
        Returns None on any failure — deliberately no detail on why.
        """
        if not raw_key or not raw_key.startswith("xclaw_"):
            return None

        candidate_hash = _hash_key(raw_key)
        now = datetime.utcnow().isoformat()

        with self._connect() as conn:
            # Fetch all active agents and compare hashes in constant time.
            # We intentionally do NOT filter by hash in SQL to avoid leaking
            # timing information about whether a prefix exists.
            rows = conn.execute(
                "SELECT * FROM agent_identities WHERE active = 1"
            ).fetchall()

        matched = None
        for row in rows:
            if hmac.compare_digest(candidate_hash, row["key_hash"]):
                matched = row
                break

        if matched is None:
            return None

        # Update last_used_at
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_identities SET last_used_at = ? WHERE agent_id = ?",
                (now, matched["agent_id"]),
            )

        return self._row_to_identity(matched)

    # ------------------------------------------------------------------- CRUD
    def get(self, agent_id: str) -> Optional[AgentIdentity]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_identities WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._row_to_identity(row) if row else None

    def list_all(self) -> list[AgentIdentity]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_identities ORDER BY created_at"
            ).fetchall()
        return [self._row_to_identity(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM agent_identities").fetchone()[0]

    def rotate_key(self, agent_id: str) -> tuple[AgentIdentity, str]:
        """
        Invalidate the existing key and issue a new one.
        Returns (updated identity, new raw key).
        """
        identity = self.get(agent_id)
        if identity is None:
            raise ValueError(f"Agent '{agent_id}' not found.")

        raw_key = _generate_raw_key(agent_id)
        key_hash = _hash_key(raw_key)
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_identities SET key_hash = ?, key_prefix = ? WHERE agent_id = ?",
                (key_hash, raw_key[:12], agent_id),
            )
        identity.key_hash = key_hash
        identity.key_prefix = raw_key[:12]
        return identity, raw_key

    def update_role(
        self,
        agent_id: str,
        role: Role,
        custom_permissions: Optional[frozenset[Permission]] = None,
    ) -> AgentIdentity:
        identity = self.get(agent_id)
        if identity is None:
            raise ValueError(f"Agent '{agent_id}' not found.")
        perms = custom_permissions or ROLE_PERMISSIONS[role]
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_identities SET role = ?, permissions = ? WHERE agent_id = ?",
                (role.value, json.dumps(sorted(p.value for p in perms)), agent_id),
            )
        identity.role = role
        identity.permissions = perms
        return identity

    def revoke(self, agent_id: str) -> None:
        """Deactivate an agent — their key will no longer authenticate."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_identities SET active = 0 WHERE agent_id = ?", (agent_id,)
            )

    # ------------------------------------------------------------ deserialise
    def _row_to_identity(self, row: sqlite3.Row) -> AgentIdentity:
        perms = frozenset(Permission(p) for p in json.loads(row["permissions"]))
        return AgentIdentity(
            agent_id=row["agent_id"],
            role=Role(row["role"]),
            permissions=perms,
            key_hash=row["key_hash"],
            key_prefix=row["key_prefix"],
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used_at=(
                datetime.fromisoformat(row["last_used_at"])
                if row["last_used_at"] else None
            ),
        )
