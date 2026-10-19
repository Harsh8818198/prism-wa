"""API keys: made once, stored only as a hash, checked on every public call (spec WS14).

    pk_live_<32 hex characters>

The secret is returned by `create` and never again. `verify` hashes what a caller sends and looks the
hash up; a revoked key finds nothing. Scopes are the only authorisation a key carries, and the plan
decides which scopes can be granted at all (the template API is on every plan; the wider "project"
API is Pro and up).
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

from prism_wa import plans

PREFIX = "pk_live_"
SCOPES = {
    "template_messages:send": "template_api",       # the feature a plan needs before this scope can be granted
    "messages:send": "project_api",
    "messages:read": "project_api",
    "contacts:read": "project_api",
    "contacts:write": "project_api",
    "templates:read": "project_api",
    "campaigns:read": "project_api",
    "campaigns:write": "project_api",
}
DEFAULT_RATE = 600


class ApiKeyError(Exception):
    """A problem with a key request (not a missing dict key). `reason` is machine-readable."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Key:
    id: str
    tenant_id: str
    name: str
    scopes: tuple
    rate_limit_per_minute: int


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create(cur, tenant_id, name: str, scopes: list, *, created_by=None, rate_limit: int = DEFAULT_RATE) -> dict:
    """Make a key. Returns {"id", "secret", ...}; the secret is shown ONCE."""
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise ApiKeyError("bad_name", "give the key a name of 1 to 80 characters")
    scopes = sorted(set(scopes or []))
    if not scopes:
        raise ApiKeyError("no_scopes", "choose what this key may do")
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise ApiKeyError("bad_scope", "unknown permission: %s" % ", ".join(unknown))
    for scope in scopes:
        plans.require(cur, tenant_id, SCOPES[scope], "The API permission %r" % scope)
    secret = PREFIX + secrets.token_hex(16)
    cur.execute("insert into api_keys (tenant_id, name, prefix, key_hash, scopes, rate_limit_per_minute, created_by)"
                " values (%s, %s, %s, %s, %s, %s, %s) returning id",
                (tenant_id, name, secret[:len(PREFIX) + 4], _hash(secret), scopes, rate_limit, created_by))
    return {"id": str(cur.fetchone()[0]), "secret": secret, "name": name, "scopes": scopes, "prefix": secret[:len(PREFIX) + 4]}


def list_keys(cur, tenant_id) -> list:
    cur.execute("select id, name, prefix, scopes, rate_limit_per_minute, created_at, last_used_at, revoked_at"
                " from api_keys where tenant_id = %s order by created_at desc", (tenant_id,))
    cols = ("id", "name", "prefix", "scopes", "rate_limit_per_minute", "created_at", "last_used_at", "revoked_at")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def revoke(cur, tenant_id, key_id) -> bool:
    cur.execute("update api_keys set revoked_at = now() where id = %s and tenant_id = %s and revoked_at is null",
                (key_id, tenant_id))
    return cur.rowcount > 0


def verify(cur, secret: str) -> Optional[Key]:
    """The key this secret belongs to, or None (unknown, malformed or revoked)."""
    if not (secret or "").startswith(PREFIX) or len(secret) > 80:
        return None
    cur.execute("select id, tenant_id, name, scopes, rate_limit_per_minute from api_keys"
                " where key_hash = %s and revoked_at is null", (_hash(secret),))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("update api_keys set last_used_at = now() where id = %s", (row[0],))
    return Key(str(row[0]), str(row[1]), row[2], tuple(row[3]), row[4])
