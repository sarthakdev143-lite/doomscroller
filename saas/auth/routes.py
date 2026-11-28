"""Auth and account endpoints (tables in docs/saas/CONTRACT.md, "Auth endpoints").

Enumeration safety:
* signup answers the same 201 whether or not the email exists, and never logs in (a Set-Cookie would
  itself reveal "new account"); the verify link signs the user in only in the browser that signed up
  (bound by the `su` cookie, which both paths set), so a verify link mailed to a victim can't log
  them into an attacker's account.
* login runs one argon2 verify whether or not the account exists, and fails with one message.
* forgot-password and magic-link always answer {"ok": true}; mail goes out on a background thread.

Google-only mode (saas.auth.policy, SAAS_AUTH_GOOGLE_ONLY): every email/password route answers 404
`google_only`, their pages redirect to /login, and Google sign-in (plus the TOTP step) is the only way in.
"""
from __future__ import annotations

import hashlib
import html
import logging
import time
from typing import Any, Optional, Union
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..common import client_ip, utcnow
from ..config import settings
from ..db import get_db
from ..models.core import AuthSession, Identity, User
from . import compat, google, keys as keys_mod, mailer, passwords, policy, service, sessions, totp
from .crypto import sign, verify_sig
from .deps import require_recent_auth, require_user, require_verified_user
from .errors import ApiError, ApiRoute

log = logging.getLogger("saas.auth")

router = APIRouter(route_class=ApiRoute)
SIGNUP_COOKIE = "su"
_PASSWORD_AUTH = [Depends(policy.require_password_auth)]  # off (404 google_only) in Google-only mode


# ---- bodies ---------------------------------------------------------------------------------------

class _FormBase(BaseModel):
    fp: Optional[str] = Field(default=None, max_length=128)
    ts: Optional[Union[int, float, str]] = None
    website: Optional[str] = Field(default=None, max_length=500)
    cf_turnstile_response: Optional[str] = Field(default=None, max_length=4096)


class SignupIn(_FormBase):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)
    name: str = Field(default="", max_length=120)
    invite: Optional[str] = Field(default=None, max_length=200)
    ref: Optional[str] = Field(default=None, max_length=40)


class LoginIn(_FormBase):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)
    next: Optional[str] = Field(default=None, max_length=300)


class MfaLoginIn(BaseModel):
    mfa_token: str = Field(max_length=200)
    code: str = Field(max_length=32)


class EmailIn(_FormBase):
    email: str = Field(max_length=320)
    next: Optional[str] = Field(default=None, max_length=300)


class ResetIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class ReauthIn(BaseModel):
    password: Optional[str] = Field(default=None, max_length=1024)
    code: Optional[str] = Field(default=None, max_length=32)


class CodeIn(BaseModel):
    code: str = Field(max_length=32)


class ProfileIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    timezone: Optional[str] = Field(default=None, max_length=64)  # IANA, e.g. "Asia/Kolkata"


class PasswordChangeIn(BaseModel):
    current_password: Optional[str] = Field(default=None, max_length=1024)
    new_password: str = Field(max_length=1024)


class DeleteIn(BaseModel):
    confirm: str = Field(max_length=20)


class KeyIn(BaseModel):
    name: str = Field(default="API key", max_length=80)
    scopes: Optional[list[str]] = Field(default=None, max_length=10)


# ---- helpers --------------------------------------------------------------------------------------

def _email_bucket(email: str) -> str:
    return "acct:" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]


def _require_turnstile(token: Optional[str], request: Request) -> None:
    if not compat.turnstile_ok(token, client_ip(request)):
        raise ApiError(400, "captcha_failed", "Please complete the human check and try again.")


def _session_response(db: Session, request: Request, user: User, *, method: str, mfa: bool,
                      body: Optional[dict] = None, redirect_to: Optional[str] = None) -> Response:
    """Revoke whatever session the browser presented, create a fresh one, commit, set the cookie."""
    sessions.revoke_presented(db, request, reason="replaced")
    sess, raw = sessions.create(db, user, request, method=method, mfa=mfa)
    compat.link_identity(db, request, user.id)
    db.commit()
    if redirect_to is not None:
        resp: Response = RedirectResponse(redirect_to, status_code=303)
    else:
        resp = JSONResponse(body if body is not None else {"ok": True, "user": service.public_user(db, user)})
    sessions.set_cookie(resp, raw, sess)
    return resp


def _signup_cookie_value(email: str) -> str:
    exp = int(time.time()) + settings.verify_token_hours * 3600
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]
    msg = f"{exp}.{digest}"
    return f"{msg}.{sign('signup-browser', msg)}"


def _signup_cookie_matches(raw: Optional[str], email: str) -> bool:
    if not raw or raw.count(".") != 2:
        return False
    exp_s, digest, sig = raw.split(".")
    if not verify_sig("signup-browser", f"{exp_s}.{digest}", sig):
        return False
    try:
        if int(exp_s) < time.time():
            return False
    except ValueError:
        return False
    return digest == hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]


def _set_signup_cookie(resp: Response, email: str) -> None:
    resp.set_cookie(SIGNUP_COOKIE, _signup_cookie_value(email), max_age=settings.verify_token_hours * 3600,
                    httponly=True, samesite="lax", secure=settings.is_prod, path="/auth")


# ---- signup / verify ------------------------------------------------------------------------------

@router.post("/auth/signup", status_code=201, dependencies=_PASSWORD_AUTH)
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)):
    compat.rate_limit(request, "signup_ip", settings.signup_max_per_ip_hour, 3600)
    email = service.normalise_email(body.email)
    _require_turnstile(body.cf_turnstile_response, request)
    fp = compat.clean_fingerprint(body.fp)
    refusal = passwords.check_new_password(body.password, email=email, name=body.name)
    if refusal:
        raise ApiError(400, refusal[0], refusal[1])

    # Invite and risk gates run before the existence check, so a new and an existing address are refused
    # (or accepted) identically.
    gates = service.signup_gates(db, request, email=email, invite=body.invite, fingerprint=fp,
                                 honeypot=body.website, form_ts=body.ts, turnstile_ok=True, method="password")
    existing = service.find_user_by_email(db, email)
    if existing is not None:
        db.rollback()  # nothing from the gates (e.g. a redeemed invite) is kept for an existing account
        passwords.hash_password(body.password)  # same work as the new-account path
        if existing.status == "active" and compat.rate_hit(request, "signup_exists_mail", 2, 3600,
                                                           key=_email_bucket(email)):
            mailer.send_account_exists(email)
        resp = JSONResponse({"ok": True, "verify_required": True}, status_code=201)
        _set_signup_cookie(resp, email)
        return resp

    user = service.create_account(db, request, email=email, password=body.password, name=body.name,
                                  invite=body.invite, ref=body.ref, fingerprint=fp, method="password", gates=gates)
    raw = service.issue_email_token(db, user, "verify", ip=client_ip(request))
    db.commit()
    mailer.send_verify(email, raw)
    resp = JSONResponse({"ok": True, "verify_required": True}, status_code=201)
    _set_signup_cookie(resp, email)
    return resp


def _to_google_login() -> RedirectResponse:
    """Where a stale password-era link (verify, magic, reset) lands in Google-only mode."""
    return RedirectResponse("/login?notice=google_only", status_code=303)


@router.get("/auth/verify/{token}")
def verify_email(token: str, request: Request, db: Session = Depends(get_db)):
    if policy.google_only():
        return _to_google_login()
    found = service.consume_email_token(db, token, "verify")
    if found is None:
        owner = service.token_owner(db, token, "verify")
        db.rollback()
        if owner is not None and owner.email_verified_at is not None:
            return RedirectResponse("/login?verified=1", status_code=303)
        return RedirectResponse("/verify-email?status=invalid", status_code=303)
    _tok, user = found
    service.mark_verified(db, user)
    compat.log_event(db, "email_verified", "info", ip=client_ip(request), user_id=user.id)
    current = sessions.from_request(db, request)
    if current is not None and current[1].id == user.id:
        db.commit()
        return RedirectResponse("/app?verified=1", status_code=303)
    same_browser = _signup_cookie_matches(request.cookies.get(SIGNUP_COOKIE), user.email)
    if same_browser and not service.has_totp(db, user.id):
        resp = _session_response(db, request, user, method="verify", mfa=False, redirect_to="/app?verified=1")
        resp.delete_cookie(SIGNUP_COOKIE, path="/auth")
        return resp
    db.commit()
    return RedirectResponse("/login?verified=1", status_code=303)


@router.post("/auth/verify/resend", dependencies=_PASSWORD_AUTH)
def resend_verification(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if user.email_verified_at is not None:
        return {"ok": True, "already_verified": True}
    compat.rate_limit(request, "verify_resend", 3, 3600, key=f"user:{user.id}")
    raw = service.issue_email_token(db, user, "verify", ip=client_ip(request))
    db.commit()
    mailer.send_verify(user.email, raw)
    return {"ok": True}


# ---- login / logout -------------------------------------------------------------------------------

@router.post("/auth/login", dependencies=_PASSWORD_AUTH)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    compat.rate_limit(request, "login_ip", settings.login_max_per_ip_15m, 900)
    invalid = ApiError(401, "invalid_credentials", "Email or password is incorrect.")
    try:
        email = service.normalise_email(body.email)
    except ApiError:
        passwords.verify_password(None, body.password)
        raise invalid
    compat.rate_limit(request, "login_acct", settings.login_max_per_account_15m, 900, key=_email_bucket(email))
    _require_turnstile(body.cf_turnstile_response, request)
    if body.website not in (None, "") and str(body.website).strip():  # honeypot
        passwords.verify_password(None, body.password)
        raise invalid
    user = service.authenticate_password(db, email, body.password)
    if user is None:
        compat.log_event(db, "login_failed", "low", ip=client_ip(request), detail={"email_hash": _email_bucket(email)})
        db.commit()
        raise invalid
    next_path = service.safe_next(body.next)
    if service.has_totp(db, user.id):
        raw = service.begin_mfa_challenge(db, user, method="password", next_path=next_path, ip=client_ip(request))
        db.commit()
        return {"ok": False, "mfa_required": True, "mfa_token": raw}
    return _session_response(db, request, user, method="password", mfa=False,
                             body=None)


@router.post("/auth/login/mfa")
def login_mfa(body: MfaLoginIn, request: Request, db: Session = Depends(get_db)):
    compat.rate_limit(request, "login_mfa", 20, 900)
    done = service.complete_mfa_challenge(db, body.mfa_token, body.code)
    if done is None:
        db.commit()  # keep the attempt counter
        raise ApiError(401, "invalid_code", "That code didn't work. Check your authenticator app and try again.")
    ch, user = done
    # `next` is where the sign-in that raised this challenge was heading (Google's callback, a magic link).
    return _session_response(db, request, user, method=ch.method, mfa=True,
                             body={"ok": True, "user": service.public_user(db, user),
                                   "next": service.safe_next(ch.next_path)})


@router.post("/auth/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    sessions.revoke_presented(db, request, reason="logout")
    db.commit()
    resp = JSONResponse({"ok": True})
    sessions.clear_cookie(resp)
    return resp


# ---- magic link -----------------------------------------------------------------------------------

@router.post("/auth/magic", dependencies=_PASSWORD_AUTH)
def request_magic_link(body: EmailIn, request: Request, db: Session = Depends(get_db)):
    compat.rate_limit(request, "magic_ip", 10, 3600)
    _require_turnstile(body.cf_turnstile_response, request)
    try:
        email = service.normalise_email(body.email)
    except ApiError:
        return {"ok": True}
    if body.website not in (None, "") and str(body.website).strip():
        return {"ok": True}
    user = service.find_user_by_email(db, email)
    if user is not None and user.status == "active" and compat.rate_hit(request, "magic_email", 3, 3600,
                                                                         key=_email_bucket(email)):
        raw = service.issue_email_token(db, user, "magic", ip=client_ip(request), next_path=service.safe_next(body.next))
        db.commit()
        mailer.send_magic(email, raw)
    return {"ok": True}


def _page(request: Request, title: str, body_html: str) -> HTMLResponse:
    doc = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
           f"<meta name=\"robots\" content=\"noindex\"><title>{html.escape(title)} · {html.escape(settings.brand_name)}"
           f"</title></head><body><main>{body_html}</main></body></html>")
    # same-origin, not no-referrer: under no-referrer browsers send `Origin: null` on this page's form POST, and
    # the CSRF layer rightly refuses a null origin. same-origin still never leaks the token URL to another site.
    return HTMLResponse(doc, headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})


@router.get("/auth/magic/{token}")
def magic_confirm_page(token: str, request: Request, db: Session = Depends(get_db)):
    if policy.google_only():
        return _to_google_login()
    found = service.peek_email_token(db, token, "magic")
    if found is None:
        return RedirectResponse("/login?error=link_expired", status_code=303)
    tok, user = found
    current = sessions.from_request(db, request)
    if current is not None and current[1].id == user.id:
        return RedirectResponse(service.safe_next(tok.next_path), status_code=303)
    if current is not None:
        resp = _page(request, "Already signed in",
                     "<h1>You're signed in to another account</h1>"
                     "<p>Sign out first, then open the link from your email again.</p>"
                     "<p><a href=\"/app\">Back to the app</a></p>")
        return resp
    action = html.escape(f"/auth/magic/{token}", quote=True)
    resp = _page(request, "Sign in",
                 f"<h1>Sign in to {html.escape(settings.brand_name)}</h1>"
                 f"<p>Continue as <strong>{html.escape(user.email)}</strong>?</p>"
                 f"<form method=\"post\" action=\"{action}\"><button type=\"submit\">Sign in</button></form>")
    if request.cookies.get(sessions.COOKIE):  # a dead sid would make the CSRF layer demand a header
        sessions.clear_cookie(resp)
    return resp


@router.post("/auth/magic/{token}")
def magic_consume(token: str, request: Request, db: Session = Depends(get_db)):
    if policy.google_only():
        return _to_google_login()
    compat.rate_limit(request, "magic_consume", 20, 900)
    found = service.consume_email_token(db, token, "magic")
    if found is None:
        db.rollback()
        return RedirectResponse("/login?error=link_expired", status_code=303)
    tok, user = found
    if user.email_verified_at is None:
        service.mark_verified(db, user)  # the link proves control of the inbox
    next_path = service.safe_next(tok.next_path)
    if service.has_totp(db, user.id):
        raw = service.begin_mfa_challenge(db, user, method="magic", next_path=next_path, ip=client_ip(request))
        db.commit()
        return RedirectResponse(_mfa_url(raw, next_path), status_code=303)
    return _session_response(db, request, user, method="magic", mfa=False, redirect_to=next_path)


# ---- forgot / reset -------------------------------------------------------------------------------

@router.post("/auth/forgot", dependencies=_PASSWORD_AUTH)
def forgot_password(body: EmailIn, request: Request, db: Session = Depends(get_db)):
    compat.rate_limit(request, "forgot_ip", 10, 3600)
    _require_turnstile(body.cf_turnstile_response, request)
    try:
        email = service.normalise_email(body.email)
    except ApiError:
        return {"ok": True}
    if body.website not in (None, "") and str(body.website).strip():
        return {"ok": True}
    user = service.find_user_by_email(db, email)
    if user is not None and user.status == "active" and compat.rate_hit(request, "forgot_email", 3, 3600,
                                                                         key=_email_bucket(email)):
        raw = service.issue_email_token(db, user, "reset", ip=client_ip(request))
        db.commit()
        mailer.send_reset(email, raw)
    return {"ok": True}


@router.post("/auth/reset", dependencies=_PASSWORD_AUTH)
def reset_password(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    compat.rate_limit(request, "reset_ip", 20, 3600)
    found = service.peek_email_token(db, body.token, "reset")
    if found is None:
        raise ApiError(400, "invalid_token", "This reset link is invalid or has expired. Request a new one.")
    _tok, user = found
    refusal = passwords.check_new_password(body.password, email=user.email, name=user.name)
    if refusal:  # checked before consuming, so a weak choice doesn't burn the link
        raise ApiError(400, refusal[0], refusal[1])
    if service.consume_email_token(db, body.token, "reset") is None:
        db.rollback()
        raise ApiError(400, "invalid_token", "This reset link is invalid or has expired. Request a new one.")
    service.set_password(db, user, body.password)
    if user.email_verified_at is None:
        service.mark_verified(db, user)
    sessions.revoke_all(db, user.id, reason="password_reset")
    compat.log_event(db, "password_reset", "info", ip=client_ip(request), user_id=user.id)
    db.commit()
    mailer.send_password_changed(user.email)
    resp = JSONResponse({"ok": True})
    sessions.clear_cookie(resp)
    return resp


# ---- Google ---------------------------------------------------------------------------------------
#
# Every way out of the round trip that is not a signed-in redirect is a 303 to /login?error=<code> (with the
# attempt's next/invite/reauth), which renders a page for that code with a retry button. Redirecting rather
# than rendering on the callback URL means a refresh never replays Google's code.

# What the callback can raise -> the page code /login renders (saas.web.app_pages.AUTH_ERRORS).
_CALLBACK_CODES = {
    "oauth_code": "google_failed", "oauth_exchange": "google_failed", "oauth_token": "google_failed",
    "oauth_unreachable": "google_unreachable", "invalid_email": "google_failed",
}


def _mfa_url(raw: str, next_path: Optional[str]) -> str:
    nxt = service.safe_next(next_path)
    return f"/login?mfa={raw}" + (f"&{urlencode({'next': nxt})}" if nxt != "/app" else "")


def _login_error(code: str, row=None, *, next_path: Optional[str] = None, invite: Optional[str] = None,
                 reauth: bool = False) -> RedirectResponse:
    """303 to the sign-in page's error view; its retry button starts the same attempt again."""
    data = (row.data or {}) if row is not None else {}
    nxt = service.safe_next(next_path or (row.next_path if row is not None else None))
    invite = invite or data.get("invite")
    params = {"error": code}
    if nxt != "/app":
        params["next"] = nxt
    if invite:
        params["invite"] = str(invite)[:64]
    if reauth or data.get("reauth_user_id"):
        params["reauth"] = "1"
    resp = RedirectResponse("/login?" + urlencode(params), status_code=303)
    resp.delete_cookie(google.STATE_COOKIE, path="/auth/google")
    return resp


def _wants_page(request: Request) -> bool:
    """A browser navigation (a link or a button), as opposed to a script asking for JSON."""
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "application/json" not in accept


def _invite_email(db: Session, code: Optional[str]) -> Optional[str]:
    """The address an invite was issued to, for Google's account chooser. None when unknown or unusable."""
    code = (code or "").strip().upper()
    if not code or len(code) > 32 or settings.launch_mode != "waitlist":
        return None
    try:
        from ..models.waitlist import WaitlistEntry
    except Exception:  # pragma: no cover - the waitlist package is part of the app
        return None
    entry = db.query(WaitlistEntry).filter(WaitlistEntry.invite_code == code).one_or_none()
    return entry.email if entry is not None and entry.status in ("invited", "joined") else None


@router.get("/auth/google/start")
def google_start(request: Request, next: Optional[str] = None, invite: Optional[str] = None,
                 ref: Optional[str] = None, fp: Optional[str] = None, link: int = 0, reauth: int = 0,
                 db: Session = Depends(get_db)):
    """303 to Google. `reauth=1` confirms the signed-in user (the fresh session resets the re-auth clock);
    `link=1` attaches Google to the signed-in account; `invite` carries a waitlist invite to account creation."""
    next_path = service.safe_next(next)
    page = _wants_page(request)
    if not google.enabled():
        if page:
            return _login_error("google_unavailable", next_path=next_path, invite=invite)
        raise ApiError(404, "google_disabled", "Google sign-in is not available.")
    try:
        compat.rate_limit(request, "google_start", 20, 600)
    except ApiError as exc:
        if page and exc.status == 429:
            return _login_error("rate_limited", next_path=next_path, invite=invite, reauth=bool(reauth))
        raise
    if not (link or reauth) and page and sessions.from_request(db, request) is not None:
        # Already signed in (a "Sign in" link on a marketing page, a second tab): no need to visit Google.
        return RedirectResponse(next_path, status_code=303)
    data: dict[str, Any] = {"invite": (invite or "")[:200] or None, "ref": (ref or "")[:40] or None,
                            "fp": compat.clean_fingerprint(fp)}
    hint = _invite_email(db, invite)
    if link or reauth:
        current = sessions.from_request(db, request)
        if current is None:
            if page:  # the session ended meanwhile: a normal sign-in gets them back to the same place
                return RedirectResponse("/login?" + urlencode({"next": next_path}), status_code=303)
            raise ApiError(401, "unauthenticated", "Sign in first.")
        data["link_user_id" if link else "reauth_user_id"] = current[1].id
        hint = current[1].email
    url, state = google.start(db, next_path=next_path, data=data, login_hint=hint)
    db.commit()
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(google.STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                    secure=settings.is_prod, path="/auth/google")
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, state: Optional[str] = None, code: Optional[str] = None,
                    error: Optional[str] = None, db: Session = Depends(get_db)):
    if error:  # Google sends `state` back with the error, so the retry can keep next/invite
        row = google.find_state(db, state)
        if row is not None and row.used_at is None:
            row.used_at = utcnow()
            db.commit()
        return _login_error(google.error_code(error), row)
    try:
        row = google.consume_state(db, state, request.cookies.get(google.STATE_COOKIE))
    except ApiError as exc:
        db.rollback()
        old = google.find_state(db, state)
        if exc.code == google.STATE_EXPIRED and sessions.from_request(db, request) is not None:
            # Refresh or Back after a finished sign-in: this browser is signed in, so just carry on.
            resp = RedirectResponse(service.safe_next(old.next_path if old else None), status_code=303)
            resp.delete_cookie(google.STATE_COOKIE, path="/auth/google")
            return resp
        return _login_error(exc.code, old)
    db.commit()  # the attempt is spent whatever happens next
    try:
        tokens = google.exchange_code(code or "", row.code_verifier)
        claims = google.validate_id_token(tokens["id_token"], nonce=row.nonce)
        user, created = _google_user(db, request, claims, row.data or {})
    except ApiError as exc:
        db.rollback()
        log.info("google sign-in refused: %s", exc.code)
        return _login_error(_CALLBACK_CODES.get(exc.code, exc.code), row)
    if user.status != "active":
        db.rollback()
        return _login_error("account_unavailable", row)
    next_path = service.safe_next(row.next_path)
    if created and next_path == "/app":
        next_path = "/app?welcome=1"
    if service.has_totp(db, user.id):
        raw = service.begin_mfa_challenge(db, user, method="google", next_path=next_path, ip=client_ip(request))
        db.commit()
        resp = RedirectResponse(_mfa_url(raw, next_path), status_code=303)
    else:
        resp = _session_response(db, request, user, method="google", mfa=False, redirect_to=next_path)
    resp.delete_cookie(google.STATE_COOKIE, path="/auth/google")
    return resp


def _google_user(db: Session, request: Request, claims: dict, data: dict) -> tuple[User, bool]:
    """(user, created). validate_id_token has already refused an address Google has not verified, so an
    existing account is only ever linked to a Google identity whose email Google vouches for."""
    sub = str(claims["sub"])
    email = service.normalise_email(claims["email"])
    ident = db.query(Identity).filter(Identity.provider == "google", Identity.subject == sub).one_or_none()
    link_user_id = data.get("link_user_id")
    reauth_user_id = data.get("reauth_user_id")
    if ident is not None:
        if link_user_id and ident.user_id != link_user_id:
            raise ApiError(409, "google_linked_elsewhere", "That Google account is linked to another user.")
        if reauth_user_id and ident.user_id != reauth_user_id:
            raise ApiError(409, "reauth_other_account", "That is a different Google account.")
        ident.last_login_at = utcnow()
        user = db.get(User, ident.user_id)
        if user is None:
            raise ApiError(400, "account_unavailable", "Account unavailable.")
        service.apply_admin_list(db, user)
        return user, False
    created = False
    if link_user_id:
        user = db.get(User, link_user_id)
        if user is None:
            raise ApiError(400, "account_unavailable", "Account unavailable.")
    else:
        user = service.find_user_for_google(db, email)
        if reauth_user_id and (user is None or user.id != reauth_user_id):
            raise ApiError(409, "reauth_other_account", "That is a different Google account.")
        if user is not None and user.email_verified_at is None:
            # Pre-hijack defence: someone may have registered this address with a password before its
            # owner arrived through Google. Google proves the owner, so the squatter's password and
            # sessions go. (An admin row from `saas_manage.py grant-admin` has no password and is claimed
            # here too, quietly.)
            had_password = bool(user.password_hash)
            user.password_hash = None
            sessions.revoke_all(db, user.id, reason="google_takeover_guard")
            if had_password:
                compat.log_event(db, "prehijack_reset", "medium", ip=client_ip(request), user_id=user.id)
            service.mark_verified(db, user)
    if user is None:
        user = service.create_account(db, request, email=email, name=str(claims.get("name") or "")[:120],
                                      invite=data.get("invite"), ref=data.get("ref"), fingerprint=data.get("fp"),
                                      method="google", email_verified=True)
        created = True
    service.apply_admin_list(db, user)
    db.add(Identity(user_id=user.id, provider="google", subject=sub, email=email, last_login_at=utcnow()))
    db.flush()
    return user, created


# ---- re-auth & step-up ----------------------------------------------------------------------------

def _session_and_user(request: Request, db: Session) -> tuple[AuthSession, User]:
    found = sessions.from_request(db, request)
    if found is None:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    return found


@router.post("/auth/reauth")
def reauth(body: ReauthIn, request: Request, db: Session = Depends(get_db)):
    sess, user = _session_and_user(request, db)
    compat.rate_limit(request, "reauth", 10, 900, key=f"user:{user.id}")
    if body.password and policy.google_only():
        raise ApiError(400, "google_only", "Confirm with Google, or with a code from your authenticator app.",
                       google="/auth/google/start?reauth=1")
    ok = False
    if body.password:
        ok = passwords.verify_password(user.password_hash, body.password)
    elif body.code and service.has_totp(db, user.id):
        ok = totp.verify(db, user, body.code)
    if not ok:
        db.commit()
        raise ApiError(401, "invalid_credentials", "That didn't match. Try again.")
    sess.reauth_at = utcnow()
    db.commit()
    from datetime import timedelta
    return {"ok": True, "until": (sess.reauth_at + timedelta(minutes=settings.reauth_minutes)).isoformat()}


@router.post("/auth/mfa/verify")
def mfa_step_up(body: CodeIn, request: Request, db: Session = Depends(get_db)):
    sess, user = _session_and_user(request, db)
    compat.rate_limit(request, "mfa_step_up", 10, 900, key=f"user:{user.id}")
    if not totp.verify(db, user, body.code):
        db.commit()
        raise ApiError(401, "invalid_code", "That code didn't work.")
    sess.mfa_at = utcnow()
    sess.reauth_at = sess.mfa_at
    raw = sessions.rotate(db, sess)  # privilege change: the pre-step-up token stops working
    db.commit()
    resp = JSONResponse({"ok": True})
    sessions.set_cookie(resp, raw, sess)
    return resp


# ---- account --------------------------------------------------------------------------------------

@router.get("/api/me")
def me(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return service.public_user(db, user)


@router.patch("/api/me")
def update_me(body: ProfileIn, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if body.name is not None:
        user.name = body.name.strip()[:120]
    if body.timezone is not None:
        from ..jobs.schedule import valid_timezone

        tz = valid_timezone(body.timezone)
        if tz is None:
            raise ApiError(400, "invalid_timezone", "Use an IANA time zone such as 'Asia/Kolkata' or 'America/New_York'.")
        user.timezone = tz
    db.commit()
    return service.public_user(db, user)


@router.post("/api/me/password", dependencies=_PASSWORD_AUTH)
def change_password(body: PasswordChangeIn, request: Request, user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    compat.rate_limit(request, "password_change", 10, 3600, key=f"user:{user.id}")
    sess = request.state.auth_session
    if user.password_hash:
        if not body.current_password or not passwords.verify_password(user.password_hash, body.current_password):
            raise ApiError(401, "invalid_credentials", "Your current password is incorrect.")
    elif not sessions.recently_authenticated(sess):
        raise ApiError(403, "reauth_required", "Confirm it's you first.")
    service.set_password(db, user, body.new_password)
    sessions.revoke_all(db, user.id, reason="password_changed", except_id=sess.id)
    raw = sessions.rotate(db, sess)
    sess.reauth_at = utcnow()
    db.commit()
    mailer.send_password_changed(user.email)
    resp = JSONResponse({"ok": True})
    sessions.set_cookie(resp, raw, sess)
    return resp


@router.get("/api/me/sessions")
def list_sessions(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    current = request.state.auth_session
    now = utcnow()
    rows = (db.query(AuthSession)
            .filter(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
            .order_by(AuthSession.last_seen_at.desc()).limit(50).all())
    return {"sessions": [{"id": s.id, "current": s.id == current.id, "ip": s.ip, "user_agent": s.user_agent,
                          "country": s.country, "method": s.method, "created_at": s.created_at.isoformat(),
                          "last_seen_at": s.last_seen_at.isoformat()}
                         for s in rows if s.last_seen_at + sessions.idle_limit(user) > now]}


@router.delete("/api/me/sessions/{session_id}")
def revoke_session(session_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    s = db.query(AuthSession).filter(AuthSession.id == session_id, AuthSession.user_id == user.id).one_or_none()
    if s is None:
        raise ApiError(404, "not_found", "Session not found.")
    sessions.revoke(db, s, "user_revoked")
    db.commit()
    return {"ok": True}


@router.post("/api/me/sessions/revoke-others")
def revoke_other_sessions(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    n = sessions.revoke_all(db, user.id, reason="user_revoked_others", except_id=request.state.auth_session.id)
    db.commit()
    return {"ok": True, "revoked": n}


@router.post("/api/me/mfa/totp/setup")
def totp_setup(user: User = Depends(require_user), _sess: AuthSession = Depends(require_recent_auth),
               db: Session = Depends(get_db)):
    try:
        out = totp.begin_setup(db, user)
    except ValueError:
        raise ApiError(409, "mfa_already_enabled", "Two-factor authentication is already on.")
    db.commit()
    return {"secret": out["secret"], "otpauth_uri": out["otpauth_uri"], "qr_svg": out["qr_svg"],
            "qr_data_uri": out["qr_data_uri"]}


@router.post("/api/me/mfa/totp/confirm")
def totp_confirm(body: CodeIn, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    compat.rate_limit(request, "totp_confirm", 10, 900, key=f"user:{user.id}")
    codes = totp.confirm_setup(db, user, body.code)
    if codes is None:
        db.rollback()
        raise ApiError(400, "invalid_code", "That code didn't match. Make sure your phone's clock is right.")
    sess = request.state.auth_session
    sess.mfa_at = utcnow()
    raw = sessions.rotate(db, sess)
    db.commit()
    if policy.mail_allowed():
        mailer.send_mfa_changed(user.email, True)
    resp = JSONResponse({"ok": True, "recovery_codes": codes})
    sessions.set_cookie(resp, raw, sess)
    return resp


@router.post("/api/me/mfa/totp/disable")
def totp_disable(body: CodeIn, user: User = Depends(require_user), _sess: AuthSession = Depends(require_recent_auth),
                 db: Session = Depends(get_db)):
    if user.role == "admin":
        raise ApiError(403, "mfa_mandatory", "Admins can't turn off two-factor authentication.")
    if not totp.verify(db, user, body.code):
        db.commit()
        raise ApiError(401, "invalid_code", "That code didn't work.")
    totp.disable(db, user)
    db.commit()
    if policy.mail_allowed():
        mailer.send_mfa_changed(user.email, False)
    return {"ok": True}


@router.post("/api/me/mfa/recovery-codes")
def regenerate_codes(user: User = Depends(require_user), _sess: AuthSession = Depends(require_recent_auth),
                     db: Session = Depends(get_db)):
    if not service.has_totp(db, user.id):
        raise ApiError(400, "mfa_not_enabled", "Turn on two-factor authentication first.")
    codes = totp.regenerate_recovery_codes(db, user)
    db.commit()
    return {"recovery_codes": codes}


@router.get("/api/me/export")
def export_me(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    compat.rate_limit(request, "export", 5, 3600, key=f"user:{user.id}")
    data = service.export_account(db, user)
    return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=\"account-export.json\""})


@router.post("/api/me/delete")
def delete_me(body: DeleteIn, request: Request, user: User = Depends(require_user),
              _sess: AuthSession = Depends(require_recent_auth), db: Session = Depends(get_db)):
    if body.confirm != "DELETE":
        raise ApiError(400, "confirm_required", "Type DELETE to confirm.")
    if user.role == "admin":
        raise ApiError(403, "forbidden", "Admin accounts can't be deleted from here.")
    compat.log_event(db, "account_deleted", "info", ip=client_ip(request), user_id=user.id)
    service.delete_account(db, user)
    db.commit()
    resp = JSONResponse({"ok": True})
    sessions.clear_cookie(resp)
    return resp


# ---- API keys -------------------------------------------------------------------------------------

@router.get("/api/keys")
def list_keys(user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ..models.core import ApiKey

    rows = db.query(ApiKey).filter(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc()).limit(50).all()
    return {"keys": [keys_mod.public(k) for k in rows], "scopes": list(keys_mod.ALL_SCOPES),
            "api_enabled": keys_mod.plan_allows_api(db, user)}


@router.post("/api/keys", status_code=201)
def create_key(body: KeyIn, request: Request, user: User = Depends(require_verified_user),
               db: Session = Depends(get_db)):
    compat.rate_limit(request, "key_create", 10, 3600, key=f"user:{user.id}")
    if not keys_mod.plan_allows_api(db, user):
        raise ApiError(403, "plan_required", "API and MCP access come with any paid plan.")
    key, raw = keys_mod.create(db, user, body.name, body.scopes)
    compat.log_event(db, "api_key_created", "info", ip=client_ip(request), user_id=user.id, detail={"key": key.id})
    db.commit()
    return {**keys_mod.public(key), "key": raw}


@router.delete("/api/keys/{key_id}")
def delete_key(key_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not keys_mod.revoke(db, user, key_id):
        raise ApiError(404, "not_found", "Key not found.")
    db.commit()
    return {"ok": True}
