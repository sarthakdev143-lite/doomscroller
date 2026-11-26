"""The signed-in product and the sign-in pages, served by the Python app only (never the static export).

Pages render a shell and fetch their data from the JSON API in the browser (docs/saas/CONTRACT.md), so
every number a page shows is the one the API enforces. Registered on saas.web.routes.router.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import google
from ..auth.deps import optional_user, require_user
from ..auth.service import safe_next
from ..config import settings
from ..db import get_db
from ..models.core import User
from .routes import FREE_MINUTES, FREE_SHORTS, _render, _source_names, router

_GOOGLE_ERRORS = {
    "google_denied": "Google sign-in was cancelled.",
    "google_failed": "Google sign-in didn’t work. Try again, or use your email.",
    "account_disabled": "This account can’t sign in. Contact support.",
}


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
                   user_verified=user.email_verified_at is not None, free_grant=_free_grant(db), **extra)


# ---- sign-in pages --------------------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request, user: Optional[User] = Depends(optional_user)):
    q = request.query_params
    nxt = safe_next(q.get("next"))
    mfa = (q.get("mfa") or "")[:200]
    if user is not None and not mfa:
        return RedirectResponse(nxt, status_code=303)
    return _auth_page(request, "auth/login.html", "auth-login", next_path=nxt, mfa_token=mfa,
                      verified=q.get("verified") == "1", reset=q.get("reset") == "1",
                      error_message=_GOOGLE_ERRORS.get(q.get("error") or "", "Sign-in didn’t work. Try again." if q.get("error") else ""))


@router.get("/signup")
def signup_page(request: Request, user: Optional[User] = Depends(optional_user)):
    if user is not None:
        return RedirectResponse("/app", status_code=303)
    q = request.query_params
    return _auth_page(request, "auth/signup.html", "auth-signup", invite=(q.get("invite") or "")[:64],
                      ref=(q.get("ref") or "")[:64])


@router.get("/verify-email")
def verify_email_page(request: Request, user: Optional[User] = Depends(optional_user)):
    if user is not None and user.email_verified_at is not None:
        return RedirectResponse("/app", status_code=303)
    return _auth_page(request, "auth/verify_email.html", "auth-verify", status=request.query_params.get("status") or "",
                      signed_in=user is not None, signed_in_email=user.email if user else "")


@router.get("/forgot")
def forgot_page(request: Request):
    return _auth_page(request, "auth/forgot.html", "auth-forgot")


@router.get("/reset/{token}")
def reset_page(request: Request, token: str):
    return _auth_page(request, "auth/reset.html", "auth-reset", token=token[:200])


# ---- the app --------------------------------------------------------------------------------------

@router.get("/app")
def app_dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _app_page(request, db, user, "app/dashboard.html", "dashboard",
                     verified_now=request.query_params.get("verified") == "1")


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
