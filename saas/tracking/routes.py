"""Tracker endpoints: GET /api/t/config, POST /api/t, POST /api/t/replay, POST /api/consent,
POST /api/t/csp (CSP violation reports).

/api/t* is exempt from CSRF (sendBeacon cannot set headers) but every POST must carry an Origin or
Referer of ours; a forged beacon from a script is still subject to per-IP and per-visitor limits and
can only ever write to its own visitor.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..common import utcnow
from ..config import settings
from ..db import get_db
from ..models.tracking import Visitor
from ..security import events, limits
from ..security.middleware import origin_ok
from ..templating import asset
from . import consent, identity, ingest, replay, retention

log = logging.getLogger("saas.tracking")

router = APIRouter()


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


@router.get("/api/t/config")
def tracker_config(request: Request):
    limits.enforce(request, "t_config", 120, 60)
    country = consent.country_of(request)
    required = consent.required(request, country)
    state = consent.state(request, country)
    allowed = settings.tracking_enabled and state in ("granted", "implied")
    vid = identity.visitor_id(request)
    body = {
        "tracking": settings.tracking_enabled,
        "consent_required": required,
        "consent": state,  # granted | implied | denied | pending
        "notice": not required,
        "gpc": consent.gpc(request),
        "banner_version": consent.BANNER_VERSION,
        "replay_sample": settings.replay_sample_rate,
        "replay": False,
        "replay_js": asset(replay.RECORD_JS),
        "privacy_url": "/privacy",
        "cookies_url": "/cookies",
        "brand": settings.brand_name,
    }
    set_vid = None
    if allowed:
        if not vid:
            vid = set_vid = identity.new_visitor_id()
        body["replay"] = replay.sampled(vid)
        body["vid"] = vid  # fallback for browsers that drop the cookie
    resp = JSONResponse(body)
    if set_vid:
        identity.set_cookie(resp, set_vid)
    elif vid and not allowed:
        identity.clear_cookie(resp)
    return resp


def _parse_json(body: bytes, limit: int):
    raw = replay.inflate(body, limit)
    return json.loads(raw)


@router.post("/api/t", status_code=202)
async def track(request: Request, db: Session = Depends(get_db)):
    if not settings.tracking_enabled:
        return {"ok": True, "stored": 0}
    if not origin_ok(request, require=True):
        return _err(403, "bad_origin", "Beacon from an unknown origin.")
    await anyio.to_thread.run_sync(limits.enforce, request, "t_ip", 300, 60)
    body = await request.body()
    if not body:
        return _err(400, "bad_payload", "Empty body.")
    try:
        data = _parse_json(body, 256 * 1024)
        batch = ingest.Batch.model_validate(data)
    except replay.ReplayRejected as exc:
        return _err(exc.status, "bad_payload", f"Unreadable body ({exc.code}).")
    except (ValueError, ValidationError, UnicodeDecodeError):
        return _err(400, "bad_payload", "Malformed tracking batch.")
    state = consent.state(request)
    if state not in ("granted", "implied"):
        return {"ok": True, "stored": 0, "reason": "consent"}
    vid = identity.visitor_id(request) or (batch.vid if identity.valid(batch.vid) else None)
    if vid:
        await anyio.to_thread.run_sync(limits.enforce, request, "t_vid", 120, 60, vid)
    result, set_vid = await anyio.to_thread.run_sync(
        lambda: ingest.process(db, request, batch, consent_state=state))
    resp = JSONResponse(result, status_code=202)
    if set_vid:
        identity.set_cookie(resp, set_vid)
    return resp


@router.post("/api/t/replay", status_code=202)
async def track_replay(request: Request, pv: str, seq: int, vid: str | None = None,
                       db: Session = Depends(get_db)):
    if not settings.tracking_enabled:
        return {"ok": True, "stored": False}
    if not origin_ok(request, require=True):
        return _err(403, "bad_origin", "Upload from an unknown origin.")
    if not consent.analytics_allowed(request):
        return _err(403, "no_consent", "Recording needs consent.")
    await anyio.to_thread.run_sync(limits.enforce, request, "t_replay_ip", 240, 60)
    visitor = identity.visitor_id(request) or (vid if identity.valid(vid) else None)
    if not visitor or not pv.startswith("p_") or len(pv) > 40:
        return _err(400, "bad_request", "Missing visitor or page id.")
    body = await request.body()
    if not body:
        return _err(400, "bad_payload", "Empty chunk.")
    try:
        chunk = await anyio.to_thread.run_sync(
            lambda: replay.store_chunk(db, visitor_id=visitor, page_id=pv, seq=seq, body=body))
    except replay.ReplayRejected as exc:
        return _err(exc.status, exc.code, "Replay chunk not stored.")
    return {"ok": True, "stored": True, "seq": chunk.seq}


@router.post("/api/consent")
async def set_consent(request: Request, db: Session = Depends(get_db)):
    await anyio.to_thread.run_sync(limits.enforce, request, "consent", 30, 60)
    choice = None
    ctype = request.headers.get("content-type", "")
    try:
        if "json" in ctype:
            data = await request.json()
            choice = data.get("choice") if isinstance(data, dict) else None
        else:
            form = await request.form()
            choice = form.get("choice")
    except Exception:
        choice = None
    if choice in ("accept", "accepted", "yes", "true", "1"):
        choice = "granted"
    if choice in ("decline", "declined", "reject", "no", "false", "0"):
        choice = "denied"
    if choice not in ("granted", "denied"):
        return _err(400, "bad_choice", "choice must be 'granted' or 'denied'.")

    vid = identity.visitor_id(request)
    body: dict = {"ok": True, "consent": choice}
    set_vid = clear_vid = False

    def work():
        nonlocal vid, set_vid, clear_vid
        if choice == "granted" and settings.tracking_enabled:
            if not vid:
                vid = identity.new_visitor_id()
                set_vid = True
            v = ingest.ensure_visitor(db, request, vid, "granted")
            client = dict(v.client or {})
            client["consent_version"] = consent.BANNER_VERSION
            v.client = client
            db.commit()
            body["vid"] = vid
            body["replay"] = replay.sampled(vid)
        elif choice == "denied" and vid:
            # Withdrawal: stop, and erase the behavioural record kept so far.
            v = db.get(Visitor, vid)
            if v is not None:
                retention.forget_visitor(db, vid)
                v.consent = "denied"
                v.consent_at = utcnow()
                db.commit()
            clear_vid = True

    await anyio.to_thread.run_sync(work)
    resp = JSONResponse(body)
    consent.set_cookie(resp, choice)
    if set_vid:
        identity.set_cookie(resp, vid)
    if clear_vid:
        identity.clear_cookie(resp)
    return resp


_csp_seen: dict[str, float] = {}
_csp_lock = threading.Lock()


@router.post("/api/t/csp", status_code=204)
async def csp_report(request: Request, db: Session = Depends(get_db)):
    """Browsers POST CSP violations here (report-uri). Deduped: one event per violation per hour."""
    if not limits.hit(request, "csp_report", 30, 60):
        return Response(status_code=204)
    try:
        data = json.loads((await request.body())[:16384] or b"{}")
    except ValueError:
        return Response(status_code=204)
    reports = data if isinstance(data, list) else [data]
    for rep in reports[:5]:
        if not isinstance(rep, dict):
            continue
        r = rep.get("csp-report") or rep.get("body") or rep
        if not isinstance(r, dict):
            continue
        directive = str(r.get("violated-directive") or r.get("effectiveDirective") or "")[:80]
        blocked = str(r.get("blocked-uri") or r.get("blockedURL") or "")[:200]
        page = ingest.clean_path(str(r.get("document-uri") or r.get("documentURL") or ""))
        key = f"{directive}|{blocked}|{page}"
        now = time.time()
        with _csp_lock:
            if _csp_seen.get(key, 0) > now - 3600:
                continue
            _csp_seen[key] = now
            if len(_csp_seen) > 5000:
                _csp_seen.clear()
        await anyio.to_thread.run_sync(lambda: events.log(
            db, "csp_violation", "low", detail={"directive": directive, "blocked": blocked, "page": page}))
    return Response(status_code=204)
