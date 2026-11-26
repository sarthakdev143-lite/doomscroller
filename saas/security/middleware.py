"""The security layer every request passes through before any route runs.

Pure ASGI (not BaseHTTPMiddleware): it has to count streamed request bytes, replace a response
mid-flight and never buffer an upload. In order, per request:

1. Bans: IP / CIDR, ASN and visitor-cookie bans -> 403 (static assets skip this and step 2).
2. Global per-IP limits: `rate_limit_global_per_min` on everything, `rate_limit_write_per_min` on
   state-changing /api/* and /auth/* -> 429 with Retry-After.
3. Origin + CSRF on state-changing /api/*, /auth/*, /admin/*: a cross-site `Origin` is refused, and
   a request carrying the `sid` session cookie must send `X-CSRF-Token` equal to the `csrf` cookie
   (double submit). Exempt: /webhooks/*, /api/t and /api/t/* (beacons, origin-checked in the route),
   /api/v1/* and /mcp (API keys, no cookies).
4. Body caps per route class -> 413, enforced on Content-Length *and* on the bytes actually streamed.
5. Response headers: CSP with a per-request nonce (`request.state.csp_nonce`), frame-deny, nosniff,
   Referrer-Policy, Permissions-Policy, COOP/CORP, no-store on API/admin, HSTS in prod; and the
   `csrf` cookie on every HTML response that arrived without one (`request.state.csrf_token`).
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
import threading
import time
from urllib.parse import urlparse

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..common import client_ip
from ..config import settings
from . import bans, limits

log = logging.getLogger("saas.security.middleware")

CSRF_COOKIE = "csrf"
CSRF_HEADER = "x-csrf-token"
SESSION_COOKIE = "sid"
_WRITE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_STATIC_PREFIXES = ("/static/", "/ts/", "/admin-static/", "/favicon", "/robots.txt", "/manifest")
_CSRF_SCOPES = ("/api/", "/auth/", "/admin/")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
TURNSTILE = "https://challenges.cloudflare.com"


class _TooLarge(Exception):
    pass


# ---- route classes ------------------------------------------------------------------------------
def is_static(path: str) -> bool:
    return path.startswith(_STATIC_PREFIXES)


def is_beacon(path: str) -> bool:
    return path == "/api/t" or path.startswith("/api/t/")


def csrf_exempt(path: str) -> bool:
    return (path.startswith("/webhooks/") or is_beacon(path) or path.startswith("/api/v1/")
            or path == "/api/v1" or path == "/mcp" or path.startswith("/mcp/"))


def in_csrf_scope(path: str) -> bool:
    return path.startswith(_CSRF_SCOPES)


def body_cap(path: str) -> int:
    """Max request body in bytes for this path."""
    if path.startswith("/api/uploads"):
        # The uploads route streams and enforces SAAS_MAX_UPLOAD_MB itself (413 upload_too_large).
        # This is only a backstop above it (multipart overhead + slack), so that route answers first.
        return (settings.max_upload_mb + 64) * 1024 * 1024
    if path == "/api/t/replay":
        return 1024 * 1024
    if path == "/api/t/csp":
        return 16 * 1024
    if is_beacon(path):
        return 64 * 1024
    if path.startswith("/webhooks/"):
        return 512 * 1024
    if path == "/mcp" or path.startswith("/mcp/"):
        return 1024 * 1024
    return settings.max_body_kb * 1024


# ---- origins ------------------------------------------------------------------------------------
def _origin_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}".lower()


def allowed_origins(request: Request) -> set[str]:
    out = set()
    pub = _origin_of(settings.public_url)
    if pub:
        out.add(pub)
    host = request.headers.get("host")
    if host:
        out.add(f"{request.url.scheme}://{host}".lower())
    for extra in (settings.tracking_allowed_origins or "").split(","):
        o = _origin_of(extra.strip())
        if o:
            out.add(o)
    return out


def origin_ok(request: Request, *, require: bool = False) -> bool:
    """True when Origin (or, failing that, Referer) is ours. A missing header passes unless `require`."""
    origin = request.headers.get("origin")
    if origin:
        return origin.lower() in allowed_origins(request)
    ref = _origin_of(request.headers.get("referer"))
    if ref:
        return ref in allowed_origins(request)
    return not require


# ---- CSP ----------------------------------------------------------------------------------------
def _extra(csv: str) -> str:
    return " ".join(x.strip() for x in (csv or "").split(",") if x.strip())


def content_security_policy(nonce: str, path: str = "/") -> str:
    style = "'self'" if settings.csp_strict_style and not path.startswith("/admin") else "'self' 'unsafe-inline'"
    directives = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' {TURNSTILE} {_extra(settings.csp_extra_script_src)}".strip(),
        f"style-src {style}",
        f"img-src 'self' data: blob: https: {_extra(settings.csp_extra_img_src)}".strip(),
        "font-src 'self' data:",
        "media-src 'self' blob:",
        f"connect-src 'self' {TURNSTILE} {_extra(settings.csp_extra_connect_src)}".strip(),
        f"frame-src {TURNSTILE} {_extra(settings.csp_extra_frame_src)}".strip(),
        "worker-src 'self' blob:",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        f"form-action 'self' {_extra(settings.csp_extra_form_action)}".strip(),
        "frame-ancestors 'none'",
        "report-uri /api/t/csp",
    ]
    if settings.is_prod:
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


PERMISSIONS_POLICY = ", ".join([
    "accelerometer=()", "camera=()", "display-capture=()", "geolocation=()", "gyroscope=()",
    "magnetometer=()", "microphone=()", "midi=()", "payment=()", "usb=()", "serial=()", "hid=()",
    "bluetooth=()", "xr-spatial-tracking=()", "browsing-topics=()", "fullscreen=(self)",
    "autoplay=(self)", "picture-in-picture=(self)",
])


def _security_headers(path: str, nonce: str) -> list[tuple[str, str]]:
    h = [
        ("X-Frame-Options", "DENY"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ("Permissions-Policy", PERMISSIONS_POLICY),
        ("Cross-Origin-Opener-Policy", "same-origin-allow-popups"),
        ("Cross-Origin-Resource-Policy", "same-site"),
        ("X-Permitted-Cross-Domain-Policies", "none"),
    ]
    # FastAPI's dev-only interactive docs pull Swagger UI from a CDN with inline script.
    if not (path.startswith("/_docs") and not settings.is_prod):
        h.append(("Content-Security-Policy", content_security_policy(nonce, path)))
    if settings.is_prod:
        h.append(("Strict-Transport-Security", f"max-age={settings.hsts_max_age}; includeSubDomains"))
    if path.startswith(("/api/", "/admin", "/auth/", "/waitlist/confirm")):
        h.append(("Cache-Control", "no-store"))
    return h


# ---- event dedupe -------------------------------------------------------------------------------
_noted: dict[tuple[str, str], float] = {}
_noted_lock = threading.Lock()


def _note(kind: str, severity: str, ip: str, path: str, detail: dict | None = None, every_s: int = 600) -> None:
    key = (kind, ip)
    now = time.time()
    with _noted_lock:
        if _noted.get(key, 0) > now - every_s:
            return
        _noted[key] = now
        if len(_noted) > 20_000:
            _noted.clear()
    try:
        from ..db import session_scope
        from . import events

        with session_scope() as db:
            events.log(db, kind, severity, ip=ip, path=path, detail=detail)
    except Exception as exc:
        log.debug("could not record %s: %s", kind, exc)


# ---- background maintenance ----------------------------------------------------------------------
_bg_started = False
_bg_lock = threading.Lock()


def _ensure_background() -> None:
    global _bg_started
    if _bg_started:
        return
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True
    from . import maintenance, netintel

    if settings.background_maintenance and netintel.network_allowed():
        maintenance.start()


# ---- the middleware -----------------------------------------------------------------------------
class SecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _reject(request: Request, status: int, code: str, message: str,
                headers: dict[str, str] | None = None):
        path = request.url.path
        wants_json = path.startswith(("/api/", "/auth/", "/admin/api", "/webhooks/", "/mcp")) or \
            "application/json" in request.headers.get("accept", "")
        if wants_json:
            return JSONResponse({"error": code, "message": message}, status_code=status, headers=headers)
        return PlainTextResponse(message, status_code=status, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        _ensure_background()
        request = Request(scope)
        path = scope.get("path") or "/"
        method = scope.get("method", "GET").upper()
        nonce = secrets.token_urlsafe(16)
        state = scope.setdefault("state", {})
        state["csp_nonce"] = nonce

        csrf_cookie = request.cookies.get(CSRF_COOKIE) or ""
        csrf_valid = bool(_TOKEN_RE.match(csrf_cookie))
        csrf_token = csrf_cookie if csrf_valid else secrets.token_urlsafe(24)
        state["csrf_token"] = csrf_token

        sent_headers = False

        async def send_wrapper(message: Message) -> None:
            nonlocal sent_headers
            if message["type"] == "http.response.start":
                sent_headers = True
                headers = MutableHeaders(scope=message)
                for name, value in _security_headers(path, nonce):
                    if name not in headers:
                        headers[name] = value
                ctype = headers.get("content-type", "")
                if not csrf_valid and ctype.startswith("text/html"):
                    cookie = (f"{CSRF_COOKIE}={csrf_token}; Path=/; Max-Age=31536000; SameSite=Lax"
                              + ("; Secure" if settings.is_prod else ""))
                    headers.append("set-cookie", cookie)
            await send(message)

        async def reject(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
            resp = self._reject(request, status, code, message, headers)
            await resp(scope, receive, send_wrapper)

        ip = client_ip(request)

        if not is_static(path):
            # 1. bans
            vid = request.cookies.get("vid")
            banned = await anyio.to_thread.run_sync(_ban_hit, ip, vid)
            if banned:
                await anyio.to_thread.run_sync(_note, "ban_hit", "medium", ip, path, {"kind": banned})
                await reject(403, "banned", "Access denied.")
                return

            # 2. global per-IP limits
            ok, retry = await anyio.to_thread.run_sync(
                _global_hit, "g", ip, settings.rate_limit_global_per_min, 2.0)
            if ok and method in _WRITE and path.startswith(("/api/", "/auth/")) and not is_beacon(path) \
                    and not path.startswith("/webhooks/"):
                ok, retry = await anyio.to_thread.run_sync(
                    _global_hit, "w", ip, settings.rate_limit_write_per_min, 1.0)
            if not ok:
                await anyio.to_thread.run_sync(_note, "rate_limited", "low", ip, path, {"bucket": "global"})
                await reject(429, "rate_limited", "Too many requests. Slow down and try again shortly.",
                             {"Retry-After": str(retry)})
                return

        # 3. origin + CSRF
        if method in _WRITE and in_csrf_scope(path) and not csrf_exempt(path):
            if not path.endswith("/callback") and not origin_ok(request):
                await anyio.to_thread.run_sync(_note, "bad_origin", "medium", ip, path,
                                               {"origin": (request.headers.get("origin") or "")[:200]})
                await reject(403, "bad_origin", "Cross-site request refused.")
                return
            if SESSION_COOKIE in request.cookies:
                header = request.headers.get(CSRF_HEADER) or ""
                if not (csrf_cookie and header and hmac.compare_digest(csrf_cookie.encode(), header.encode())):
                    await anyio.to_thread.run_sync(_note, "csrf_failed", "medium", ip, path, None)
                    await reject(403, "csrf_failed", "Missing or invalid CSRF token. Reload the page and try again.")
                    return

        # 4. body caps
        cap = body_cap(path)
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > cap:
                    await reject(413, "payload_too_large", "Request body too large.")
                    return
            except ValueError:
                await reject(400, "bad_request", "Invalid Content-Length.")
                return

        received = 0
        too_large = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    too_large = True
                    raise _TooLarge()
            return message

        replaced = False

        async def guarded_send(message: Message) -> None:
            # FastAPI turns an exception while reading the body into a 400; when the cause was our
            # cap, answer with the 413 it really is.
            nonlocal replaced
            if too_large:
                if message["type"] == "http.response.start" and not replaced:
                    replaced = True
                    resp = self._reject(request, 413, "payload_too_large", "Request body too large.")
                    await resp(scope, receive, send_wrapper)
                return
            await send_wrapper(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            if not sent_headers:
                await reject(413, "payload_too_large", "Request body too large.")


def _ban_hit(ip: str, vid: str | None) -> str | None:
    try:
        if bans.ip_banned(ip):
            return "ip"
        if vid and bans.value_banned("visitor", vid):
            return "visitor"
        snap = bans.snapshot()
        if snap.values.get("asn") and settings.geoip_asn_db:
            from ..tracking import geo

            asn = geo.lookup(ip).asn
            if asn and str(asn) in snap.values["asn"]:
                return "asn"
    except Exception as exc:
        log.debug("ban check failed: %s", exc)
    return None


def _global_hit(bucket: str, ip: str, per_min: int, sync_s: float) -> tuple[bool, int]:
    return limits.limiter.hit(bucket, ip, per_min, 60, sync_s=sync_s)


async def _rate_limited_handler(request: Request, exc: limits.RateLimited):
    return JSONResponse(exc.detail, status_code=429, headers={"Retry-After": str(exc.retry_after)})


def reset_state() -> None:
    with _noted_lock:
        _noted.clear()


def install(app: FastAPI) -> None:
    """Wire the security layer into a fresh app (called once from saas.main.create_app)."""
    # Module-level caches outlive a test's fresh in-memory DB; every new app starts clean.
    limits.reset_state()
    bans.reset_state()
    reset_state()
    from . import netintel

    netintel.reset_state()
    app.add_middleware(SecurityMiddleware)
    app.add_exception_handler(limits.RateLimited, _rate_limited_handler)
