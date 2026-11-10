"""The phone control panel: review clips, run the channels, and see how the loop is doing.

It runs as its own small server (scripts/run.py starts it on port 8600) so that only these routes are
reachable when a tunnel exposes it, never the main API. Every route needs the REVIEW_TOKEN, given once as
?key= and then kept in a cookie. The page itself lives in app/review_ui.py.
"""
from __future__ import annotations

import hmac

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.config import settings
from app.db import init_db, session_scope
from app.growth import approval, controls, safety
from app.review_ui import LOCKED, PAGE

review_app = FastAPI(title="Doomscroller control panel", docs_url=None, redoc_url=None, openapi_url=None)
_COOKIE = "review_key"


@review_app.on_event("startup")
def _startup() -> None:
    init_db()


def _allowed(request: Request) -> bool:
    token = settings.review_token
    given = request.cookies.get(_COOKIE) or ""
    return bool(token) and hmac.compare_digest(given.encode(), token.encode())


def _guard(request: Request) -> None:
    if not settings.review_token:
        raise HTTPException(503, "REVIEW_TOKEN is not set, so the control panel is shut")
    if not _allowed(request):
        raise HTTPException(401, "open the link with ?key= first")


class Decision(BaseModel):
    title: str = ""


class NewChannel(BaseModel):
    slug: str
    name: str = ""
    niche: str = ""
    brand: str = ""
    uploads: int = 3
    timezone: str = ""
    feeds: list[str] = []


class ChannelSettings(BaseModel):
    name: str | None = None
    niche: str | None = None
    brand_name: str | None = None
    uploads_per_day: int | None = None
    timezone: str | None = None


class FeedEdit(BaseModel):
    add: list[str] = []
    remove: list[str] = []


class Unblock(BaseModel):
    name: str


@review_app.get("/", response_class=HTMLResponse)
def page(request: Request, key: str = ""):
    if key and settings.review_token and hmac.compare_digest(key.encode(), settings.review_token.encode()):
        response = RedirectResponse("/", status_code=303)
        secure = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
        response.set_cookie(_COOKIE, key, max_age=90 * 24 * 3600, httponly=True, samesite="lax", secure=secure)
        return response
    if not _allowed(request):
        return HTMLResponse(LOCKED, status_code=401 if settings.review_token else 503)
    return HTMLResponse(PAGE)


# ---------- Review ----------
@review_app.get("/api/queue")
def api_queue(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(approval.queue())


@review_app.post("/api/clips/{video_id}/approve")
def api_approve(video_id: str, decision: Decision, request: Request) -> dict:
    _guard(request)
    try:
        return approval.approve(video_id, decision.title)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


@review_app.post("/api/clips/{video_id}/reject")
def api_reject(video_id: str, request: Request) -> dict:
    _guard(request)
    try:
        return approval.reject(video_id)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


@review_app.post("/api/clips/{video_id}/undo")
def api_undo(video_id: str, request: Request) -> dict:
    _guard(request)
    try:
        return approval.undo(video_id)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


@review_app.get("/media/{video_id}")
def media(video_id: str, request: Request):
    _guard(request)
    path = approval.media_path(video_id)
    if path is None:
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="video/mp4")


# ---------- Channels ----------
@review_app.get("/api/channels")
def api_channels(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse({"channels": approval.channels_overview()})


def _channel_write(run) -> dict:
    """Every channel edit goes through channels.json: validated, written whole, reloaded. A bad value is a 400."""
    from app.growth.channels import ChannelsConfigError

    try:
        result = run()
    except ChannelsConfigError as exc:
        raise HTTPException(400, str(exc))
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(400, result["error"])
    return result if isinstance(result, dict) else {"ok": True}


@review_app.post("/api/channels")
def api_add_channel(body: NewChannel, request: Request) -> dict:
    _guard(request)
    from app.growth import channels

    return _channel_write(lambda: channels.add_channel(
        body.slug.strip(), name=body.name, niche=body.niche, brand_name=body.brand,
        feeds=tuple(f for f in body.feeds if f.strip()), uploads_per_day=body.uploads,
        timezone=body.timezone))


@review_app.post("/api/channels/{slug}/settings")
def api_channel_settings(slug: str, body: ChannelSettings, request: Request) -> dict:
    _guard(request)
    from app.growth import channels

    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "nothing to change")
    return _channel_write(lambda: channels.update_channel(slug, **fields))


@review_app.post("/api/channels/{slug}/enable")
def api_channel_enable(slug: str, request: Request) -> dict:
    _guard(request)
    from app.growth import channels

    return _channel_write(lambda: channels.set_enabled(slug, True))


@review_app.post("/api/channels/{slug}/disable")
def api_channel_disable(slug: str, request: Request) -> dict:
    _guard(request)
    from app.growth import channels

    return _channel_write(lambda: channels.set_enabled(slug, False))


@review_app.post("/api/channels/{slug}/feeds")
def api_channel_feeds(slug: str, body: FeedEdit, request: Request) -> dict:
    _guard(request)
    from app.growth import channels

    return _channel_write(lambda: channels.change_feeds(
        slug, add=tuple(body.add), remove=tuple(body.remove)))


@review_app.post("/api/channels/{slug}/pause")
def api_pause(slug: str, request: Request) -> dict:
    _guard(request)
    if not safety.pause(slug):
        raise HTTPException(404, "no such channel")
    return {"slug": slug, "paused": True}


@review_app.post("/api/channels/{slug}/resume")
def api_resume(slug: str, request: Request) -> dict:
    _guard(request)
    safety.resume(slug)  # already running is not an error: the page may be a tap behind
    return {"slug": slug, "paused": False}


# ---------- Status & report ----------
@review_app.get("/api/report")
def api_report(request: Request) -> JSONResponse:
    _guard(request)
    from app.growth.report import build_report

    return JSONResponse(build_report())


@review_app.get("/api/doctor")
def api_doctor(request: Request, live: bool = False) -> JSONResponse:
    _guard(request)
    from app.growth.report import doctor

    return JSONResponse(doctor(live=live))


# ---------- Controls ----------
@review_app.get("/api/controls")
def api_controls(request: Request) -> JSONResponse:
    _guard(request)
    with session_scope() as db:
        blocks = safety.block_list(db)
    actions = [{"action": a, "label": label, "about": about} for a, (label, about) in controls.ACTIONS.items()]
    return JSONResponse({"actions": actions, "status": controls.status(), "blocks": blocks})


@review_app.post("/api/controls/{action}")
def api_run_control(action: str, request: Request) -> dict:
    _guard(request)
    try:
        return controls.trigger(action)
    except KeyError:
        raise HTTPException(404, "no such action")


@review_app.post("/api/resume-all")
def api_resume_all(request: Request) -> dict:
    _guard(request)
    return {"ok": safety.resume(None)}


@review_app.post("/api/unblock")
def api_unblock(body: Unblock, request: Request) -> dict:
    _guard(request)
    lifted = safety.unblock(body.name)
    return {"name": body.name, "lifted": lifted}
