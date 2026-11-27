"""API keys: `dsk_live_<40 random chars>`. Shown once; stored as SHA-256; scoped; revocable.

`authenticate` is shared by /api/v1 (FastAPI dependency) and /mcp (ASGI wrapper), and re-checks on
every request that the key is live, the user is active and their plan still includes API access, then
applies the per-key rate limit.
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional

from fastapi import Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..common import client_ip, utcnow
from ..config import settings
from ..models.core import ApiKey, User
from . import compat
from .crypto import sha256_hex
from .errors import ApiError

KEY_PREFIX = "dsk_live_"
ALL_SCOPES = ("jobs:read", "jobs:write", "clips:read", "clips:write", "billing:read")
MAX_KEYS_PER_USER = 10
_ALPHABET = string.ascii_letters + string.digits
_TOUCH_EVERY = timedelta(seconds=60)


@dataclass
class ApiPrincipal:
    user: User
    key: ApiKey

    @property
    def user_id(self) -> str:
        return self.user.id

    def has(self, scope: str) -> bool:
        return scope in (self.key.scopes or [])


def _generate() -> str:
    return KEY_PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(40))


def plan_allows_api(db: Session, user: User) -> bool:
    from ..billing import catalog

    plan = catalog.plan_for_user(db, user)
    return bool(plan and (plan.features or {}).get("api"))


def create(db: Session, user: User, name: str, scopes: Optional[Iterable[str]] = None) -> tuple[ApiKey, str]:
    wanted = list(dict.fromkeys(scopes)) if scopes else list(ALL_SCOPES)
    bad = [s for s in wanted if s not in ALL_SCOPES]
    if bad:
        raise ApiError(400, "invalid_scope", f"Unknown scope: {bad[0]}.", allowed=list(ALL_SCOPES))
    live = db.query(ApiKey).filter(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None)).count()
    if live >= MAX_KEYS_PER_USER:
        raise ApiError(409, "too_many_keys", f"You can have at most {MAX_KEYS_PER_USER} active keys. Revoke one first.")
    raw = _generate()
    key = ApiKey(user_id=user.id, name=(name or "API key").strip()[:80], prefix=raw[: len(KEY_PREFIX) + 6],
                 secret_hash=sha256_hex(raw), scopes=wanted)
    db.add(key)
    db.flush()
    return key, raw


def public(key: ApiKey) -> dict:
    return {"id": key.id, "name": key.name, "prefix": key.prefix + "…", "scopes": list(key.scopes or []),
            "created_at": key.created_at.isoformat(),
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
            "revoked": key.revoked_at is not None}


def revoke(db: Session, user: User, key_id: str) -> bool:
    res = db.execute(update(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
                     .values(revoked_at=utcnow()))
    return res.rowcount == 1


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def authenticate(db: Session, raw: Optional[str], request: Optional[Request], scopes: Iterable[str] = ()) -> ApiPrincipal:
    invalid = ApiError(401, "invalid_api_key", "Missing or invalid API key. Send 'Authorization: Bearer dsk_live_...'.",
                       headers={"WWW-Authenticate": 'Bearer realm="api"'})
    if not raw or not raw.startswith(KEY_PREFIX) or len(raw) > 100:
        if request is not None:
            compat.rate_limit(request, "api_bad_key", 30, 60)  # slows key guessing from one IP
        raise invalid
    key = db.query(ApiKey).filter(ApiKey.secret_hash == sha256_hex(raw)).one_or_none()
    now = utcnow()
    if key is None or key.revoked_at is not None or (key.expires_at is not None and key.expires_at <= now):
        if request is not None:
            compat.rate_limit(request, "api_bad_key", 30, 60)
        raise invalid
    user = db.get(User, key.user_id)
    if user is None or user.status != "active":
        raise invalid
    if not plan_allows_api(db, user):
        raise ApiError(403, "plan_required", "API and MCP access come with any paid plan.")
    missing = [s for s in scopes if s not in (key.scopes or [])]
    if missing:
        raise ApiError(403, "insufficient_scope", f"This key lacks the '{missing[0]}' scope.", required=missing)
    compat.rate_limit(request, "api_key", settings.api_rate_per_minute, 60, key=f"key:{key.id}")
    if key.last_used_at is None or now - key.last_used_at >= _TOUCH_EVERY:
        ip = client_ip(request) if request is not None else None
        db.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now, last_used_ip=ip))
        db.commit()
        key.last_used_at = now
    return ApiPrincipal(user=user, key=key)
