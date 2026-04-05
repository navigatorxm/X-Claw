"""Auth domain models — agent identity, roles, and permissions."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Permission(str, Enum):
    """Atomic capabilities an agent can hold."""
    EXECUTE  = "execute"    # submit trades
    APPROVE  = "approve"    # approve / reject pending requests
    READ     = "read"       # query history, policies, balances, risk
    ADMIN    = "admin"      # register agents, manage policies, configure risk


class Role(str, Enum):
    """Named bundles of permissions."""
    ADMIN    = "admin"
    TRADER   = "trader"
    APPROVER = "approver"
    READONLY = "readonly"


# Default permission sets per role
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN:    frozenset({Permission.EXECUTE, Permission.APPROVE,
                              Permission.READ, Permission.ADMIN}),
    Role.TRADER:   frozenset({Permission.EXECUTE, Permission.READ}),
    Role.APPROVER: frozenset({Permission.APPROVE, Permission.READ}),
    Role.READONLY: frozenset({Permission.READ}),
}


@dataclass
class AgentIdentity:
    """
    Authenticated agent record — stored in the auth DB.

    The raw API key is NEVER stored here. Only the SHA-256 hash is persisted.
    The raw key is returned once at registration and is irrecoverable after that.
    """
    agent_id: str
    role: Role
    permissions: frozenset[Permission]
    key_hash: str               # SHA-256(raw_api_key) — hex digest
    key_prefix: str             # first 8 chars of raw key for display / debugging
    active: bool = True
    simulation: bool = False    # True → agent may only trade simulation wallets
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None

    def has_permission(self, perm: Permission) -> bool:
        return perm in self.permissions

    def is_admin(self) -> bool:
        return Permission.ADMIN in self.permissions

    def to_dict(self, include_hash: bool = False) -> dict:
        d: dict = {
            "agent_id": self.agent_id,
            "role": self.role.value,
            "permissions": sorted(p.value for p in self.permissions),
            "key_prefix": self.key_prefix,
            "active": self.active,
            "simulation": self.simulation,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }
        if include_hash:
            d["key_hash"] = self.key_hash
        return d
