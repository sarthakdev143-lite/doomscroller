"""The phone control panel: review clips, run the channels, and see how the loop is doing.

It runs as its own small server (scripts/run.py starts it on port 8600) so that only these routes are
reachable when a tunnel exposes it, never the main API. Every route needs the REVIEW_TOKEN, given once as
?key= and then kept in a cookie. The page itself lives in app/review_ui.py.
"""
from __future__ import annotations

import hmac
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app import logging_config, scheduler
from app.config import settings
from app.db import init_db, session_scope
from app.growth import approval, controls, safety
from app.review_ui import LOCKED, MANIFEST, PAGE, UNCONFIGURED


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    yield


review_app = FastAPI(
    title="Doomscroller control panel",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)
_COOKIE = "review_key"


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
    description: str | None = None
    tags: list[str] | None = None


class MetadataUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class RejectFlaggedRequest(BaseModel):
    channel_slug: str = ""
    video_ids: list[str] | None = None


class ApproveCleanRequest(BaseModel):
    channel_slug: str = ""
    min_score: float | None = None
    video_ids: list[str] | None = None


class PinRequest(BaseModel):
    pinned: bool = True


class CloneChannelRequest(BaseModel):
    new_slug: str = ""
    name: str = ""


class ImportChannelsRequest(BaseModel):
    payload: dict | None = None
    dry_run: bool = True


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
    long_uploads_per_day: int | None = None
    long_auto_publish: bool | None = None
    timezone: str | None = None
    require_approval: bool | None = None
    auto_approve_score: float | None = None
    cadence_backstop: bool | None = None


class FeedEdit(BaseModel):
    add: list[str] = []
    remove: list[str] = []


class Unblock(BaseModel):
    name: str


class ClipRequest(BaseModel):
    source: str
    count: int = 3
    auto_queue: bool | None = None
    channel_slug: str = ""


class LongBuildRequest(BaseModel):
    channel_slug: str = ""
    format: str = "best_of"
    target: str = ""
    targets: list[str] = []
    theme: str = ""
    target_minutes: int | None = None
    auto_publish: bool | None = None


class LongThumbSelect(BaseModel):
    variant_id: str = ""


class LongApproveRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    variant_id: str | None = None


class FailureCleanupRequest(BaseModel):
    video_id: str = ""
    missing_only: bool = True
    action: str = "archive"


@review_app.get("/", response_class=HTMLResponse)
def page(request: Request, key: str = ""):
    if key and settings.review_token and hmac.compare_digest(key.encode(), settings.review_token.encode()):
        response = RedirectResponse("/", status_code=303)
        secure = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
        response.set_cookie(_COOKIE, key, max_age=90 * 24 * 3600, httponly=True, samesite="lax", secure=secure)
        return response
    # Never let a phone cache the page: it is redeployed on every update, and a stale copy from the last
    # deploy is exactly what hides new tabs and fixes until the browser is cleared by hand.
    no_store = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}
    if not settings.review_token:
        return HTMLResponse(UNCONFIGURED, status_code=503, headers=no_store)
    if not _allowed(request):
        return HTMLResponse(LOCKED, status_code=401, headers=no_store)
    return HTMLResponse(PAGE, headers=no_store)


@review_app.get("/manifest.webmanifest")
def manifest(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@review_app.post("/api/logout")
def api_logout(request: Request) -> JSONResponse:
    _guard(request)
    response = JSONResponse({"ok": True})
    response.delete_cookie(_COOKIE)
    return response


# ---------- Review ----------
@review_app.get("/api/queue")
def api_queue(request: Request, channel: str = "") -> JSONResponse:
    _guard(request)
    return JSONResponse(approval.queue(channel_slug=channel))


@review_app.post("/api/clips/reject-flagged")
def api_reject_flagged(request: Request, body: RejectFlaggedRequest | None = None) -> dict:
    _guard(request)
    try:
        return approval.reject_flagged(
            body.channel_slug if body else "",
            video_ids=body.video_ids if body else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/clips/approve-clean")
def api_approve_clean(request: Request, body: ApproveCleanRequest | None = None) -> dict:
    _guard(request)
    try:
        with session_scope() as db:
            return approval.approve_clean(
                db,
                channel_slug=body.channel_slug if body else "",
                min_score=body.min_score if body else None,
                video_ids=body.video_ids if body else None,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/clips/{video_id}/approve")
def api_approve(video_id: str, decision: Decision, request: Request) -> dict:
    _guard(request)
    try:
        return approval.approve(
            video_id,
            decision.title,
            description=decision.description,
            tags=decision.tags,
        )
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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


@review_app.post("/api/clips/{video_id}/title")
def api_update_title(video_id: str, decision: Decision, request: Request) -> dict:
    _guard(request)
    try:
        return approval.update_title(video_id, decision.title)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/clips/{video_id}/metadata")
def api_update_metadata(video_id: str, body: MetadataUpdate, request: Request) -> dict:
    _guard(request)
    try:
        return approval.update_metadata(
            video_id,
            title=body.title,
            description=body.description,
            tags=body.tags,
        )
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/clips/{video_id}/shelf-reject")
def api_shelf_reject(video_id: str, request: Request) -> dict:
    _guard(request)
    try:
        return approval.reject_from_shelf(video_id)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


@review_app.post("/api/clips/{video_id}/pin")
def api_pin_clip(video_id: str, request: Request, body: PinRequest | None = None) -> dict:
    _guard(request)
    try:
        return approval.pin_clip_next(video_id, pinned=body.pinned if body else True)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


@review_app.post("/api/scraped/{video_id}/approve")
def api_approve_scraped(video_id: str, decision: Decision, request: Request) -> dict:
    _guard(request)
    try:
        return approval.curate_scraped(
            video_id,
            approved=True,
            title=decision.title,
            description=decision.description,
            tags=decision.tags,
        )
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/scraped/{video_id}/reject")
def api_reject_scraped(video_id: str, request: Request) -> dict:
    _guard(request)
    try:
        return approval.curate_scraped(video_id, approved=False)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


def _serve_media(video_id: str, download: str = "") -> FileResponse:
    path = approval.media_path(video_id)
    if path is None:
        raise HTTPException(404, "no such clip")
    if str(download or "").strip().lower() in ("1", "true", "yes", "on"):
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(video_id or "")).strip("-")[:64] or "clip"
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="short-{safe_id}.mp4"'},
        )
    return FileResponse(path, media_type="video/mp4")


@review_app.get("/media/{video_id}")
def media(video_id: str, request: Request, download: str = ""):
    _guard(request)
    return _serve_media(video_id, download)


@review_app.get("/api/media/{video_id}")
def api_media(video_id: str, request: Request, download: str = ""):
    _guard(request)
    return _serve_media(video_id, download)


# ---------- Channels ----------
@review_app.get("/api/channels")
def api_channels(request: Request) -> JSONResponse:
    _guard(request)
    from app.growth import channels

    rows = approval.channels_overview()
    modes = channels.channel_modes()
    for r in rows:
        mode = modes.get(r.get("slug", ""), "")
        r.setdefault("mode", mode)
        r.setdefault("procedural", mode == "procedural_ambient")
    return JSONResponse({"channels": rows})


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


@review_app.post("/api/channels/import")
def api_import_channels(body: ImportChannelsRequest, request: Request) -> dict:
    _guard(request)
    from app.growth import channels

    payload = body.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("channels"), list):
        raise HTTPException(400, "paste a channels export or backup JSON with a 'channels' list")
    # Accept a /api/backup file as-is: its status extras are dropped, the legacy default row is skipped, and an
    # import never switches a channel on (credentials and sign-in belong to this machine, not the file).
    keep = ("slug", "name", "niche", "brand_name", "uploads_per_day", "long_uploads_per_day", "long_auto_publish",
            "timezone", "require_approval", "auto_approve_score", "cadence_backstop", "feeds", "meta")
    rows = []
    for raw in payload["channels"]:
        if not isinstance(raw, dict):
            raise HTTPException(400, "every channel in the file must be a JSON object")
        if raw.get("default") is True or str(raw.get("slug") or "").strip() == channels.DEFAULT_SLUG:
            continue
        rows.append({k: raw[k] for k in keep if k in raw})
    return _channel_write(lambda: channels.import_channels_snapshot(
        None, {"version": payload.get("version", 1), "channels": rows}, dry_run=body.dry_run))


@review_app.post("/api/channels/{slug}/clone")
def api_clone_channel(slug: str, body: CloneChannelRequest, request: Request) -> dict:
    _guard(request)
    from app.growth import channels

    new_slug = body.new_slug.strip()
    if not new_slug:
        raise HTTPException(400, "give the clone a short name")
    return _channel_write(lambda: {
        "ok": True,
        "slug": channels.clone_channel(None, slug, new_slug, name=body.name.strip()).slug,
    })


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


@review_app.get("/api/failures")
def api_failures(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(approval.list_failed_videos())


@review_app.post("/api/failures/cleanup")
def api_failures_cleanup(request: Request, body: FailureCleanupRequest | None = None) -> dict:
    _guard(request)
    req = body or FailureCleanupRequest()
    try:
        return approval.cleanup_failed_videos(
            video_id=req.video_id,
            missing_only=req.missing_only,
            action=req.action,
        )
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _read_tunnel_url() -> str:
    from app.growth import notify

    try:
        if notify.TUNNEL_URL_FILE.is_file():
            return notify.TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


# ---------- Controls ----------
@review_app.get("/api/controls")
def api_controls(request: Request) -> JSONResponse:
    _guard(request)
    with session_scope() as db:
        blocks = safety.block_list(db)
    actions = [{"action": a, "label": label, "about": about} for a, (label, about) in controls.ACTIONS.items()]
    return JSONResponse({
        "actions": actions,
        "status": controls.status(),
        "blocks": blocks,
        "storage": scheduler.storage_report(),
        "tunnel_url": _read_tunnel_url(),
        "scheduler": {
            "running": bool(scheduler._scheduler and scheduler._scheduler.running),
            "overdue": scheduler.overdue_jobs(),
            "health": logging_config.job_health(limit=200),
            "recent": list(reversed(logging_config.read_job_history(limit=12))),
        },
    })


@review_app.get("/api/controls/preview")
def api_controls_preview(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(controls.preview_maintenance())


_WARN_LOG_RE = re.compile(r"\[(?:WARNING|ERROR|CRITICAL)\]|\b(?:WARNING|ERROR|CRITICAL)\b")


@review_app.get("/api/logs")
def api_logs(request: Request, limit: int = 80, level: str = "all") -> JSONResponse:
    _guard(request)
    try:
        clamped = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        raise HTTPException(400, "invalid limit")
    mode = (level or "all").strip().lower()
    if mode not in ("all", "warn", "warning", "warnings", "error", "errors"):
        raise HTTPException(400, "level must be 'all' or 'warn'")
    norm_level = "all" if mode == "all" else "warn"
    lines = [logging_config.scrub_secrets(ln) for ln in logging_config.read_recent_logs(limit=clamped)]
    if norm_level == "warn":
        lines = [ln for ln in lines if _WARN_LOG_RE.search(ln)]
    return JSONResponse(
        {"lines": lines, "count": len(lines), "level": norm_level},
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


_NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@review_app.get("/api/system/doctor")
def api_system_doctor(request: Request) -> JSONResponse:
    _guard(request)
    from scripts import doctor as sys_doctor

    return JSONResponse(sys_doctor.run_doctor(fix=False), headers=_NO_STORE)


@review_app.post("/api/system/doctor/fix")
def api_system_doctor_fix(request: Request) -> JSONResponse:
    _guard(request)
    from scripts import doctor as sys_doctor

    return JSONResponse(sys_doctor.run_doctor(fix=True), headers=_NO_STORE)


@review_app.get("/api/backup")
def api_backup(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(
        approval.export_snapshot(),
        headers={
            "Content-Disposition": 'attachment; filename="doomscroller-backup.json"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@review_app.post("/api/notify/test")
def api_notify_test(request: Request) -> JSONResponse:
    _guard(request)
    from app.growth import notify

    return JSONResponse(notify.send_test_notification())


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


# ---------- Clipper ----------
@review_app.post("/api/clip")
def api_queue_clip(body: ClipRequest, request: Request) -> dict:
    _guard(request)
    from app.clipper import job as clip_job

    try:
        job_id = clip_job.enqueue_from_panel(
            body.source,
            count=body.count,
            auto_queue=body.auto_queue,
            channel_slug=body.channel_slug,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    clip_job.start_queue_worker()
    return {"job_id": job_id, "status": "queued"}


@review_app.post("/api/clip/upload")
async def api_upload_clip(
    request: Request,
    filename: str = "",
    count: int = 3,
    auto_queue: bool | None = None,
    channel_slug: str = "",
) -> dict:
    _guard(request)
    from app.clipper import job as clip_job

    try:
        part_path, final_path = clip_job.prepare_inbox_upload(filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    written = 0
    try:
        with open(part_path, "wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > clip_job.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "video exceeds upload size limit")
                handle.write(chunk)
        if written == 0:
            raise HTTPException(400, "uploaded file is empty")
        part_path.replace(final_path)
        job_id = clip_job.enqueue_from_panel(
            str(final_path),
            count=count,
            auto_queue=auto_queue,
            channel_slug=channel_slug,
        )
    except HTTPException:
        part_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise
    except ValueError as exc:
        part_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    except Exception:
        part_path.unlink(missing_ok=True)
        raise
    clip_job.start_queue_worker()
    return {"job_id": job_id, "status": "queued", "bytes": written, "filename": final_path.name}


@review_app.get("/api/clip/jobs")
def api_clip_jobs(request: Request, limit: int = 15) -> JSONResponse:
    _guard(request)
    from app.clipper import job as clip_job

    return JSONResponse({"jobs": clip_job.list_jobs(limit=max(1, min(50, int(limit))))})


@review_app.post("/api/clip/clear-finished")
def api_clear_finished_clip_jobs(request: Request) -> dict:
    _guard(request)
    from app.clipper import job as clip_job

    return clip_job.clear_finished_jobs()


@review_app.post("/api/clip/jobs/{job_id}/retry")
def api_retry_clip_job(job_id: str, request: Request) -> dict:
    _guard(request)
    from app.clipper import job as clip_job

    try:
        updated = clip_job.retry_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    clip_job.start_queue_worker()
    return updated


@review_app.post("/api/clip/jobs/{job_id}/dismiss")
def api_dismiss_clip_job(job_id: str, request: Request) -> dict:
    _guard(request)
    from app.clipper import job as clip_job

    try:
        return clip_job.dismiss_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# ---------- Long-Form 16:9 ----------
_LONG_THUMB_UPLOAD_LIMIT = 10 * 1024 * 1024
_LONG_TRACK_UPLOAD_LIMIT = 30 * 1024 * 1024


async def _read_limited_body(request: Request, limit_bytes: int, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit_bytes:
            raise HTTPException(413, f"{label} exceeds {limit_bytes // (1024 * 1024)} MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


@review_app.get("/api/long/videos")
def api_long_videos(request: Request) -> JSONResponse:
    _guard(request)
    from app.longform import job as long_job

    return JSONResponse(long_job.long_panel_data())


@review_app.post("/api/long/build")
def api_long_build(body: LongBuildRequest, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    try:
        job_id = long_job.enqueue_long_job(
            body.channel_slug,
            format_kind=body.format,
            target=body.target,
            targets=body.targets,
            theme=body.theme,
            target_minutes=body.target_minutes,
            auto_publish=body.auto_publish,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    long_job.start_long_worker()
    return {"job_id": job_id, "status": "queued"}


@review_app.get("/api/long/jobs")
def api_long_jobs(request: Request, limit: int = 20) -> JSONResponse:
    _guard(request)
    from app.longform import job as long_job

    return JSONResponse({"jobs": long_job.list_long_jobs(limit=max(1, min(50, int(limit))))})


@review_app.post("/api/long/clear-finished")
def api_clear_finished_long_jobs(request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    return long_job.clear_finished_long_jobs()


@review_app.post("/api/long/jobs/{job_id}/retry")
def api_retry_long_job(job_id: str, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    try:
        updated = long_job.retry_long_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    long_job.start_long_worker()
    return updated


@review_app.post("/api/long/jobs/{job_id}/dismiss")
def api_dismiss_long_job(job_id: str, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    try:
        return long_job.dismiss_long_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@review_app.get("/api/long/{video_id}/hero")
def api_long_hero(video_id: str, request: Request):
    _guard(request)
    from app.longform import job as long_job

    path = long_job.hero_frame_path(video_id)
    if path is None:
        raise HTTPException(404, "no hero frame")
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(video_id or "")).strip("-")[:64] or "long"
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="hero-{safe_id}.jpg"'},
    )


@review_app.get("/api/long/{video_id}/thumb/{variant_id}")
def api_long_thumb(video_id: str, variant_id: str, request: Request):
    _guard(request)
    from app.longform import job as long_job

    path = long_job.thumbnail_path(video_id, variant_id)
    if path is None:
        raise HTTPException(404, "no such thumbnail")
    return FileResponse(path, media_type="image/jpeg", headers=_NO_STORE)


@review_app.post("/api/long/{video_id}/thumbnail")
def api_long_select_thumb(video_id: str, body: LongThumbSelect, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    try:
        return long_job.select_thumbnail(video_id, body.variant_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/long/{video_id}/thumbnail/upload")
async def api_long_upload_thumb(video_id: str, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    raw = await _read_limited_body(request, _LONG_THUMB_UPLOAD_LIMIT, "thumbnail")
    try:
        return long_job.upload_custom_thumbnail(video_id, raw)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/long/{video_id}/portrait")
async def api_long_upload_portrait(video_id: str, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    raw = await _read_limited_body(request, _LONG_THUMB_UPLOAD_LIMIT, "portrait")
    try:
        return long_job.re_render_ambient_with_portrait(video_id, raw)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/long/ambient/tracks")
async def api_long_upload_ambient_track(request: Request, filename: str = "", preset: str = "") -> dict:
    _guard(request)
    from app.longform import job as long_job

    raw = await _read_limited_body(request, _LONG_TRACK_UPLOAD_LIMIT, "audio track")
    try:
        return long_job.upload_ambient_track(filename, raw, preset=preset)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/long/{video_id}/metadata")
def api_long_metadata(video_id: str, body: MetadataUpdate, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    try:
        return long_job.update_long_metadata(
            video_id,
            title=body.title,
            description=body.description,
            tags=body.tags,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@review_app.post("/api/long/{video_id}/approve")
def api_long_approve(video_id: str, request: Request, body: LongApproveRequest | None = None) -> dict:
    _guard(request)
    from app.longform import job as long_job

    try:
        return long_job.approve_long(
            video_id,
            title=body.title if body else None,
            description=body.description if body else None,
            tags=body.tags if body else None,
            variant_id=body.variant_id if body else None,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@review_app.post("/api/long/{video_id}/unapprove")
def api_long_unapprove(video_id: str, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    try:
        return long_job.unapprove_long(video_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@review_app.post("/api/long/{video_id}/reject")
def api_long_reject(video_id: str, request: Request) -> dict:
    _guard(request)
    from app.longform import job as long_job

    try:
        return long_job.reject_long(video_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))

