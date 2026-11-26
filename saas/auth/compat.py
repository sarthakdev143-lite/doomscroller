"""One shim for every call the core makes into modules other agents own.

saas.security, saas.tracking and saas.waitlist are built in parallel. Each call here imports at call
time and, if the module or function is missing:
  * in dev: logs once and degrades (a local limiter, Turnstile treated as passed, free credits allowed);
  * in prod: fails closed with 503 `security_unavailable` (or, for free credits, grants none),
    because a missing security layer must never silently mean "no security".
Tests patch these functions rather than the other agents' modules.
"""
from __future__ import annotations

import importlib
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from ..common import client_ip
from ..config import settings
from .errors import ApiError

log = logging.getLogger("saas.auth.compat")
_warned: set[str] = set()


def _resolve(module: str, attr: str):
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return None
    return getattr(mod, attr, None)


def _missing(what: str) -> None:
    if settings.is_prod:
        log.error("security dependency missing in prod: %s", what)
        raise ApiError(503, "security_unavailable", "This action is temporarily unavailable. Try again shortly.")
    if what not in _warned:
        _warned.add(what)
        log.warning("dev fallback: %s is not available yet", what)


# ---- rate limits ----------------------------------------------------------------------------------

class _LocalLimiter:
    """Sliding-window counter, used only while saas.security.limits is absent (dev)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def hit(self, bucket: str, key: str, limit: int, window_s: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[(bucket, key)]
            while q and q[0] <= now - window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_local = _LocalLimiter()


def rate_limit(request: Optional[Request], bucket: str, limit: int, window_s: int, key: str | None = None) -> None:
    """Raise 429 `rate_limited` when over the limit."""
    enforce = _resolve("saas.security.limits", "enforce")
    if enforce is not None:
        enforce(request, bucket, limit, window_s, key)
        return
    _missing("saas.security.limits")
    k = key or (client_ip(request) if request is not None else "-")
    if not _local.hit(bucket, k, limit, window_s):
        raise ApiError(429, "rate_limited", "Too many requests. Slow down and try again shortly.",
                       headers={"Retry-After": str(window_s)})


def rate_hit(request: Optional[Request], bucket: str, limit: int, window_s: int, key: str | None = None) -> bool:
    """Non-raising: True while within the limit."""
    hit = _resolve("saas.security.limits", "hit")
    if hit is not None:
        return bool(hit(request, bucket, limit, window_s, key))
    _missing("saas.security.limits")
    k = key or (client_ip(request) if request is not None else "-")
    return _local.hit(bucket, k, limit, window_s)


# ---- bot checks ---------------------------------------------------------------------------------

def turnstile_ok(token: Optional[str], ip: str) -> bool:
    verify = _resolve("saas.security.turnstile", "verify")
    if verify is not None:
        try:
            return bool(verify(token, ip))
        except Exception:
            log.exception("turnstile verify failed")
            return not settings.is_prod
    _missing("saas.security.turnstile")
    return True


def form_is_bot(honeypot: Any, ts: Any) -> bool:
    check = _resolve("saas.security.forms", "check")
    if check is not None:
        try:
            return bool(check(honeypot=honeypot, ts=ts).is_bot)
        except Exception:
            log.exception("form check failed")
            return False
    return bool(honeypot not in (None, "") and str(honeypot).strip())


def clean_fingerprint(fp: Any) -> Optional[str]:
    fn = _resolve("saas.security.forms", "clean_fingerprint")
    if fn is not None:
        return fn(fp)
    if isinstance(fp, str) and 16 <= len(fp.strip()) <= 64 and all(c in "0123456789abcdef" for c in fp.strip().lower()):
        return fp.strip().lower()
    return None


def assess_signup(db: Session, request: Request, email: str, fingerprint: Optional[str], *,
                  honeypot: Any = None, form_ts: Any = None, turnstile_ok: Optional[bool] = None) -> dict:
    """The risk verdict as a plain dict: {score, reasons, block, grant_free_credits}."""
    fn = _resolve("saas.security.risk", "assess_signup")
    if fn is not None:
        verdict = fn(db, request, email, fingerprint, honeypot=honeypot, form_ts=form_ts, turnstile_ok=turnstile_ok)
        if hasattr(verdict, "as_dict"):
            return verdict.as_dict()
        return {"score": int(getattr(verdict, "score", 0)), "reasons": list(getattr(verdict, "reasons", [])),
                "block": bool(getattr(verdict, "block", False)),
                "grant_free_credits": bool(getattr(verdict, "grant_free_credits", False))}
    if settings.is_prod:
        log.error("risk.assess_signup missing in prod: no free credits")
        return {"score": 0, "reasons": ["risk_unavailable"], "block": False, "grant_free_credits": False}
    _missing("saas.security.risk")
    return {"score": 0, "reasons": ["risk_unavailable_dev"], "block": False, "grant_free_credits": True}


def record_signup(db: Session, user_id: str, email: str, ip: Optional[str], fingerprint: Optional[str]) -> None:
    fn = _resolve("saas.security.risk", "record_signup")
    if fn is None:
        return
    try:
        fn(db, user_id, email, ip, fingerprint)
    except Exception:
        log.exception("risk.record_signup failed")


def log_event(db: Session, kind: str, severity: str = "info", *, ip: Optional[str] = None,
              user_id: Optional[str] = None, fingerprint: Optional[str] = None, detail: Any = None) -> None:
    fn = _resolve("saas.security.events", "log")
    if fn is None:
        log.info("security event %s (%s) user=%s ip=%s", kind, severity, user_id, ip)
        return
    try:
        fn(db, kind, severity, ip=ip, user_id=user_id, fingerprint=fingerprint, detail=detail)
    except Exception:
        log.exception("security event %s could not be recorded", kind)


# ---- tracking & waitlist --------------------------------------------------------------------------

def visitor_id(request: Request) -> Optional[str]:
    fn = _resolve("saas.tracking.identity", "visitor_id")
    if fn is None:
        return None
    try:
        return fn(request)
    except Exception:
        return None


def link_identity(db: Session, request: Request, user_id: str) -> None:
    fn = _resolve("saas.tracking.identity", "link")
    if fn is None:
        return
    try:
        fn(db, visitor_id(request), user_id=user_id)
    except Exception:
        log.exception("identity.link failed")


def redeem_invite(db: Session, code: Optional[str], email: str):
    """The waitlist entry the invite belongs to, or None. Only called in launch_mode=waitlist."""
    fn = _resolve("saas.waitlist", "redeem_invite")
    if fn is None:
        if settings.is_prod:
            log.error("waitlist.redeem_invite missing in prod: refusing signup")
            return None
        _missing("saas.waitlist.redeem_invite")
        return True  # dev: allow signups until the waitlist backend lands
    if not code:
        return None
    try:
        return fn(db, code, email)
    except ApiError:
        raise
    except Exception:
        log.exception("waitlist.redeem_invite failed")
        return None
