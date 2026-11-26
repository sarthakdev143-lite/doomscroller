"""Waitlist: join (double opt-in), confirm, position, fraud-resistant referrals, invites.

Referral rules (a referral moves the referrer up `waitlist_referral_bump` places, once):
- it counts only when the referred person *confirms* their email;
- never when the referred entry shares an IP, fingerprint or browser with the referrer's cluster
  (every IP / fingerprint / visitor the referrer has been seen with), or with another referral of
  the same referrer that already counted (one person farming from one machine);
- never for an alias of the referrer's own address, a flagged (risky) entry, or past 25 counted
  referrals in a day for one referrer (held for review instead).
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..common import client_ip, utcnow
from ..config import settings
from ..models.security import SeenSignal
from ..models.tracking import VisitorLink
from ..models.waitlist import InviteBatch, WaitlistEntry
from ..security import disposable, events, forms, limits, risk, turnstile
from ..security.dbutil import touch_signal
from . import emails

log = logging.getLogger("saas.waitlist")

_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"  # no 0/1/i/l/o: survives being read aloud
_INVITE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ROLES = {"creator", "agency", "editor", "marketer", "developer", "podcaster", "educator", "business", "other"}
REFERRAL_DAILY_CAP = 25
WAITING = ("confirmed",)


def _code(n: int, alphabet: str = _ALPHABET) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def referral_url(entry: WaitlistEntry) -> str:
    return f"{settings.public_url.rstrip('/')}/?ref={entry.referral_code}"


def _rank_key(entry: WaitlistEntry) -> int:
    return int(entry.seq) - settings.waitlist_referral_bump * int(entry.referrals_counted or 0)


def position(db: Session, entry: WaitlistEntry) -> int | None:
    """1-based place among people still waiting (confirmed, not yet invited)."""
    if entry.status in ("invited", "joined", "removed"):
        return None
    ahead = db.execute(select(func.count(WaitlistEntry.id)).where(
        WaitlistEntry.status.in_(WAITING), WaitlistEntry.id != entry.id,
        or_(WaitlistEntry.rank_key < entry.rank_key,
            and_(WaitlistEntry.rank_key == entry.rank_key, WaitlistEntry.seq < entry.seq)))).scalar_one()
    return int(ahead) + 1


def waiting_total(db: Session) -> int:
    return int(db.execute(select(func.count(WaitlistEntry.id)).where(WaitlistEntry.status.in_(WAITING))).scalar_one())


def public_view(db: Session, entry: WaitlistEntry) -> dict:
    return {"ok": True, "status": entry.status, "position": position(db, entry), "total": waiting_total(db),
            "referrals": int(entry.referrals_counted or 0), "referral_url": referral_url(entry),
            "referral_code": entry.referral_code, "confirmed": entry.confirmed_at is not None}


@dataclass
class Outcome:
    status: int
    body: dict
    entry: WaitlistEntry | None = None
    events: list = field(default_factory=list)


def _err(status: int, code: str, message: str) -> Outcome:
    return Outcome(status, {"ok": False, "error": code, "message": message})


def _clean_channel(url: object) -> str | None:
    """Stored as text for the owner to read; never fetched (the jobs agent owns link fetching)."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()[:500]
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
        return None
    return p.geturl()[:500]


def _same_person(entry: WaitlistEntry, *, ip: str, fp: str | None, vid: str | None) -> bool:
    return bool((vid and entry.visitor_id == vid) or (fp and entry.fingerprint == fp)
                or (ip and entry.ip == ip))


def _next_seq(db: Session) -> int:
    return int(db.execute(select(func.coalesce(func.max(WaitlistEntry.seq), 0))).scalar_one()) + 1


def _new_referral_code(db: Session) -> str:
    for _ in range(10):
        code = _code(8)
        if not db.execute(select(WaitlistEntry.id).where(WaitlistEntry.referral_code == code)).first():
            return code
    return _code(12)


def join(db: Session, request: Request, data: dict) -> Outcome:
    """POST /api/waitlist. Caller has already applied the per-IP rate limits."""
    from ..tracking import identity

    ip = client_ip(request)
    vid = identity.visitor_id(request)
    fp = forms.clean_fingerprint(data.get("fp"))
    raw_email = str(data.get("email") or "").strip()[:320]

    # Bots that fill the honeypot get a convincing "ok" and nothing else.
    fc = forms.check(honeypot=data.get("website"), ts=data.get("ts"))
    if fc.honeypot:
        events.log(db, "honeypot", "medium", ip=ip, fingerprint=fp, visitor_id=vid, path=request.url.path,
                   detail={"form": "waitlist"})
        return Outcome(200, {"ok": True, "position": None, "referral_url": None, "already": False,
                             "confirm_required": True})
    if fc.too_fast:
        events.log(db, "form_too_fast", "low", ip=ip, fingerprint=fp, visitor_id=vid, path=request.url.path,
                   detail={"form": "waitlist", "fill_ms": fc.fill_ms})
        return _err(400, "too_fast", "That was quick! Please wait a moment and submit again.")

    try:
        from email_validator import EmailNotValidError, validate_email

        email = disposable.normalise(validate_email(raw_email, check_deliverability=False).normalized)
    except (EmailNotValidError, ValueError, TypeError):
        return _err(400, "invalid_email", "Enter a valid email address.")
    if disposable.is_disposable(email):
        events.log(db, "disposable_email", "low", ip=ip, fingerprint=fp, visitor_id=vid, path=request.url.path,
                   detail={"domain": disposable.domain_of(email)})
        return _err(400, "disposable_email", "Please use a permanent email address, not a disposable one.")

    token = data.get("cf_turnstile_response") or data.get("cf-turnstile-response")
    if turnstile.enabled() and not turnstile.verify(token, ip, action="waitlist"):
        events.log(db, "turnstile_failed", "medium", ip=ip, fingerprint=fp, visitor_id=vid, path=request.url.path)
        return _err(400, "turnstile_failed", "Please complete the verification and try again.")

    canon = disposable.canonical(email)
    existing = db.execute(select(WaitlistEntry).where(or_(WaitlistEntry.email == email,
                                                          WaitlistEntry.email_canonical == canon))
                          .order_by(WaitlistEntry.seq).limit(1)).scalar_one_or_none()
    if existing is not None and existing.status != "removed":
        return _already(db, request, existing, ip=ip, fp=fp, vid=vid)

    verdict = risk.assess(db, request, email, fp, context="waitlist", turnstile_ok=None)
    hard = set(verdict.reasons) & {"banned", "automation", "ip_velocity"}
    if "banned" in hard:
        return _err(403, "blocked", "This sign-up can't be accepted.")
    if hard:
        return _err(429 if "ip_velocity" in hard else 403, "blocked",
                    "Too many sign-ups from this network. Try again later.")

    ref_code = str(data.get("ref") or "").strip().lower()[:16]
    referrer = None
    if ref_code and re.fullmatch(r"[a-z0-9]{6,16}", ref_code):
        referrer = db.execute(select(WaitlistEntry).where(WaitlistEntry.referral_code == ref_code)).scalar_one_or_none()
        if referrer is not None and (referrer.email_canonical == canon or referrer.status == "removed"):
            referrer = None

    role = str(data.get("role") or "").strip().lower()[:64] or None
    if role and role not in ROLES:
        role = "other"

    source = None
    from ..models.tracking import Visitor

    vis = db.get(Visitor, vid) if vid else None
    if vis is not None:
        source = {k: v for k, v in {"source": vis.first_source, "medium": vis.first_medium,
                                    "campaign": vis.first_campaign, "referrer": vis.first_referrer_host,
                                    "landing": vis.first_landing}.items() if v}

    now = utcnow()
    tok = secrets.token_urlsafe(32)
    entry = WaitlistEntry(
        email=email, email_canonical=canon, role=role, channel_url=_clean_channel(data.get("channel_url")),
        status="pending", seq=_next_seq(db), referral_code=_new_referral_code(db),
        referred_by_id=referrer.id if referrer else None, referral_status="pending" if referrer else None,
        confirm_token_hash=_hash(tok), confirm_sent_at=now,
        confirm_expires_at=now + timedelta(hours=settings.waitlist_confirm_hours), confirm_sends=1,
        ip=ip, fingerprint=fp, visitor_id=vid, country=verdict.detail.get("country"),
        user_agent=(request.headers.get("user-agent") or "")[:512], source=source,
        risk_score=verdict.score, risk_reasons=verdict.reasons or None,
        flagged=verdict.score >= settings.risk_no_credit_score, created_at=now, updated_at=now,
    )
    entry.rank_key = _rank_key(entry)
    db.add(entry)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()  # lost a race with the same email
        existing = db.execute(select(WaitlistEntry).where(WaitlistEntry.email == email)).scalar_one_or_none()
        if existing is None:
            return _err(409, "conflict", "Please try again.")
        return _already(db, request, existing, ip=ip, fp=fp, vid=vid)
    touch_signal(db, "ip", ip, "waitlist", entry.id, now)
    touch_signal(db, "fp", fp, "waitlist", entry.id, now)
    touch_signal(db, "email", canon, "waitlist", entry.id, now)
    if vid:
        identity.link(db, vid, waitlist_id=entry.id)
        if vis is not None:
            st = dict(vis.stats or {})
            st["waitlist"] = "joined"
            vis.stats = st
            from ..tracking import scoring

            scoring.rescore(vis)
    db.commit()
    pos = position_if_confirmed(db, entry)
    emails.confirm_email(email, tok, pos)
    return Outcome(200, {"ok": True, "already": False, "confirm_required": True, "position": pos,
                         "referral_url": referral_url(entry), "referral_code": entry.referral_code}, entry)


def position_if_confirmed(db: Session, entry: WaitlistEntry) -> int:
    ahead = db.execute(select(func.count(WaitlistEntry.id)).where(
        WaitlistEntry.status.in_(WAITING), WaitlistEntry.id != entry.id,
        or_(WaitlistEntry.rank_key < entry.rank_key,
            and_(WaitlistEntry.rank_key == entry.rank_key, WaitlistEntry.seq < entry.seq)))).scalar_one()
    return int(ahead) + 1


def _already(db: Session, request: Request, entry: WaitlistEntry, *, ip: str, fp: str | None,
             vid: str | None) -> Outcome:
    """Someone submitted an address that is already on the list.

    The referral link (and position) only go back to the browser that looks like the same person;
    anyone else just learns "check your inbox", and the owner of the address gets the email.
    """
    now = utcnow()
    same = _same_person(entry, ip=ip, fp=fp, vid=vid)
    if entry.status == "pending":
        recently = entry.confirm_sent_at and now - entry.confirm_sent_at < timedelta(minutes=10)
        if not recently and (entry.confirm_sends or 0) < 6:
            tok = secrets.token_urlsafe(32)
            entry.confirm_token_hash = _hash(tok)
            entry.confirm_sent_at = now
            entry.confirm_expires_at = now + timedelta(hours=settings.waitlist_confirm_hours)
            entry.confirm_sends = (entry.confirm_sends or 0) + 1
            db.commit()
            emails.confirm_email(entry.email, tok, position_if_confirmed(db, entry))
    elif not same and entry.status == "confirmed":
        # At most one "you're already on the list" email per address per hour, however often it is typed.
        if limits.hit(None, "wl_already_mail", 1, 3600, key=entry.id):
            emails.already_email(entry.email, referral_url(entry), position(db, entry))
    body = {"ok": True, "already": True, "confirm_required": entry.status == "pending",
            "position": None, "referral_url": None}
    if same:
        body["position"] = position(db, entry) if entry.status != "pending" else position_if_confirmed(db, entry)
        body["referral_url"] = referral_url(entry)
        body["referral_code"] = entry.referral_code
    return Outcome(200, body, entry)


# ---- confirmation -----------------------------------------------------------------------------
def find_by_token(db: Session, token: str) -> WaitlistEntry | None:
    if not token or len(token) > 128:
        return None
    return db.execute(select(WaitlistEntry).where(WaitlistEntry.confirm_token_hash == _hash(token))).scalar_one_or_none()


def confirm(db: Session, request: Request, token: str, fp: object = None) -> tuple[str, WaitlistEntry | None]:
    """-> ("confirmed" | "already" | "expired" | "invalid", entry)."""
    from ..tracking import identity

    entry = find_by_token(db, token)
    if entry is None or entry.status == "removed":
        return "invalid", None
    if entry.status != "pending":
        return "already", entry
    now = utcnow()
    if entry.confirm_expires_at and entry.confirm_expires_at < now:
        return "expired", entry
    ip = client_ip(request)
    fp = forms.clean_fingerprint(fp)
    vid = identity.visitor_id(request)
    entry.status = "confirmed"
    entry.confirmed_at = now
    entry.confirm_ip = ip
    entry.confirm_fingerprint = fp
    entry.updated_at = now
    touch_signal(db, "ip", ip, "waitlist", entry.id, now)
    touch_signal(db, "fp", fp, "waitlist", entry.id, now)
    if vid:
        identity.link(db, vid, waitlist_id=entry.id)
        from ..models.tracking import Visitor

        vis = db.get(Visitor, vid)
        if vis is not None:
            st = dict(vis.stats or {})
            st["waitlist"] = "confirmed"
            vis.stats = st
            from ..tracking import scoring

            scoring.rescore(vis)
    if entry.referred_by_id:
        _settle_referral(db, entry, extra_ip=ip, extra_fp=fp, extra_vid=vid)
    db.commit()
    return "confirmed", entry


def _cluster(db: Session, entry: WaitlistEntry) -> tuple[set[str], set[str], set[str]]:
    """Every IP, fingerprint and visitor id this entry has been seen with."""
    ips = {x for x in (entry.ip, entry.confirm_ip) if x}
    fps = {x for x in (entry.fingerprint, entry.confirm_fingerprint) if x}
    vids = {x for x in (entry.visitor_id,) if x}
    vids |= set(db.execute(select(VisitorLink.visitor_id).where(VisitorLink.waitlist_id == entry.id)).scalars())
    subjects = [("waitlist", entry.id)] + [("visitor", v) for v in vids]
    for kind, sid in subjects:
        for k, val in db.execute(select(SeenSignal.kind, SeenSignal.value).where(
                SeenSignal.subject_kind == kind, SeenSignal.subject_id == sid)).all():
            if k == "ip":
                ips.add(val)
            elif k == "fp":
                fps.add(val)
    return ips, fps, vids


def _settle_referral(db: Session, referred: WaitlistEntry, *, extra_ip: str | None, extra_fp: str | None,
                     extra_vid: str | None) -> None:
    referrer = db.get(WaitlistEntry, referred.referred_by_id)
    reason = None
    if referrer is None or referrer.status == "removed":
        reason = "referrer_gone"
    elif referrer.email_canonical == referred.email_canonical:
        reason = "self"
    elif referred.flagged:
        reason = "risky"
    else:
        r_ips, r_fps, r_vids = _cluster(db, referrer)
        mine_ips = {x for x in (referred.ip, referred.confirm_ip, extra_ip) if x}
        mine_fps = {x for x in (referred.fingerprint, referred.confirm_fingerprint, extra_fp) if x}
        mine_vids = {x for x in (referred.visitor_id, extra_vid) if x}
        if mine_vids & r_vids:
            reason = "same_browser"
        elif mine_fps & r_fps:
            reason = "same_fingerprint"
        elif mine_ips & r_ips:
            reason = "same_ip"
        else:
            siblings = db.execute(select(WaitlistEntry).where(
                WaitlistEntry.referred_by_id == referrer.id, WaitlistEntry.referral_status == "counted",
                WaitlistEntry.id != referred.id)).scalars().all()
            for s in siblings:
                s_ips = {x for x in (s.ip, s.confirm_ip) if x}
                s_fps = {x for x in (s.fingerprint, s.confirm_fingerprint) if x}
                if (mine_ips & s_ips) or (mine_fps & s_fps) or (s.visitor_id and s.visitor_id in mine_vids):
                    reason = "sibling_cluster"
                    break
            if reason is None:
                day_ago = utcnow() - timedelta(days=1)
                today = db.execute(select(func.count(WaitlistEntry.id)).where(
                    WaitlistEntry.referred_by_id == referrer.id, WaitlistEntry.referral_status == "counted",
                    WaitlistEntry.confirmed_at > day_ago)).scalar_one()
                if today >= REFERRAL_DAILY_CAP:
                    reason = "velocity_hold"
    if reason is None:
        referred.referral_status = "counted"
        referred.referral_reason = None
        referrer.referrals_counted = int(referrer.referrals_counted or 0) + 1
        referrer.rank_key = _rank_key(referrer)
        referrer.updated_at = utcnow()
    else:
        referred.referral_status = "held" if reason == "velocity_hold" else "rejected"
        referred.referral_reason = reason
        events.log(db, "referral_rejected", "low" if reason in ("referrer_gone",) else "medium",
                   ip=referred.confirm_ip or referred.ip, fingerprint=referred.confirm_fingerprint or referred.fingerprint,
                   detail={"referrer": referred.referred_by_id, "referred": referred.id, "reason": reason},
                   commit=False)


# ---- invites -----------------------------------------------------------------------------------
def _new_invite_code(db: Session) -> str:
    for _ in range(10):
        code = "-".join(_code(4, _INVITE_ALPHABET) for _ in range(3))
        if not db.execute(select(WaitlistEntry.id).where(WaitlistEntry.invite_code == code)).first():
            return code
    return _code(16, _INVITE_ALPHABET)


def invite(db: Session, entry: WaitlistEntry, *, batch_id: str | None = None, send: bool = True) -> WaitlistEntry:
    now = utcnow()
    if not entry.invite_code:
        entry.invite_code = _new_invite_code(db)
    entry.status = "invited"
    entry.invited_at = now
    entry.invite_expires_at = now + timedelta(days=settings.waitlist_invite_days)
    entry.invite_batch_id = batch_id or entry.invite_batch_id
    entry.invite_sends = int(entry.invite_sends or 0) + (1 if send else 0)
    entry.updated_at = now
    db.flush()
    if send:
        emails.invite_email(entry.email, entry.invite_code, settings.waitlist_invite_days)
    return entry


def invite_batch(db: Session, count: int, *, actor_id: str | None = None, note: str | None = None,
                 include_flagged: bool = False) -> InviteBatch:
    """Invite the next `count` confirmed people in line (flagged entries wait for manual review)."""
    count = max(1, min(int(count), 1000))
    q = select(WaitlistEntry).where(WaitlistEntry.status == "confirmed")
    if not include_flagged:
        q = q.where(WaitlistEntry.flagged.is_(False))
    rows = db.execute(q.order_by(WaitlistEntry.rank_key, WaitlistEntry.seq).limit(count)).scalars().all()
    batch = InviteBatch(created_by=actor_id, requested=count, invited=0, note=(note or None) and note[:500],
                        created_at=utcnow())
    db.add(batch)
    db.flush()
    for e in rows:
        invite(db, e, batch_id=batch.id)
        batch.invited += 1
    db.commit()
    return batch


def resend(db: Session, entry: WaitlistEntry) -> str:
    """Re-send whatever this entry is waiting on. -> 'confirm' | 'invite' | 'nothing'."""
    if entry.status == "pending":
        tok = secrets.token_urlsafe(32)
        now = utcnow()
        entry.confirm_token_hash = _hash(tok)
        entry.confirm_sent_at = now
        entry.confirm_expires_at = now + timedelta(hours=settings.waitlist_confirm_hours)
        entry.confirm_sends = int(entry.confirm_sends or 0) + 1
        db.commit()
        emails.confirm_email(entry.email, tok, position_if_confirmed(db, entry))
        return "confirm"
    if entry.status == "invited":
        if entry.invite_expires_at and entry.invite_expires_at < utcnow():
            invite(db, entry, send=False)
        entry.invite_sends = int(entry.invite_sends or 0) + 1
        db.commit()
        emails.invite_email(entry.email, entry.invite_code, settings.waitlist_invite_days)
        return "invite"
    return "nothing"


def redeem_invite(db: Session, code: str, email: str) -> WaitlistEntry | None:
    """Contract: the core agent's signup calls this in launch_mode=waitlist.

    The invite is bound to the address it was sent to (aliases of it count), so a shared or resold
    code is useless to anyone else. Idempotent for the same address; the caller commits.
    """
    code = (code or "").strip().upper()
    if not code or len(code) > 32:
        return None
    entry = db.execute(select(WaitlistEntry).where(WaitlistEntry.invite_code == code)).scalar_one_or_none()
    if entry is None:
        return None
    if disposable.canonical(email or "") != entry.email_canonical:
        events.log(db, "invite_wrong_email", "medium", detail={"entry": entry.id}, commit=False)
        return None
    if entry.status == "joined":
        return entry
    if entry.status != "invited":
        return None
    if entry.invite_expires_at and entry.invite_expires_at < utcnow():
        return None
    entry.status = "joined"
    entry.redeemed_at = utcnow()
    entry.updated_at = entry.redeemed_at
    db.flush()
    return entry


def mark_redeemed_user(db: Session, entry: WaitlistEntry, user_id: str) -> None:
    entry.redeemed_user_id = user_id
    db.flush()
