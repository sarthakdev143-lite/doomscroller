"""Cloudflare Turnstile server-side verification.

`verify` returns True when no secret is configured (dev, tests), as the contract says. With a secret
it fails closed: a missing token, a network error or a hostname mismatch all return False.
"""
from __future__ import annotations

import logging
import uuid
from urllib.parse import urlparse

import httpx

from ..config import settings

log = logging.getLogger("saas.security.turnstile")

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def enabled() -> bool:
    return bool(settings.turnstile_secret_key)


def verify(token: str | None, ip: str, *, action: str | None = None, client: httpx.Client | None = None) -> bool:
    if not settings.turnstile_secret_key:
        return True
    if not token or len(token) > 2048:
        return False
    data = {
        "secret": settings.turnstile_secret_key,
        "response": token,
        # Lets Cloudflare dedupe a retried verification of the same token.
        "idempotency_key": str(uuid.uuid4()),
    }
    if ip and ip != "testclient":
        data["remoteip"] = ip
    try:
        if client is not None:
            resp = client.post(VERIFY_URL, data=data, timeout=8.0)
        else:
            with httpx.Client(timeout=8.0) as c:
                resp = c.post(VERIFY_URL, data=data)
        body = resp.json()
    except Exception as exc:
        # Never log the secret or the token; the exception type is enough to diagnose.
        log.warning("turnstile verification unavailable: %s", type(exc).__name__)
        return False
    if not body.get("success"):
        log.info("turnstile rejected: %s", body.get("error-codes"))
        return False
    expected_host = urlparse(settings.public_url).hostname
    host = body.get("hostname")
    if settings.is_prod and expected_host and host and host != expected_host:
        log.warning("turnstile token minted for another hostname")
        return False
    if action and body.get("action") and body.get("action") != action:
        return False
    return True
