"""Who may use /admin, and the audit trail of what they did.

Access = optional IP allowlist (`SAAS_ADMIN_IP_ALLOWLIST`, checked first, answers 404 so the panel's
existence is not advertised) + the core agent's `require_admin` (role admin + TOTP passed this
session) + a belt-and-braces re-check of role and status here. If `saas.auth.deps` cannot be
imported the panel fails closed (403 for everyone).

`AdminRoute` turns the core's ApiError / HTTPException raised by those dependencies into a JSON
envelope for /admin/api/* and a small HTML page (or the login redirect) for pages, whether or not
the app installed the core's error handlers.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from ..common import client_ip, utcnow
from ..config import settings
from ..db import get_db
from ..models.security import AuditLog
from ..security import netutil

log = logging.getLogger("saas.admin")


class AdminDenied(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _fallback_require_admin() -> Any:
    raise AdminDenied(403, "forbidden", "Admin sign-in is not available on this server.")


try:  # the core agent's dependency; tests override whichever object this ends up being
    from ..auth.deps import require_admin as require_admin  # noqa: PLC0414
except Exception:  # pragma: no cover - only before the core agent lands
    require_admin = _fallback_require_admin


def ip_gate(request: Request) -> None:
    nets = netutil.parse_network_list(settings.admin_ip_allowlist)
    if nets and not netutil.in_any(client_ip(request), nets):
        raise AdminDenied(404, "not_found", "Not found.")


def admin_user(request: Request, _gate: None = Depends(ip_gate), user: Any = Depends(require_admin)) -> Any:
    if user is None or getattr(user, "role", None) != "admin":
        raise AdminDenied(403, "forbidden", "Admins only.")
    if getattr(user, "status", "active") not in ("active", None):
        raise AdminDenied(403, "forbidden", "This admin account is not active.")
    request.state.admin = user
    return user


# ---- errors -------------------------------------------------------------------------------------
_DENIED_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Admin</title><link rel="stylesheet" href="/static/css/tokens.css">
<link rel="stylesheet" href="/admin-static/public.css"></head><body class="pub"><main class="pub-card">
<p class="pub-brand">Admin</p><h1>{title}</h1><p class="pub-lead">{message}</p>
{actions}</main></body></html>"""

# Where a denied admin goes next. Admin access is Google sign-in (or a password, where those are on) plus 2FA on
# the session, so the 2FA page offers both ways forward: prove the code on this session, or set 2FA up first.
# Neither sends a signed-in admin back to /login?next=/admin, which would bounce straight back here.
_ACTIONS = {
    "mfa_required": ('<p><a class="pub-btn" href="/login?stepup=1&amp;next=/admin">Enter my 2FA code</a></p>'
                     '<p><a href="/app/settings#mfa-title">No 2FA yet? Turn it on in Settings</a>, then come back.</p>'),
}
_DEFAULT_ACTIONS = ('<p><a class="pub-btn" href="/login?next=/admin">Sign in</a></p>'
                    '<p><a href="/app">Back to the app</a></p>')

_denied_seen: dict[str, float] = {}
_denied_lock = threading.Lock()


def _note_denied(request: Request, status: int, code: str) -> None:
    ip = client_ip(request)
    now = time.time()
    with _denied_lock:
        if _denied_seen.get(ip, 0) > now - 600:
            return
        _denied_seen[ip] = now
        if len(_denied_seen) > 5000:
            _denied_seen.clear()
    try:
        from ..db import session_scope
        from ..security import events

        with session_scope() as db:
            events.log(db, "admin_denied", "medium", ip=ip, path=request.url.path,
                       detail={"status": status, "code": code})
    except Exception:
        pass


def denied_response(request: Request, status: int, code: str, message: str) -> Response:
    if status in (401, 403):
        _note_denied(request, status, code)
    if request.url.path.startswith("/admin/api") or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"error": code, "message": message}, status_code=status)
    if status == 404:
        return HTMLResponse("Not found", status_code=404)
    if status == 401:
        return Response(status_code=303, headers={"Location": "/login?next=/admin"})
    import html

    title = "Two-factor needed" if code == "mfa_required" else "Admins only"
    return HTMLResponse(_DENIED_HTML.format(title=title, message=html.escape(message),
                                            actions=_ACTIONS.get(code, _DEFAULT_ACTIONS)), status_code=status)


class AdminRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except AdminDenied as exc:
                return denied_response(request, exc.status, exc.code, exc.message)
            except HTTPException as exc:
                if 300 <= exc.status_code < 400:
                    return Response(status_code=exc.status_code, headers=exc.headers)
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                return denied_response(request, exc.status_code, detail.get("error", "denied"),
                                       detail.get("message", str(exc.detail)))
            except Exception as exc:
                status, code = getattr(exc, "status", None), getattr(exc, "code", None)
                if isinstance(status, int) and isinstance(code, str) and 400 <= status < 500:
                    return denied_response(request, status, code, getattr(exc, "message", "") or code)
                raise

        return handler


# ---- audit --------------------------------------------------------------------------------------
def audit(db: Session, request: Request, action: str, *, target_kind: str | None = None,
          target_id: str | None = None, detail: dict | None = None, commit: bool = False) -> AuditLog:
    admin = getattr(request.state, "admin", None)
    row = AuditLog(actor_id=getattr(admin, "id", None), actor_email=getattr(admin, "email", None),
                   ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:512],
                   action=action[:64], target_kind=target_kind, target_id=(target_id or None) and str(target_id)[:160],
                   detail=detail, created_at=utcnow())
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return row


def get_admin_db(db: Session = Depends(get_db)) -> Session:
    return db
