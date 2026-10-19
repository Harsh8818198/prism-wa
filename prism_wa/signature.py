"""X-Hub-Signature-256 — proof that a webhook really came from Meta.

Meta signs the RAW request body with the app secret (HMAC-SHA256) and sends
`sha256=<hex>`. Verify against the bytes as received, before any JSON parsing:
re-serialising a parsed body changes the bytes and the check would fail (or,
worse, be skipped to make it pass).

This closes spec defect 1: the n8n handler had no check, so anyone who learned
its URL could inject messages.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Iterable, Optional, Union

PREFIX = "sha256="


def sign(secret: str, body: bytes) -> str:
    """The header value Meta would send (tests and local tools use this)."""
    return PREFIX + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secrets: Union[str, Iterable[str]], body: bytes,
                     header: Optional[str]) -> bool:
    """True when `header` is the HMAC of `body` under any of `secrets`.

    Several secrets are accepted so the app secret can be rotated without a
    gap (old and new both valid while Meta is switched over). No secrets at all
    fails closed: an unconfigured gateway rejects everything rather than
    accepting unsigned events.
    """
    if isinstance(secrets, str):
        secrets = [secrets]
    usable = [s for s in secrets if s]
    if not usable or not header or not header.startswith(PREFIX):
        return False
    given = header[len(PREFIX):].strip().lower().encode("utf-8", "replace")
    ok = False
    for secret in usable:
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest().encode("ascii")
        # every secret is compared (no early exit), each in constant time
        ok = hmac.compare_digest(expected, given) or ok
    return ok
