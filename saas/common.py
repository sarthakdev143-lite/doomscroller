"""Small helpers every saas module shares. Integrator-owned; keep it dependency-free."""
from __future__ import annotations

import ipaddress
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import Request

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    """Opaque, unguessable, URL-safe id. Never expose autoincrement ids (enumeration)."""
    return f"{prefix}{uuid.uuid4().hex}"


def token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _valid_ip(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """The caller's IP, trusting proxy headers only when SAAS_TRUST_PROXY says the proxy exists.

    Trusting X-Forwarded-For from the open internet lets any client pick its own IP and walk
    past every per-IP limit, ban and fingerprint cluster, so the default trusts nothing.
    """
    mode = settings.trust_proxy.lower()
    if mode == "cloudflare":
        ip = _valid_ip(request.headers.get("cf-connecting-ip"))
        if ip:
            return ip
    elif mode == "forwarded":
        xff = request.headers.get("x-forwarded-for", "")
        # The right-most entry was appended by our own proxy; earlier ones are client-supplied.
        parts = [p for p in (s.strip() for s in xff.split(",")) if p]
        ip = _valid_ip(parts[-1]) if parts else None
        if ip:
            return ip
    return (request.client.host if request.client else None) or "0.0.0.0"


def client_country(request: Request) -> str | None:
    """Cloudflare's country header, when behind Cloudflare. GeoIP enrichment lives in saas.tracking."""
    if settings.trust_proxy.lower() == "cloudflare":
        cc = (request.headers.get("cf-ipcountry") or "").strip().upper()
        if len(cc) == 2 and cc not in ("XX", "T1"):
            return cc
    return None
