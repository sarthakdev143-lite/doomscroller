"""Server-side sessions behind the `sid` cookie.

The cookie holds 256 bits of randomness; the table holds only its SHA-256, so a database leak yields
no usable sessions. A session dies at whichever comes first: revocation, the absolute lifetime
(SAAS_SESSION_DAYS) or the idle limit (SAAS_SESSION_IDLE_HOURS, much shorter for admins).

Fixation: a session is only ever created by the server after authentication, and login revokes any
session the browser already presented. Privilege changes (TOTP step-up, password change) rotate the
token so a token captured before the change stops working.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import Request, Response
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..common import client_country, client_ip, utcnow
from ..config import settings
from ..models.core import AuthSession, User
from .crypto import random_token, sha256_hex

COOKIE = "sid"
_TOUCH_EVERY = timedelta(seconds=60)


def idle_limit(user: User) -> timedelta:
    if user.role == "admin":
        return timedelta(minutes=settings.admin_session_idle_minutes)
    return timedelta(hours=settings.session_idle_hours)


def create(db: Session, user: User, request: Optional[Request], *, method: str, mfa: bool = False) -> tuple[AuthSession, str]:
    raw = random_token(32)
    now = utcnow()
    sess = AuthSession(
        user_id=user.id,
        token_hash=sha256_hex(raw),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=settings.session_days),
        ip=client_ip(request) if request is not None else None,
        user_agent=(request.headers.get("user-agent", "")[:400] if request is not None else None),
        country=client_country(request) if request is not None else None,
        method=method,
        mfa_at=now if mfa else None,
        reauth_at=now,  # a fresh login is a fresh authentication
    )
    db.add(sess)
    user.last_login_at = now
    db.flush()
    return sess, raw


def set_cookie(response: Response, raw: str, sess: AuthSession) -> None:
    max_age = max(60, int((sess.expires_at - utcnow()).total_seconds()))
    response.set_cookie(COOKIE, raw, max_age=max_age, httponly=True, samesite="lax",
                        secure=settings.is_prod, path="/")


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=settings.is_prod)


def lookup(db: Session, raw: Optional[str]) -> Optional[tuple[AuthSession, User]]:
    if not raw or len(raw) > 200:
        return None
    sess = db.query(AuthSession).filter(AuthSession.token_hash == sha256_hex(raw)).one_or_none()
    if sess is None or sess.revoked_at is not None:
        return None
    now = utcnow()
    if sess.expires_at <= now:
        return None
    user = db.get(User, sess.user_id)
    if user is None or user.status != "active":
        return None
    if sess.last_seen_at + idle_limit(user) <= now:
        return None
    if now - sess.last_seen_at >= _TOUCH_EVERY:
        db.execute(update(AuthSession).where(AuthSession.id == sess.id).values(last_seen_at=now))
        db.commit()
        sess.last_seen_at = now
    return sess, user


def from_request(db: Session, request: Request) -> Optional[tuple[AuthSession, User]]:
    return lookup(db, request.cookies.get(COOKIE))


def rotate(db: Session, sess: AuthSession) -> str:
    raw = random_token(32)
    sess.token_hash = sha256_hex(raw)
    db.flush()
    return raw


def revoke(db: Session, sess: AuthSession, reason: str) -> None:
    if sess.revoked_at is None:
        sess.revoked_at = utcnow()
        sess.revoked_reason = reason[:40]
        db.flush()


def revoke_all(db: Session, user_id: str, *, reason: str, except_id: Optional[str] = None) -> int:
    q = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if except_id:
        q = q.where(AuthSession.id != except_id)
    res = db.execute(q.values(revoked_at=utcnow(), revoked_reason=reason[:40]))
    return int(res.rowcount or 0)


def revoke_presented(db: Session, request: Request, reason: str = "replaced") -> None:
    """Revoke whatever session the browser presented (login/logout), so no old token survives."""
    raw = request.cookies.get(COOKIE)
    if not raw or len(raw) > 200:
        return
    db.execute(update(AuthSession).where(AuthSession.token_hash == sha256_hex(raw), AuthSession.revoked_at.is_(None))
               .values(revoked_at=utcnow(), revoked_reason=reason))


def recently_authenticated(sess: AuthSession) -> bool:
    return sess.reauth_at is not None and utcnow() - sess.reauth_at <= timedelta(minutes=settings.reauth_minutes)
