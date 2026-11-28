"""FastAPI dependencies for authentication and authorisation (see docs/saas/CONTRACT.md).

    user: User = Depends(require_user)
    principal: ApiPrincipal = Depends(require_api_key("jobs:write"))

`require_user` answers 401 JSON on /api, /auth and /admin JSON paths and a 303 to /login?next= for
pages. Every dependency resolves the session once per request and caches it on request.state.
"""
from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models.core import AuthSession, User
from . import keys as keys_mod
from . import sessions
from .errors import ApiError
from .keys import ApiPrincipal

__all__ = ["optional_user", "require_user", "require_verified_user", "require_admin", "require_recent_auth",
           "require_api_key", "current_session", "ApiPrincipal", "get_db"]

_JSON_PREFIXES = ("/api/", "/auth/", "/webhooks/", "/mcp")


def _wants_json(request: Request) -> bool:
    path = request.url.path
    if path.startswith(_JSON_PREFIXES):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _resolve(request: Request, db: Session) -> Optional[tuple[AuthSession, User]]:
    if hasattr(request.state, "auth_resolved"):
        return request.state.auth_resolved
    found = sessions.from_request(db, request)
    request.state.auth_resolved = found
    request.state.auth_session = found[0] if found else None
    request.state.user = found[1] if found else None
    return found


def current_session(request: Request) -> Optional[AuthSession]:
    return getattr(request.state, "auth_session", None)


def optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    found = _resolve(request, db)
    return found[1] if found else None


def _unauthenticated(request: Request) -> Exception:
    if _wants_json(request):
        return ApiError(401, "unauthenticated", "Sign in to continue.")
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return HTTPException(status_code=303, headers={"Location": f"/login?next={quote(target, safe='')}"})


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    found = _resolve(request, db)
    if not found:
        raise _unauthenticated(request)
    return found[1]


def require_verified_user(user: User = Depends(require_user)) -> User:
    if user.email_verified_at is None:
        from . import policy

        if policy.google_only():
            raise ApiError(403, "email_unverified", "Sign in with Google once to confirm your email address.")
        raise ApiError(403, "email_unverified", "Confirm your email address first. Check your inbox for the link.")
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    from . import totp

    found = _resolve(request, db)
    if not found:
        raise _unauthenticated(request)
    sess, user = found
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Admins only.")
    if not totp.is_enabled(db, user.id) or sess.mfa_at is None:
        raise ApiError(403, "mfa_required", "Admin access needs two-factor authentication on this session.")
    return user


def require_recent_auth(request: Request, db: Session = Depends(get_db)) -> AuthSession:
    found = _resolve(request, db)
    if not found:
        raise _unauthenticated(request)
    sess, _user = found
    if not sessions.recently_authenticated(sess):
        raise ApiError(403, "reauth_required", "Confirm it's you: enter your password (or 2FA code) again.")
    return sess


def require_api_key(*scopes: str) -> Callable[..., ApiPrincipal]:
    def dependency(request: Request, db: Session = Depends(get_db)) -> ApiPrincipal:
        raw = keys_mod.bearer_token(request.headers.get("authorization"))
        principal = keys_mod.authenticate(db, raw, request, scopes)
        request.state.api_principal = principal
        return principal

    return dependency
