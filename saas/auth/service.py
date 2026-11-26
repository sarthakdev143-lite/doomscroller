"""Account logic shared by every auth entry point (password signup, Google, magic link, API).

`create_account` is the only way a user row is ever created, so invite redemption, the risk verdict,
referral linking and the credit account happen identically whichever door someone came through.
Free credits are not granted at signup: the verdict is stored, and the grant happens when the email
is verified, once per inbox (Gmail dots/+tags folded) and once per device fingerprint.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Any, Optional

from email_validator import EmailNotValidError, validate_email
from fastapi import Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..common import client_country, client_ip, utcnow
from ..config import settings
from ..models.core import ApiKey, AuthSession, EmailToken, Identity, MfaChallenge, TotpSecret, User
from . import compat, passwords
from .crypto import random_token, sha256_hex
from .errors import ApiError

log = logging.getLogger("saas.auth.service")

_GMAIL = {"gmail.com", "googlemail.com"}
TOKEN_TTL = {
    "verify": lambda: timedelta(hours=settings.verify_token_hours),
    "reset": lambda: timedelta(minutes=settings.reset_token_minutes),
    "magic": lambda: timedelta(minutes=settings.magic_token_minutes),
}
MFA_CHALLENGE_TTL = timedelta(minutes=5)
MFA_MAX_ATTEMPTS = 5


# ---- email helpers --------------------------------------------------------------------------------

def normalise_email(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 320:
        raise ApiError(400, "invalid_email", "Enter a valid email address.")
    try:
        info = validate_email(raw.strip(), check_deliverability=False)
    except EmailNotValidError:
        raise ApiError(400, "invalid_email", "Enter a valid email address.")
    return info.normalized.lower()


def canonical_email(email: str) -> str:
    local, _, domain = email.lower().partition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _hash_canonical(email: str) -> str:
    return hashlib.sha256(canonical_email(email).encode("utf-8")).hexdigest()[:40]


def find_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).one_or_none()


def safe_next(raw: Optional[str], default: str = "/app") -> str:
    """Only same-site relative paths: no scheme, no //host, no backslash tricks."""
    if not raw or not isinstance(raw, str) or len(raw) > 300:
        return default
    if not raw.startswith("/") or raw.startswith("//") or "\\" in raw or any(ord(c) < 32 for c in raw):
        return default
    return raw


def _new_referral_code(db: Session) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    for _ in range(20):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if db.query(User.id).filter(User.referral_code == code).first() is None:
            return code
    return secrets.token_hex(8)


# ---- account creation -----------------------------------------------------------------------------

def create_account(db: Session, request: Request, *, email: str, password: Optional[str] = None, name: str = "",
                   invite: Optional[str] = None, ref: Optional[str] = None, fingerprint: Optional[str] = None,
                   honeypot: Any = None, form_ts: Any = None, turnstile_ok: Optional[bool] = None,
                   method: str = "password", email_verified: bool = False) -> User:
    """Create a user (caller has checked the email is free). Raises ApiError on refusal."""
    from ..billing import ledger

    if settings.launch_mode == "waitlist":
        entry = compat.redeem_invite(db, (invite or "").strip() or None, email)
        if not entry:
            raise ApiError(403, "invite_required",
                           "Sign-ups are invite-only right now. Join the waitlist or use the link from your invite.")

    verdict = compat.assess_signup(db, request, email, fingerprint, honeypot=honeypot, form_ts=form_ts,
                                   turnstile_ok=turnstile_ok)
    ip = client_ip(request)
    if verdict.get("block"):
        compat.log_event(db, "signup_blocked", "warning", ip=ip, fingerprint=fingerprint,
                         detail={"reasons": verdict.get("reasons", [])[:10], "method": method})
        raise ApiError(403, "signup_blocked",
                       f"We couldn't create an account from here. Contact {settings.support_email} if this is a mistake.")

    now = utcnow()
    user = User(
        email=email,
        email_canonical=canonical_email(email),
        name=(name or "").strip()[:120],
        password_hash=passwords.hash_password(password) if password else None,
        password_changed_at=now if password else None,
        role="user",
        status="active",
        plan_slug="free",
        signup_ip=ip,
        signup_fingerprint=fingerprint,
        signup_country=client_country(request),
        signup_user_agent=request.headers.get("user-agent", "")[:400],
        visitor_id=compat.visitor_id(request),
        referral_code=_new_referral_code(db),
        signup_risk={k: verdict.get(k) for k in ("score", "reasons", "block", "grant_free_credits")},
        flags={},
    )
    if email in settings.admin_email_set and email_verified:
        user.role = "admin"
    db.add(user)
    db.flush()
    ledger.ensure_account(db, user.id)

    if ref:
        _link_referral(db, user, ref.strip().lower())

    compat.record_signup(db, user.id, email, ip, fingerprint)
    compat.link_identity(db, request, user.id)
    compat.log_event(db, "signup", "info", ip=ip, user_id=user.id, fingerprint=fingerprint,
                     detail={"method": method, "score": verdict.get("score")})
    if email_verified:
        mark_verified(db, user)
    return user


def _link_referral(db: Session, user: User, code: str) -> None:
    from ..models.billing import Referral

    referrer = db.query(User).filter(User.referral_code == code).one_or_none()
    if referrer is None or referrer.id == user.id or referrer.status != "active":
        return
    same_device = bool(user.signup_fingerprint and user.signup_fingerprint == referrer.signup_fingerprint)
    same_inbox = canonical_email(referrer.email) == canonical_email(user.email)
    user.referred_by = referrer.id
    db.add(Referral(referrer_id=referrer.id, referred_id=user.id,
                    status="void" if (same_device or same_inbox) else "pending",
                    reason="same_identity" if (same_device or same_inbox) else ""))
    db.flush()


def mark_verified(db: Session, user: User) -> None:
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    if user.email in settings.admin_email_set and user.role != "admin":
        user.role = "admin"
    db.flush()
    grant_free_credits(db, user)


def grant_free_credits(db: Session, user: User) -> int:
    """Once per verified identity: the stored verdict must allow it, and neither this inbox (canonical)
    nor this device fingerprint may have had a grant before. Returns credits granted."""
    from ..billing import ledger

    amount = settings.signup_free_credits
    if amount <= 0 or user.free_credits_granted_at is not None or user.email_verified_at is None:
        return 0
    verdict = user.signup_risk or {}
    reason = None
    if not verdict.get("grant_free_credits"):
        reason = "risk_verdict"
    elif user.signup_fingerprint and db.query(User.id).filter(
            User.signup_fingerprint == user.signup_fingerprint, User.id != user.id,
            User.free_credits_granted_at.is_not(None)).first() is not None:
        reason = "fingerprint_already_granted"
    if reason:
        user.flags = {**(user.flags or {}), "free_credits_withheld": reason}
        db.flush()
        return 0
    entry = ledger.grant(db, user.id, amount, kind="grant", idempotency_key=f"free:{_hash_canonical(user.email)}",
                         note="Free credits")
    if entry is None:
        user.flags = {**(user.flags or {}), "free_credits_withheld": "inbox_already_granted"}
        db.flush()
        return 0
    user.free_credits_granted_at = utcnow()
    db.flush()
    return amount


# ---- email tokens ---------------------------------------------------------------------------------

def issue_email_token(db: Session, user: User, purpose: str, *, ip: Optional[str] = None,
                      next_path: Optional[str] = None) -> str:
    """A fresh single-use token; any older unused token of the same purpose is voided."""
    now = utcnow()
    db.execute(update(EmailToken).where(EmailToken.user_id == user.id, EmailToken.purpose == purpose,
                                        EmailToken.used_at.is_(None)).values(used_at=now))
    raw = random_token(32)
    db.add(EmailToken(user_id=user.id, purpose=purpose, token_hash=sha256_hex(raw), email=user.email,
                      next_path=next_path, created_at=now, expires_at=now + TOKEN_TTL[purpose](), ip=ip))
    db.flush()
    return raw


def peek_email_token(db: Session, raw: Optional[str], purpose: str) -> Optional[tuple[EmailToken, User]]:
    """The token and its user if it is valid right now, without consuming it."""
    if not raw or len(raw) > 200:
        return None
    tok = db.query(EmailToken).filter(EmailToken.token_hash == sha256_hex(raw), EmailToken.purpose == purpose).one_or_none()
    if tok is None or tok.used_at is not None or tok.expires_at <= utcnow():
        return None
    user = db.get(User, tok.user_id)
    if user is None or user.status != "active" or user.email != tok.email:
        return None
    return tok, user


def consume_email_token(db: Session, raw: Optional[str], purpose: str) -> Optional[tuple[EmailToken, User]]:
    found = peek_email_token(db, raw, purpose)
    if found is None:
        return None
    tok, user = found
    res = db.execute(update(EmailToken).where(EmailToken.id == tok.id, EmailToken.used_at.is_(None))
                     .values(used_at=utcnow()))
    if res.rowcount != 1:
        return None  # lost a race with another use of the same link
    return tok, user


def token_owner(db: Session, raw: Optional[str], purpose: str) -> Optional[User]:
    """Who a (possibly used or expired) token belonged to. Only for choosing a friendlier redirect."""
    if not raw or len(raw) > 200:
        return None
    tok = db.query(EmailToken).filter(EmailToken.token_hash == sha256_hex(raw), EmailToken.purpose == purpose).one_or_none()
    return db.get(User, tok.user_id) if tok else None


# ---- password login & MFA challenge -----------------------------------------------------------------

def authenticate_password(db: Session, email: str, password: str) -> Optional[User]:
    """Constant-work: an unknown email still costs one argon2 verify."""
    user = find_user_by_email(db, email)
    ok = passwords.verify_password(user.password_hash if user else None, password)
    if not ok or user is None or user.status != "active":
        return None
    if passwords.needs_rehash(user.password_hash or ""):
        user.password_hash = passwords.hash_password(password)
        db.flush()
    return user


def has_totp(db: Session, user_id: str) -> bool:
    row = db.get(TotpSecret, user_id)
    return row is not None and row.confirmed_at is not None


def begin_mfa_challenge(db: Session, user: User, *, method: str, next_path: Optional[str], ip: Optional[str]) -> str:
    raw = random_token(32)
    now = utcnow()
    db.add(MfaChallenge(user_id=user.id, token_hash=sha256_hex(raw), method=method, next_path=next_path,
                        created_at=now, expires_at=now + MFA_CHALLENGE_TTL, ip=ip))
    db.flush()
    return raw


def complete_mfa_challenge(db: Session, raw: Optional[str], code: str) -> Optional[tuple[MfaChallenge, User]]:
    from . import totp

    if not raw or len(raw) > 200:
        return None
    ch = db.query(MfaChallenge).filter(MfaChallenge.token_hash == sha256_hex(raw)).one_or_none()
    if ch is None or ch.used_at is not None or ch.expires_at <= utcnow() or ch.attempts >= MFA_MAX_ATTEMPTS:
        return None
    user = db.get(User, ch.user_id)
    if user is None or user.status != "active":
        return None
    ch.attempts += 1
    db.flush()
    if not totp.verify(db, user, code):
        return None
    res = db.execute(update(MfaChallenge).where(MfaChallenge.id == ch.id, MfaChallenge.used_at.is_(None))
                     .values(used_at=utcnow()))
    if res.rowcount != 1:
        return None
    return ch, user


# ---- password change / reset ------------------------------------------------------------------------

def set_password(db: Session, user: User, new_password: str) -> None:
    refusal = passwords.check_new_password(new_password, email=user.email, name=user.name)
    if refusal:
        raise ApiError(400, refusal[0], refusal[1])
    user.password_hash = passwords.hash_password(new_password)
    user.password_changed_at = utcnow()
    db.flush()


# ---- views ----------------------------------------------------------------------------------------

def plan_of(db: Session, user: User):
    from ..billing import catalog

    return catalog.plan_for_user(db, user)


def public_user(db: Session, user: User) -> dict:
    from ..billing import ledger

    google = db.query(Identity.id).filter(Identity.user_id == user.id, Identity.provider == "google").first() is not None
    plan = plan_of(db, user)
    return {
        "id": user.id,
        "email": user.email,
        "email_verified": user.email_verified_at is not None,
        "name": user.name,
        "role": user.role,
        "status": user.status,
        "plan": {"slug": plan.slug if plan else user.plan_slug, "name": plan.name if plan else user.plan_slug,
                 "features": (plan.features if plan else {})},
        "balance": ledger.balance(db, user.id),
        "mfa_enabled": has_totp(db, user.id),
        "has_password": bool(user.password_hash),
        "google_linked": google,
        "referral_code": user.referral_code,
        "referral_url": f"{settings.public_url.rstrip('/')}/?ref={user.referral_code}",
        "free_credits_granted": user.free_credits_granted_at is not None,
        "billing_hold": bool(user.billing_hold),
        "created_at": user.created_at.isoformat(),
    }


# ---- deletion & export ------------------------------------------------------------------------------

def export_account(db: Session, user: User) -> dict:
    from ..models.billing import CreditEntry, Payment, Subscription
    from ..models.jobs import Clip, Job

    def rows(q, fields):
        return [{f: _jsonable(getattr(r, f)) for f in fields} for r in q]

    return {
        "exported_at": utcnow().isoformat(),
        "user": public_user(db, user),
        "sessions": rows(db.query(AuthSession).filter(AuthSession.user_id == user.id),
                         ["id", "created_at", "last_seen_at", "ip", "user_agent", "revoked_at"]),
        "api_keys": rows(db.query(ApiKey).filter(ApiKey.user_id == user.id),
                         ["id", "name", "prefix", "scopes", "created_at", "last_used_at", "revoked_at"]),
        "ledger": rows(db.query(CreditEntry).filter(CreditEntry.user_id == user.id).order_by(CreditEntry.created_at),
                       ["id", "kind", "amount", "balance_after", "note", "job_id", "created_at"]),
        "payments": rows(db.query(Payment).filter(Payment.user_id == user.id),
                         ["id", "provider", "kind", "item_slug", "currency", "amount_minor", "status", "created_at"]),
        "subscriptions": rows(db.query(Subscription).filter(Subscription.user_id == user.id),
                              ["id", "plan_slug", "interval", "status", "current_period_end", "created_at"]),
        "jobs": rows(db.query(Job).filter(Job.user_id == user.id),
                     ["id", "status", "source_url", "source_title", "credits_charged", "created_at"]),
        "clips": rows(db.query(Clip).filter(Clip.user_id == user.id, Clip.deleted_at.is_(None)),
                      ["id", "job_id", "title", "description", "hashtags", "duration_s", "created_at"]),
    }


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def delete_account(db: Session, user: User) -> None:
    """Soft delete: personal data is wiped, files removed, credentials revoked. Ledger and payment
    rows stay (accounting), keyed to the anonymised row."""
    from . import sessions as sess_mod
    from ..jobs import service as job_service
    from ..models.core import RecoveryCode

    now = utcnow()
    job_service.purge_user_content(db, user)
    sess_mod.revoke_all(db, user.id, reason="account_deleted")
    db.execute(update(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None)).values(revoked_at=now))
    db.query(TotpSecret).filter(TotpSecret.user_id == user.id).delete()
    db.query(RecoveryCode).filter(RecoveryCode.user_id == user.id).delete()
    db.query(Identity).filter(Identity.user_id == user.id).delete()
    db.query(EmailToken).filter(EmailToken.user_id == user.id).delete()
    db.query(MfaChallenge).filter(MfaChallenge.user_id == user.id).delete()
    user.email_canonical = _hash_canonical(user.email)  # keeps one-grant-per-inbox without the address
    user.email = f"deleted-{user.id}@deleted.invalid"
    user.name = ""
    user.password_hash = None
    user.signup_ip = None
    user.signup_user_agent = None
    user.visitor_id = None
    user.status = "deleted"
    user.deleted_at = now
    db.flush()
