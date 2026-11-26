"""Google sign-in: OpenID Connect authorization-code flow with PKCE (S256), state and nonce. httpx only.

* `state` is random, stored hashed with the PKCE verifier and nonce, single-use, 10-minute TTL, and also
  set in an HttpOnly cookie so the callback must come back to the browser that started it (login CSRF).
* The ID token comes straight from Google's token endpoint over TLS in exchange for our client secret
  and the PKCE verifier, so per OIDC Core 3.1.3.7 TLS authenticates the issuer and we validate its
  claims (iss, aud/azp, exp, iat, nonce, email_verified) rather than fetching JWKS to check the RS256
  signature.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..common import utcnow
from ..config import settings
from ..models.core import OAuthState
from .crypto import random_token, sha256_hex
from .errors import ApiError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
STATE_COOKIE = "g_state"
_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
STATE_TTL = timedelta(minutes=10)


def enabled() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def redirect_uri() -> str:
    return settings.public_url.rstrip("/") + "/auth/google/callback"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def start(db: Session, *, next_path: str, data: dict) -> tuple[str, str]:
    """(authorization URL, raw state for the binding cookie)."""
    if not enabled():
        raise ApiError(404, "google_disabled", "Google sign-in is not available.")
    state = random_token(32)
    verifier = random_token(64)[:96]
    nonce = random_token(24)
    now = utcnow()
    db.add(OAuthState(state_hash=sha256_hex(state), code_verifier=verifier, nonce=nonce, next_path=next_path,
                      data=data, created_at=now, expires_at=now + STATE_TTL))
    db.flush()
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def consume_state(db: Session, state: Optional[str], cookie_state: Optional[str]) -> OAuthState:
    if not state or not cookie_state or len(state) > 200 or not hmac.compare_digest(state, cookie_state):
        raise ApiError(400, "oauth_state", "That sign-in attempt expired. Start again.")
    row = db.query(OAuthState).filter(OAuthState.state_hash == sha256_hex(state)).one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        raise ApiError(400, "oauth_state", "That sign-in attempt expired. Start again.")
    res = db.execute(update(OAuthState).where(OAuthState.id == row.id, OAuthState.used_at.is_(None))
                     .values(used_at=utcnow()))
    if res.rowcount != 1:
        raise ApiError(400, "oauth_state", "That sign-in attempt expired. Start again.")
    return row


def _decode_segment(seg: str) -> dict:
    pad = "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg + pad).decode("utf-8"))


def validate_id_token(id_token: str, *, nonce: str, now: Optional[float] = None) -> dict:
    try:
        header_seg, payload_seg, _sig = id_token.split(".")
        _decode_segment(header_seg)
        claims = _decode_segment(payload_seg)
    except Exception:
        raise ApiError(400, "oauth_token", "Google returned an unreadable sign-in token.")
    now = time.time() if now is None else now
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    problems = []
    if claims.get("iss") not in _ISSUERS:
        problems.append("iss")
    if settings.google_client_id not in auds:
        problems.append("aud")
    if isinstance(aud, list) and len(auds) > 1 and claims.get("azp") != settings.google_client_id:
        problems.append("azp")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60:
        problems.append("exp")
    if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + 300:
        problems.append("iat")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        problems.append("nonce")
    if not claims.get("sub"):
        problems.append("sub")
    if problems:
        raise ApiError(400, "oauth_token", "Google's sign-in token did not check out. Start again.",
                       problems=problems)
    if claims.get("email_verified") not in (True, "true") or not claims.get("email"):
        raise ApiError(400, "oauth_unverified_email", "Your Google account's email is not verified.")
    return claims


def exchange_code(code: str, verifier: str, *, client: Optional[httpx.Client] = None) -> dict:
    if not code or len(code) > 2048:
        raise ApiError(400, "oauth_code", "Google did not return a sign-in code.")
    own = client is None
    c = client or httpx.Client(timeout=10.0)
    try:
        r = c.post(TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri(),
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        raise ApiError(502, "oauth_unreachable", "Couldn't reach Google. Try again.")
    finally:
        if own:
            c.close()
    if r.status_code != 200:
        raise ApiError(400, "oauth_exchange", "Google refused the sign-in. Start again.")
    try:
        body = r.json()
    except ValueError:
        raise ApiError(502, "oauth_exchange", "Google returned an unreadable response.")
    if not isinstance(body, dict) or not body.get("id_token"):
        raise ApiError(400, "oauth_exchange", "Google did not return an identity.")
    return body
