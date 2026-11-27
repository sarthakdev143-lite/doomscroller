"""SAAS_TRUST_PROXY=edge: the origin only answers requests relayed by our Cloudflare Pages Function proxy.

The proxy adds three headers (and must strip any client-sent X-DS-* first):
    X-DS-Edge-Auth:  SAAS_EDGE_SECRET
    X-DS-Client-IP:  the visitor's IP (from Cloudflare's CF-Connecting-IP)
    X-DS-Client-Sig: hex(HMAC-SHA256(key=SAAS_EDGE_SECRET, msg=f"{ip}|{floor(unix_seconds / 60)}"))

`EdgeGate` (pure ASGI, installed outermost so bans, rate limits and logs all see the real client) refuses
with 403 anything without the right secret (so the raw origin URL is useless), anything whose IP signature
is not valid for the current minute +-2, and duplicated X-DS-* headers. It then sets the ASGI client
address to the signed IP and removes the X-DS-* headers, so `saas.common.client_ip()` (which, in this mode,
reads the connection address) returns the real visitor everywhere. With the secret empty it refuses
everything: a misconfigured edge mode fails closed. Webhooks must be routed through the proxy too.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import time
from typing import Optional

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import settings

H_AUTH, H_IP, H_SIG = b"x-ds-edge-auth", b"x-ds-client-ip", b"x-ds-client-sig"
OURS = (H_AUTH, H_IP, H_SIG)
WINDOW_MINUTES = 2


def client_sig(secret: str, ip: str, minute: int) -> str:
    return hmac.new(secret.encode("utf-8"), f"{ip}|{minute}".encode("utf-8"), hashlib.sha256).hexdigest()


def verify_client_ip(secret: str, ip: str, sig: str, *, now: Optional[float] = None) -> bool:
    if not secret or not ip or not sig or len(sig) != 64:
        return False
    minute = int((time.time() if now is None else now) // 60)
    ok = False
    for m in range(minute - WINDOW_MINUTES, minute + WINDOW_MINUTES + 1):
        ok |= hmac.compare_digest(client_sig(secret, ip, m), sig.lower())  # check all: no early exit
    return ok


def enabled() -> bool:
    return settings.trust_proxy.lower() == "edge"


class EdgeGate:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not enabled():
            await self.app(scope, receive, send)
            return
        found: dict[bytes, list[bytes]] = {}
        for k, v in scope.get("headers") or []:
            if k.lower() in OURS:
                found.setdefault(k.lower(), []).append(v)
        if any(len(v) > 1 for v in found.values()):
            await _deny(send, "edge_headers", "Request refused.")
            return
        secret = settings.edge_secret or ""
        auth = (found.get(H_AUTH) or [b""])[0]
        if not secret or not hmac.compare_digest(auth, secret.encode("utf-8")):
            await _deny(send, "edge_only", "Use the public site address.")
            return
        ip = (found.get(H_IP) or [b""])[0].decode("latin-1").strip()
        sig = (found.get(H_SIG) or [b""])[0].decode("latin-1").strip()
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            ip = ""
        if not ip or not verify_client_ip(secret, ip, sig):
            await _deny(send, "bad_client_ip", "Request refused.")
            return
        scope = dict(scope)
        scope["client"] = (ip, 0)
        scope["headers"] = [(k, v) for k, v in scope.get("headers") or [] if k.lower() not in OURS]
        await self.app(scope, receive, send)


async def _deny(send: Send, code: str, message: str) -> None:
    body = json.dumps({"error": code, "message": message}).encode("utf-8")
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


def install(app: FastAPI) -> None:
    """Add the gate as the outermost middleware (call after every other middleware is installed)."""
    app.add_middleware(EdgeGate)
