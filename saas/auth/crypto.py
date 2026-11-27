"""Key derivation, hashing and sealing. Everything keyed derives from SAAS_SECRET_KEY, per purpose.

* `sha256_hex` stores high-entropy random secrets (session tokens, email tokens, API keys, recovery
  codes). They carry 128+ bits of entropy, so a fast hash is the right tool: there is nothing to brute
  force, and lookups stay one indexed equality.
* `sign` / `verify_sig` are HMACs for short-lived signed URLs and redirects.
* `seal` / `unseal` encrypt small secrets at rest (the TOTP seed) with AES-256-GCM. This uses the
  `cryptography` package, which is in the venv (a google-auth dependency) but should be pinned in
  requirements-saas.txt: the standard library has no AEAD.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from functools import lru_cache

from ..config import settings

log = logging.getLogger("saas.auth.crypto")

_EPHEMERAL = secrets.token_bytes(32)
_warned_ephemeral = False
_MIN_PROD_SECRET = 32


class SecretKeyMissing(RuntimeError):
    pass


def master_key() -> bytes:
    raw = (settings.secret_key or "").encode("utf-8")
    if settings.is_prod and len(raw) < _MIN_PROD_SECRET:
        raise SecretKeyMissing("SAAS_SECRET_KEY must be set (32+ characters) when SAAS_ENV=prod")
    if not raw:
        global _warned_ephemeral
        if not _warned_ephemeral:
            _warned_ephemeral = True
            log.warning("SAAS_SECRET_KEY is empty: using an ephemeral key (sessions and 2FA die on restart)")
        return _EPHEMERAL
    return raw


@lru_cache(maxsize=32)
def _derive_cached(master: bytes, purpose: str) -> bytes:
    # HKDF-style extract-then-expand with HMAC-SHA256; the master is already high entropy.
    prk = hmac.new(b"doomscroller-saas-kdf-v1", master, hashlib.sha256).digest()
    return hmac.new(prk, purpose.encode("utf-8") + b"\x01", hashlib.sha256).digest()


def derive_key(purpose: str) -> bytes:
    return _derive_cached(master_key(), purpose)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sign(purpose: str, message: str) -> str:
    mac = hmac.new(derive_key(f"sign:{purpose}"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:24]).decode("ascii").rstrip("=")


def verify_sig(purpose: str, message: str, signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign(purpose, message), signature)


def seal(purpose: str, plaintext: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(derive_key(f"seal:{purpose}")).encrypt(nonce, plaintext.encode("utf-8"), purpose.encode("utf-8"))
    return "v1." + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def unseal(purpose: str, sealed: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not sealed.startswith("v1."):
        raise ValueError("unknown sealed format")
    blob = base64.urlsafe_b64decode(sealed[3:].encode("ascii"))
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(derive_key(f"seal:{purpose}")).decrypt(nonce, ct, purpose.encode("utf-8")).decode("utf-8")


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
