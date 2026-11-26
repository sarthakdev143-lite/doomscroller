"""Waitlist endpoints.

POST /api/waitlist                 join (JSON or form): {email, role?, channel_url?, ref?, fp, ts, website,
                                   cf_turnstile_response?} -> {ok, position, referral_url, already, ...}
GET  /waitlist/confirm/{token}     interstitial page; a script (or the button) POSTs the confirmation, so
                                   mail scanners that pre-fetch links do not confirm on someone's behalf
POST /waitlist/confirm/{token}     confirm -> 303 to /waitlist/thanks?code=<referral code>&confirmed=1
GET  /api/waitlist/me?code=<code>  position / referrals for a referral code (no email in the response)
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models.waitlist import WaitlistEntry
from ..security import limits
from ..templating import templates
from . import service

router = APIRouter()


async def _payload(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    try:
        if "json" in ctype:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        form = await request.form()
        return {k: v for k, v in form.items() if isinstance(v, str)}
    except Exception:
        return {}


@router.post("/api/waitlist")
async def join_waitlist(request: Request, db: Session = Depends(get_db)):
    import anyio

    def limit():
        limits.enforce(request, "wl_join_min", 4, 60)
        limits.enforce(request, "wl_join_hour", 12, 3600)

    await anyio.to_thread.run_sync(limit)
    data = await _payload(request)
    if not data:
        return JSONResponse({"ok": False, "error": "bad_request", "message": "Send a JSON body with an email."},
                            status_code=400)
    email_key = str(data.get("email") or "").strip().lower()[:320]
    if email_key:
        await anyio.to_thread.run_sync(lambda: limits.enforce(request, "wl_join_email", 5, 3600, email_key))
    outcome = await anyio.to_thread.run_sync(lambda: service.join(db, request, data))
    return JSONResponse(outcome.body, status_code=outcome.status)


def _confirm_page(request: Request, token: str, state: str, status: int = 200):
    return templates.TemplateResponse(request, "public/waitlist_confirm.html",
                                      {"token": token, "state": state, "brand": settings.brand_name},
                                      status_code=status)


@router.get("/waitlist/confirm/{token}", response_class=HTMLResponse)
def confirm_page(token: str, request: Request, db: Session = Depends(get_db)):
    limits.enforce(request, "wl_confirm", 30, 600)
    entry = service.find_by_token(db, token)
    if entry is None or entry.status == "removed":
        return _confirm_page(request, "", "invalid", 404)
    if entry.status != "pending":
        return RedirectResponse(f"/waitlist/thanks?code={quote(entry.referral_code)}&confirmed=1", status_code=303)
    return _confirm_page(request, token, "ready")


@router.post("/waitlist/confirm/{token}")
async def confirm_submit(token: str, request: Request, db: Session = Depends(get_db)):
    import anyio

    await anyio.to_thread.run_sync(lambda: limits.enforce(request, "wl_confirm", 30, 600))
    data = await _payload(request)
    result, entry = await anyio.to_thread.run_sync(lambda: service.confirm(db, request, token, data.get("fp")))
    wants_json = "application/json" in request.headers.get("accept", "") or "json" in request.headers.get(
        "content-type", "")
    if result in ("confirmed", "already") and entry is not None:
        if wants_json:
            return JSONResponse(service.public_view(db, entry) | {"result": result})
        return RedirectResponse(f"/waitlist/thanks?code={quote(entry.referral_code)}&confirmed=1", status_code=303)
    status = 410 if result == "expired" else 404
    if wants_json:
        return JSONResponse({"ok": False, "error": result,
                             "message": "This link has expired. Join again to get a fresh one."
                             if result == "expired" else "This link is not valid."}, status_code=status)
    return _confirm_page(request, "", result, status)


@router.get("/api/waitlist/me")
def waitlist_me(code: str, request: Request, db: Session = Depends(get_db)):
    limits.enforce(request, "wl_me", 60, 60)
    code = (code or "").strip().lower()[:16]
    entry = db.execute(select(WaitlistEntry).where(WaitlistEntry.referral_code == code)).scalar_one_or_none() \
        if code else None
    if entry is None or entry.status == "removed":
        return JSONResponse({"ok": False, "error": "not_found", "message": "No waitlist entry for that code."},
                            status_code=404)
    return service.public_view(db, entry)
