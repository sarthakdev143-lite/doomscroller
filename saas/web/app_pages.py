"""The signed-in product and the sign-in pages, served by the Python app only (never the static export).

Pages render a shell and fetch their data from the JSON API in the browser (docs/saas/CONTRACT.md), so
every number a page shows is the one the API enforces. Registered on saas.web.routes.router.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import google, policy
from ..auth.deps import optional_user, require_user
from ..auth.service import has_totp, safe_next
from ..config import settings
from ..db import get_db
from ..models.core import User
from .routes import FREE_MINUTES, FREE_SHORTS, _render, _source_names, router

# Messages for /login?error=<code> that are not a Google round trip (the magic-link page uses link_expired).
_LOGIN_ERRORS = {
    "link_expired": "That sign-in link expired or was already used. Ask for a new one.",
    "account_disabled": "This account can’t sign in. Contact support.",
}


def _support_link(track: str = "support") -> dict:
    return {"href": f"mailto:{settings.support_email}", "label": "Email support", "track": track}


_HOME = {"href": "/", "label": "Back to the home page", "track": "home"}
_WAITLIST = {"href": "/#join", "label": "Join the waitlist", "track": "waitlist"}

# Every code the Google round trip can end on (saas.auth.routes._login_error) -> the page /login renders.
# `retry` is the label above the Google button that starts the same attempt again (same next, invite, re-auth).
AUTH_ERRORS: dict[str, dict] = {
    "google_cancelled": {
        "kicker": "Sign-in cancelled", "title": "You stopped at Google.",
        "message": "You cancelled, or chose not to share your details, so nothing was shared and no account was "
                   "changed. Whenever you’re ready, start again.",
        "retry": "Try again", "links": [_HOME]},
    "google_blocked": {
        "kicker": "Not allowed", "title": "Google didn’t allow this sign-in.",
        "message": "Some work and school accounts only let their users sign in to apps an administrator has "
                   "approved. Try a personal Google account, or ask the account’s administrator.",
        "retry": "Choose another Google account", "links": [_HOME]},
    "oauth_state_mismatch": {
        "kicker": "Security check", "title": "Finish signing in where you started.",
        "message": "This sign-in began in another browser or tab, so we stopped it to keep your account safe. "
                   "Start again here and it will go through.",
        "detail": "If this keeps happening, your browser may be blocking cookies for this site. Sign-in needs one.",
        "retry": "Start again here", "links": [_HOME]},
    "oauth_state_expired": {
        "kicker": "Timed out", "title": "That sign-in took too long.",
        "message": "A sign-in attempt lasts 10 minutes and works once. Start a fresh one: it takes a few seconds.",
        "retry": "Start again", "links": [_HOME]},
    "google_failed": {
        "kicker": "Didn’t finish", "title": "Google sign-in didn’t finish.",
        "message": "Something went wrong between us and Google. Nothing was changed on your account. Try again.",
        "retry": "Try again", "links": [_HOME, _support_link()]},
    "google_unreachable": {
        "kicker": "Connection", "title": "We couldn’t reach Google.",
        "message": "Google didn’t answer us in time. Check your connection and try again in a moment.",
        "retry": "Try again", "links": [_HOME]},
    "google_unavailable": {
        "kicker": "Not set up", "title": "Sign-in isn’t set up yet.",
        "message": "This server has no Google sign-in configured, so nobody can sign in right now. Try again "
                   "later, or tell us.",
        "retry": "", "links": [_HOME, _support_link()]},
    "rate_limited": {
        "kicker": "Slow down", "title": "Too many tries in a row.",
        "message": "For your account’s safety we pause sign-in for a few minutes after many attempts. Wait a "
                   "little, then start again.",
        "retry": "Start again", "links": [_HOME]},
    "oauth_unverified_email": {
        "kicker": "Email not verified", "title": "Google hasn’t verified that email yet.",
        "message": "We only accept an address Google has confirmed belongs to you. Verify it in your Google "
                   "Account, or choose another account.",
        "retry": "Choose another Google account", "links": [_HOME]},
    "invite_required": {
        "kicker": "Invite only", "title": "Sign-ups are invite-only for now.",
        "message": "That Google account doesn’t have an account here yet. Join the waitlist and we’ll send you "
                   "an invite, or choose the Google account you were invited with.",
        "retry": "Choose another Google account", "links": [_WAITLIST, _HOME]},
    "invite_other_email": {
        "kicker": "Wrong account", "title": "This invite is for a different email.",
        "message": "Invites are tied to the address they were sent to. Continue with the Google account for "
                   "{hint} to accept it.",
        "retry": "Choose that account", "links": [_HOME]},
    "invite_expired": {
        "kicker": "Invite expired", "title": "This invite has expired.",
        "message": "Invites last a couple of weeks. Ask whoever invited you for a fresh link, or join the "
                   "waitlist again.",
        "retry": "", "links": [_WAITLIST, _HOME]},
    "signup_blocked": {
        "kicker": "Can’t create an account", "title": "We couldn’t create an account from here.",
        "message": "Our abuse checks stopped this sign-up. If that’s a mistake, email us and we’ll sort it out.",
        "retry": "", "links": [_support_link(), _HOME]},
    "account_unavailable": {
        "kicker": "Account unavailable", "title": "This account can’t sign in.",
        "message": "It has been suspended or closed. Email us if you think that’s a mistake.",
        "retry": "", "links": [_support_link(), _HOME]},
    "google_linked_elsewhere": {
        "kicker": "Already linked", "title": "That Google account belongs to another user.",
        "message": "It’s already connected to a different account here. Sign in with it instead, or choose "
                   "another Google account.",
        "retry": "Choose another Google account", "links": [{"href": "/app/settings", "label": "Back to settings",
                                                             "track": "settings"}]},
    "reauth_other_account": {
        "kicker": "Different account", "title": "That’s a different Google account.",
        "message": "To confirm it’s you, choose the Google account you’re signed in with ({hint}).",
        "retry": "Choose that account", "links": [{"href": "/app", "label": "Back to the app", "track": "app"}]},
}


def _invite_view(db: Session, code: str) -> tuple[str, str]:
    """(state, masked address) for an invite code: ok | joined | expired | invalid, or ("", "") without one."""
    code = (code or "").strip().upper()
    if not code:
        return "", ""
    try:
        from ..common import utcnow
        from ..models.waitlist import WaitlistEntry

        entry = db.query(WaitlistEntry).filter(WaitlistEntry.invite_code == code[:32]).one_or_none()
    except Exception:  # waitlist tables missing: treat as unknown rather than fail the page
        db.rollback()
        return "invalid", ""
    if entry is None or entry.status not in ("invited", "joined"):
        return "invalid", ""
    hint = policy.mask_email(entry.email)
    if entry.status == "joined":
        return "joined", hint
    if entry.invite_expires_at is not None and entry.invite_expires_at < utcnow():
        return "expired", hint
    return "ok", hint


def _error_page(request: Request, db: Session, user: Optional[User], code: str):
    """The branded page for one way a Google sign-in can end; its button retries the same attempt."""
    q = request.query_params
    nxt = safe_next(q.get("next"))
    invite = (q.get("invite") or "")[:64]
    reauth = q.get("reauth") == "1" and user is not None
    hint = ""
    if code == "invite_required" and invite and settings.launch_mode == "waitlist":
        state, hint = _invite_view(db, invite)
        code = {"ok": "invite_other_email", "expired": "invite_expired"}.get(state, code)
    if code == "reauth_other_account" and user is not None:
        hint = policy.mask_email(user.email)
    if code not in AUTH_ERRORS:
        code = "google_failed"
    problem = dict(AUTH_ERRORS[code], code=code)
    problem["message"] = problem["message"].replace("{hint}", hint or "the invited address")
    if code == "invite_required":
        invite = ""  # a bad invite would only fail again; the button starts a plain sign-in
    return _auth_page(request, "auth/error.html", "auth-error", problem=problem, retry_next=nxt,
                      retry_invite=invite, retry_reauth=reauth)


def _free_grant(db: Session) -> dict:
    """The free tier's totals, from the free plan (admin-editable); the owner's numbers if it is missing."""
    try:
        from ..billing import catalog

        plan = catalog.get_plan(db, "free")
        feats = (plan.features or {}) if plan else {}
        return {"minutes": int(feats.get("free_grant_minutes", FREE_MINUTES)),
                "shorts": int(feats.get("free_grant_shorts", FREE_SHORTS))}
    except Exception:  # billing tables not seeded yet: show the default grant rather than fail the page
        db.rollback()
        return {"minutes": FREE_MINUTES, "shorts": FREE_SHORTS}


def _auth_page(request: Request, template: str, section: str, **extra: Any):
    return _render(request, template, auth_section=section, password_min=settings.password_min_length,
                   verify_hours=settings.verify_token_hours, google_enabled=google.enabled(), **extra)


def _app_page(request: Request, db: Session, user: User, template: str, nav: str, **extra: Any):
    return _render(request, template, nav=nav, user_email=user.email, user_name=user.name or "",
                   user_verified=user.email_verified_at is not None, user_mfa=has_totp(db, user.id),
                   free_grant=_free_grant(db), **extra)


# ---- sign-in pages --------------------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request, user: Optional[User] = Depends(optional_user), db: Session = Depends(get_db)):
    q = request.query_params
    nxt = safe_next(q.get("next"))
    mfa = (q.get("mfa") or "")[:200]
    error = (q.get("error") or "")[:64]
    if error and error not in _LOGIN_ERRORS:  # every Google round-trip ending, known or not, gets a page
        return _error_page(request, db, user, error)
    stepup = q.get("stepup") == "1" and user is not None
    if stepup and not has_totp(db, user.id):
        return RedirectResponse("/app/settings#mfa-title", status_code=303)  # set it up first, then come back
    if user is not None and not mfa and not stepup:
        return RedirectResponse(nxt, status_code=303)
    return _auth_page(request, "auth/login.html", "auth-login", next_path=nxt, mfa_token=mfa, stepup=stepup,
                      verified=q.get("verified") == "1", reset=q.get("reset") == "1",
                      google_notice=q.get("notice") == "google_only" and policy.google_only(),
                      error_message=_LOGIN_ERRORS.get(error, "Sign-in didn’t work. Try again." if error else ""))


@router.get("/signup")
def signup_page(request: Request, user: Optional[User] = Depends(optional_user), db: Session = Depends(get_db)):
    if user is not None:
        return RedirectResponse("/app", status_code=303)
    q = request.query_params
    invite = (q.get("invite") or "")[:64]
    state, hint = _invite_view(db, invite) if policy.google_only() and settings.launch_mode == "waitlist" else ("", "")
    return _auth_page(request, "auth/signup.html", "auth-signup", invite=invite, ref=(q.get("ref") or "")[:64],
                      next_path=safe_next(q.get("next")), invite_state=state, invite_hint=hint)


def _retired_in_google_only(request: Request):
    """Pages of the email/password flows, which do not exist in Google-only mode: send people to sign in."""
    return RedirectResponse("/login?notice=google_only", status_code=303)


@router.get("/verify-email")
def verify_email_page(request: Request, user: Optional[User] = Depends(optional_user)):
    if policy.google_only():
        if user is not None and user.email_verified_at is not None:
            return RedirectResponse("/app", status_code=303)
        return _retired_in_google_only(request)
    if user is not None and user.email_verified_at is not None:
        return RedirectResponse("/app", status_code=303)
    return _auth_page(request, "auth/verify_email.html", "auth-verify", status=request.query_params.get("status") or "",
                      signed_in=user is not None, signed_in_email=user.email if user else "")


@router.get("/forgot")
def forgot_page(request: Request):
    if policy.google_only():
        return _retired_in_google_only(request)
    return _auth_page(request, "auth/forgot.html", "auth-forgot")


@router.get("/reset/{token}")
def reset_page(request: Request, token: str):
    if policy.google_only():
        return _retired_in_google_only(request)
    return _auth_page(request, "auth/reset.html", "auth-reset", token=token[:200])


# ---- the app --------------------------------------------------------------------------------------

@router.get("/app")
def app_dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _app_page(request, db, user, "app/dashboard.html", "dashboard",
                     verified_now=request.query_params.get("verified") == "1",
                     welcome_now=request.query_params.get("welcome") == "1",
                     welcome_credits=user.free_credits_granted_at is not None)


@router.get("/app/new")
def app_new(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    gb = settings.max_upload_mb / 1024
    return _app_page(request, db, user, "app/new.html", "new", sources=_source_names(),
                     max_upload_mb=settings.max_upload_mb,
                     max_upload=f"{gb:g} GB" if gb >= 1 else f"{settings.max_upload_mb} MB")


@router.get("/app/jobs/{job_id}")
def app_job(request: Request, job_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _app_page(request, db, user, "app/job.html", "dashboard", job_id=job_id[:80])


@router.get("/app/clips")
def app_clips(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _app_page(request, db, user, "app/clips.html", "clips")


@router.get("/app/brand")
def app_brand(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _app_page(request, db, user, "app/brand.html", "brand")


@router.get("/app/billing")
def app_billing(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    status = request.query_params.get("status") or ""
    return _app_page(request, db, user, "app/billing.html", "billing",
                     pay_status=status if status in ("paid", "pending", "failed") else "")


@router.get("/app/settings")
def app_settings(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _app_page(request, db, user, "app/settings.html", "settings", password_min=settings.password_min_length)


@router.get("/app/developers")
def app_developers(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    name = "".join(ch if ch.isalnum() else "-" for ch in settings.brand_name.lower()).strip("-") or "clipper"
    return _app_page(request, db, user, "app/developers.html", "developers",
                     mcp_url=f"{settings.public_url.rstrip('/')}/mcp", mcp_name=name)
