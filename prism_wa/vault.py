"""Encrypting per-tenant Meta tokens at rest (spec WS17, defect 10).

Today's tokens sit in plaintext columns. A customer-scoped token can send as
that customer, so a leaked database must not leak them. Tokens are stored as
AES-256-GCM blobs:

    b"W1" | key version (1 byte) | nonce (12) | ciphertext+tag

The key is NOT in the database. `WA_TOKEN_KEYS` holds `version:key` pairs
(base64, 32 bytes each, comma separated); the highest version encrypts, any
listed version decrypts, so a key can be rotated by adding a new one, running
`reencrypt` over the rows, and only then retiring the old one.

The blob is bound to its WABA (authenticated data), so copying one row's token
into another row makes it fail to decrypt instead of quietly working.
"""
from __future__ import annotations

import base64
import binascii
import os
from typing import Dict, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"W1"
NONCE_LEN = 12


class VaultError(Exception):
    """A token could not be encrypted or decrypted (wrong key, tampering, bad format)."""


def generate_key() -> str:
    """A fresh 32-byte key as base64, for WA_TOKEN_KEYS."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _decode_key(text: str) -> bytes:
    text = text.strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise VaultError("a token key is not valid base64") from exc
    if len(raw) != 32:
        raise VaultError("a token key must be 32 bytes (AES-256)")
    return raw


class Vault:
    def __init__(self, keys: Dict[int, bytes], current: Optional[int] = None):
        if not keys:
            raise VaultError("no token key configured")
        for ver, key in keys.items():
            if not 1 <= ver <= 255 or len(key) != 32:
                raise VaultError("key versions are 1-255 and keys are 32 bytes")
        self._keys = dict(keys)
        self.current = current if current is not None else max(keys)
        if self.current not in self._keys:
            raise VaultError("the current key version is not among the keys")

    @classmethod
    def from_env(cls, env=None) -> "Vault":
        env = os.environ if env is None else env
        spec = (env.get("WA_TOKEN_KEYS") or "").strip()
        if not spec:
            raise VaultError("WA_TOKEN_KEYS is not set (format: 1:<base64 key>[,2:<base64 key>])")
        keys = {}
        for part in spec.split(","):
            ver, _, key = part.strip().partition(":")
            if not ver.isdigit() or not key:
                raise VaultError("WA_TOKEN_KEYS entries look like 1:<base64 key>")
            keys[int(ver)] = _decode_key(key)
        current = env.get("WA_TOKEN_KEY_CURRENT")
        return cls(keys, int(current) if current else None)

    def encrypt(self, token: str, *, context: str) -> Tuple[bytes, int]:
        """-> (blob, key version). `context` is the WABA id the token belongs to."""
        nonce = os.urandom(NONCE_LEN)
        sealed = AESGCM(self._keys[self.current]).encrypt(nonce, token.encode("utf-8"),
                                                          context.encode("utf-8"))
        return MAGIC + bytes([self.current]) + nonce + sealed, self.current

    def decrypt(self, blob, *, context: str) -> str:
        blob = bytes(blob or b"")
        if len(blob) < len(MAGIC) + 1 + NONCE_LEN + 16 or not blob.startswith(MAGIC):
            raise VaultError("not an encrypted token")
        ver = blob[len(MAGIC)]
        key = self._keys.get(ver)
        if key is None:
            raise VaultError("this token was encrypted with key version %d, which is not configured" % ver)
        nonce = blob[len(MAGIC) + 1:len(MAGIC) + 1 + NONCE_LEN]
        try:
            return AESGCM(key).decrypt(nonce, blob[len(MAGIC) + 1 + NONCE_LEN:],
                                       context.encode("utf-8")).decode("utf-8")
        except InvalidTag as exc:
            raise VaultError("the token failed authentication (wrong key, or it was moved or altered)") from exc

    def reencrypt(self, blob, *, context: str) -> Tuple[bytes, int]:
        """The same token under the current key (for rotation)."""
        return self.encrypt(self.decrypt(blob, context=context), context=context)
