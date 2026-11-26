"""Risk scoring for identity creation: signups (core agent) and waitlist joins (saas.waitlist).

`assess_signup(db, request, email, fingerprint)` -> Verdict(score, reasons, block, grant_free_credits).
Every reason is a short machine code; points are additive and capped at 100. A *hard* reason blocks
outright whatever the score (banned, disposable inbox, honeypot, automation, Turnstile failure,
per-IP velocity). Free credits are withheld at `risk_no_credit_score`, or whenever the identity looks
like a repeat of one that already got them (same fingerprint as another account, an alias of an
existing email, an IP shared by several accounts, Tor / VPN / datacenter origin).

The core agent should call `record_signup(db, user_id, email, ip, fingerprint)` after creating the
user, so the next attempt from the same person is recognised even before the users table is read.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..common import client_ip, utcnow
from ..config import settings
from ..models.security import SeenSignal
from ..models.waitlist import WaitlistEntry
from . import bans, disposable, events, forms, netintel

log = logging.getLogger("saas.security.risk")

HARD = {"banned", "invalid_email", "disposable_email", "honeypot_filled", "form_too_fast",
        "turnstile_failed", "automation", "ip_velocity"}
NO_CREDIT = {"fingerprint_reuse", "email_alias", "ip_shared_many", "tor_exit", "vpn", "datacenter_ip",
             "disposable_email", "no_fingerprint"}


@dataclass
class Verdict:
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    block: bool = False
    grant_free_credits: bool = True
    detail: dict = field(default_factory=dict)

    def add(self, points: int, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
            self.score += points

    def as_dict(self) -> dict:
        return {"score": self.score, "reasons": list(self.reasons), "block": self.block,
                "grant_free_credits": self.grant_free_credits}


def _user_model():
    try:
        from ..models import core

        user = getattr(core, "User", None)
        return user if user is not None and hasattr(user, "__table__") else None
    except Exception:
        return None


def _visitor(db: Session, request: Request):
    try:
        from ..models.tracking import Visitor
        from ..tracking import identity

        vid = identity.visitor_id(request)
        return db.get(Visitor, vid) if vid else None
    except Exception:
        return None


def assess(db: Session, request: Request, email: str | None, fingerprint: str | None, *,
           context: str = "signup", honeypot: object = None, form_ts: object = None,
           turnstile_ok: bool | None = None, log_event: bool = True) -> Verdict:
    v = Verdict()
    now = utcnow()
    ip = client_ip(request)
    fp = forms.clean_fingerprint(fingerprint)
    email_n = disposable.normalise(email or "")
    canon = disposable.canonical(email_n) if email_n else ""
    vis = _visitor(db, request)
    v.detail = {"ip": ip, "fingerprint": fp, "context": context}

    # ---- hard stops ---------------------------------------------------------------------------
    from ..tracking import geo as geo_mod

    g = geo_mod.lookup(ip, request)
    banned = bans.check(db, ip=ip, fingerprint=fp, visitor_id=getattr(vis, "id", None), email=email_n,
                        asn=g.asn)
    if banned:
        v.add(100, "banned")
        v.detail["ban"] = banned
    if not email_n or "@" not in email_n or len(email_n) > 254:
        v.add(100, "invalid_email")
    elif disposable.is_disposable(email_n):
        v.add(60, "disposable_email")
    if honeypot is not None or form_ts is not None:
        fc = forms.check(honeypot=honeypot, ts=form_ts)
        if fc.honeypot:
            v.add(100, "honeypot_filled")
        if fc.too_fast:
            v.add(60, "form_too_fast")
        if "form_ts_missing" in fc.reasons:
            v.add(10, "form_ts_missing")
    if turnstile_ok is False:
        v.add(60, "turnstile_failed")

    from ..tracking import ua as ua_mod

    uai = ua_mod.parse(request.headers.get("user-agent"))
    strong_ua = [r for r in uai.reasons if r in ("ua_tool", "ua_headless", "empty_ua")]
    vis_bot = [r for r in (getattr(vis, "bot_reasons", None) or []) if r in ("webdriver", "automation", "ua_headless")]
    if strong_ua or vis_bot:
        v.add(70, "automation")
        v.detail["automation"] = strong_ua + vis_bot

    # ---- network ------------------------------------------------------------------------------
    intel = netintel.inspect(ip, g)
    if intel.is_tor:
        v.add(40, "tor_exit")
    if intel.is_vpn:
        v.add(25, "vpn")
    elif intel.is_datacenter:
        v.add(30, "datacenter_ip")
    v.detail["country"] = g.country

    if not fp:
        v.add(15, "no_fingerprint")

    # ---- velocity -----------------------------------------------------------------------------
    hour_ago = now - timedelta(hours=1)
    recent = 0
    try:
        recent += db.execute(select(func.count(WaitlistEntry.id)).where(
            WaitlistEntry.ip == ip, WaitlistEntry.created_at > hour_ago)).scalar_one()
    except Exception:
        pass
    User = _user_model()
    if User is not None and hasattr(User, "signup_ip") and hasattr(User, "created_at"):
        try:
            recent += db.execute(select(func.count()).select_from(User).where(
                User.signup_ip == ip, User.created_at > hour_ago)).scalar_one()
        except Exception:
            pass
    v.detail["ip_identities_last_hour"] = recent
    if recent >= max(1, settings.signup_ip_hourly_limit):
        v.add(50, "ip_velocity")
    elif recent >= 2:
        v.add(10, "ip_busy")

    # ---- clusters: has this person already got an account? ------------------------------------
    if context == "signup":
        _signup_clusters(db, v, User, ip=ip, fp=fp, canon=canon, email_n=email_n, now=now)
    else:
        _waitlist_clusters(db, v, ip=ip, fp=fp, canon=canon, email_n=email_n)

    # ---- verdict ------------------------------------------------------------------------------
    v.score = max(0, min(100, v.score))
    v.block = bool(HARD & set(v.reasons)) or v.score >= settings.risk_block_score
    v.grant_free_credits = (not v.block and v.score < settings.risk_no_credit_score
                            and not (NO_CREDIT & set(v.reasons)))
    if log_event and (v.block or v.score >= 30):
        events.log(db, f"{context}_risk", "high" if v.block else "medium", ip=ip, fingerprint=fp,
                   visitor_id=getattr(vis, "id", None),
                   detail={"score": v.score, "reasons": v.reasons, "email_domain": disposable.domain_of(email_n),
                           "blocked": v.block},
                   path=request.url.path, commit=True)
    return v


def _signup_clusters(db: Session, v: Verdict, User, *, ip: str, fp: str | None, canon: str, email_n: str,
                     now) -> None:
    # Fingerprint already behind another account -> the classic free-credit farm.
    accounts_same_fp = 0
    if fp:
        if User is not None and hasattr(User, "signup_fingerprint"):
            try:
                q = select(func.count()).select_from(User).where(User.signup_fingerprint == fp)
                if hasattr(User, "email"):
                    q = q.where(func.lower(User.email) != email_n)
                accounts_same_fp += db.execute(q).scalar_one()
            except Exception:
                pass
        accounts_same_fp += db.execute(select(func.count(func.distinct(SeenSignal.subject_id))).where(
            SeenSignal.kind == "fp", SeenSignal.value == fp, SeenSignal.subject_kind == "user")).scalar_one()
    if accounts_same_fp:
        v.add(35 if accounts_same_fp < 3 else 60, "fingerprint_reuse")
        v.detail["accounts_same_fingerprint"] = accounts_same_fp

    # Same IP as several recent accounts. One is a household or office; several is a pattern.
    month_ago = now - timedelta(days=30)
    accounts_same_ip = 0
    if User is not None and hasattr(User, "signup_ip") and hasattr(User, "created_at"):
        try:
            accounts_same_ip = db.execute(select(func.count()).select_from(User).where(
                User.signup_ip == ip, User.created_at > month_ago)).scalar_one()
        except Exception:
            pass
    if accounts_same_ip >= 3:
        v.add(30, "ip_shared_many")
    elif accounts_same_ip >= 1:
        v.add(10, "ip_shared")
    v.detail["accounts_same_ip_30d"] = accounts_same_ip

    # Alias of an existing account (j.o.e+2@gmail.com when joe@gmail.com exists).
    alias = False
    if canon:
        alias = bool(db.execute(select(func.count()).select_from(SeenSignal).where(
            SeenSignal.kind == "email", SeenSignal.value == canon, SeenSignal.subject_kind == "user")).scalar_one())
        if not alias and User is not None and hasattr(User, "email"):
            domain = canon.rsplit("@", 1)[-1]
            try:
                emails = db.execute(select(User.email).where(User.email.ilike(f"%@{domain}"))
                                    .limit(5000)).scalars().all()
                alias = any(disposable.canonical(e) == canon and disposable.normalise(e) != email_n
                            for e in emails if e)
            except Exception:
                pass
    if alias:
        v.add(50, "email_alias")


def _waitlist_clusters(db: Session, v: Verdict, *, ip: str, fp: str | None, canon: str, email_n: str) -> None:
    if canon:
        dup = db.execute(select(func.count(WaitlistEntry.id)).where(
            WaitlistEntry.email_canonical == canon, WaitlistEntry.email != email_n)).scalar_one()
        if dup:
            v.add(30, "email_alias")
    if fp:
        same_fp = db.execute(select(func.count(WaitlistEntry.id)).where(
            WaitlistEntry.fingerprint == fp, WaitlistEntry.email_canonical != canon)).scalar_one()
        if same_fp:
            v.add(min(40, 15 * same_fp), "fingerprint_reuse")
            v.detail["entries_same_fingerprint"] = same_fp


def assess_signup(db: Session, request: Request, email: str, fingerprint: str | None, *,
                  honeypot: object = None, form_ts: object = None, turnstile_ok: bool | None = None) -> Verdict:
    """Contract entry point for the core agent's signup. Adds no rows except a security event."""
    return assess(db, request, email, fingerprint, context="signup", honeypot=honeypot, form_ts=form_ts,
                  turnstile_ok=turnstile_ok)


def record_signup(db: Session, user_id: str, email: str, ip: str | None, fingerprint: str | None) -> None:
    """Remember the signals a new account arrived with (caller commits)."""
    from .dbutil import touch_signal

    now = utcnow()
    touch_signal(db, "email", disposable.canonical(email or ""), "user", user_id, now)
    touch_signal(db, "ip", ip, "user", user_id, now)
    touch_signal(db, "fp", forms.clean_fingerprint(fingerprint), "user", user_id, now)


def cluster_for(db: Session, *, ip: str | None = None, fingerprint: str | None = None) -> list[dict]:
    """Every subject that has shown up with this IP or fingerprint (for the admin panel)."""
    conds = []
    if ip:
        conds.append((SeenSignal.kind == "ip") & (SeenSignal.value == ip))
    if fingerprint:
        conds.append((SeenSignal.kind == "fp") & (SeenSignal.value == fingerprint))
    if not conds:
        return []
    rows = db.execute(select(SeenSignal).where(or_(*conds)).order_by(SeenSignal.last_seen.desc()).limit(500)).scalars()
    return [{"kind": r.kind, "value": r.value, "subject_kind": r.subject_kind, "subject_id": r.subject_id,
             "first_seen": r.first_seen, "last_seen": r.last_seen, "hits": r.hits} for r in rows]
