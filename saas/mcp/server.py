"""The remote MCP server at /mcp (streamable HTTP, stateless, JSON responses). mcp==2.3.0 SDK.

Auth: the same API keys as /api/v1 (`Authorization: Bearer dsk_live_...`). `BearerGate` (pure ASGI,
in front of the SDK's session manager) authenticates EVERY HTTP request: key live, user active, plan
includes MCP, per-key rate limit. It stores only the key id in the ASGI scope; each tool re-loads the
principal from that id in its own DB session (`_principal`), so nothing about identity is carried in
a context variable across requests. Stateless mode means no server-side MCP session can outlive a
revoked key.

Tools run their DB and yt-dlp work in a worker thread (anyio.to_thread) to keep the event loop free.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Callable, Literal, Optional

import anyio
from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ..auth import keys as keys_mod
from ..auth.errors import ApiError
from ..billing import catalog, ledger
from ..config import settings
from ..db import SessionLocal
from ..jobs import service, storage
from ..models.core import ApiKey, User

log = logging.getLogger("saas.mcp")

SCOPE_KEY = "saas.api_key_id"
MAX_BODY = 1024 * 1024

INSTRUCTIONS = (
    "Turns a long video (a YouTube/Vimeo/Twitch/etc. link the user has rights to) into finished vertical "
    "Shorts (1080x1920) with captions, framing, B-roll, sound effects and AI titles. Billing is in credits: "
    "1 credit = 1 minute of SOURCE video, rounded up per job. Typical flow: get_balance -> estimate_video(url) "
    "-> create_clips(url) -> poll get_job(job_id) every 30-60 s until status is done/failed/canceled -> "
    "get_clip_links(clip_id) for download URLs. Jobs usually take 2-15 minutes. Always tell the user the "
    "credit cost from estimate_video before calling create_clips."
)


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _principal(ctx: Context, *scopes: str):
    """(db, user, key) for this request, re-checked; the caller closes db."""
    request = ctx.request_context.request
    key_id = request.scope.get(SCOPE_KEY) if isinstance(request, Request) else None
    if not key_id:
        raise ToolError(_dump({"error": "unauthenticated", "message": "Missing API key."}))
    db = SessionLocal()
    key = db.get(ApiKey, key_id)
    user = db.get(User, key.user_id) if key is not None else None
    if key is None or key.revoked_at is not None or user is None or user.status != "active":
        db.close()
        raise ToolError(_dump({"error": "invalid_api_key", "message": "This API key is no longer valid."}))
    missing = [s for s in scopes if s not in (key.scopes or [])]
    if missing:
        db.close()
        raise ToolError(_dump({"error": "insufficient_scope", "message": f"This key lacks the '{missing[0]}' scope."}))
    return db, user, key


async def _call(ctx: Context, scopes: tuple[str, ...], fn: Callable[[Any, User, ApiKey], Any]) -> str:
    def work() -> str:
        db, user, key = _principal(ctx, *scopes)
        try:
            return _dump(fn(db, user, key))
        except ApiError as exc:
            db.rollback()
            raise ToolError(_dump({"error": exc.code, "message": exc.message,
                                   **{k: v for k, v in exc.extra.items() if k != "supported"}}))
        finally:
            db.close()

    return await anyio.to_thread.run_sync(work)


def _job_summary(db, job, *, clips: bool) -> dict:
    v = service.job_view(db, job, include_clips=clips, signed=clips)
    out = {k: v[k] for k in ("id", "status", "stage", "progress", "credits_reserved", "credits_charged",
                              "created_at", "finished_at", "error")}
    out["source"] = {"title": v["source"]["title"], "duration_s": v["source"]["duration_s"], "url": v["source"]["url"]}
    if clips:
        out["clips"] = [{k: c[k] for k in ("id", "title", "duration_s", "score", "hashtags", "download_url")}
                        for c in v.get("clips", [])]
    return out


def build_server() -> MCPServer:
    server = MCPServer(settings.brand_name.lower().replace(" ", "-") + "-clips", title=f"{settings.brand_name} Clips",
                       instructions=INSTRUCTIONS, version="1.0.0", website_url=settings.public_url)
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(description="Get the remaining credit balance (1 credit = 1 minute of source video) and the plan.",
                 annotations=read_only)
    async def get_balance(ctx: Context) -> str:
        def fn(db, user, _key):
            plan = catalog.plan_for_user(db, user)
            return {"balance": ledger.balance(db, user.id), "plan": plan.slug if plan else user.plan_slug}
        return await _call(ctx, ("billing:read",), fn)

    @server.tool(description=(
        "Check a video link before clipping it: returns title, duration_s and the credit cost, and ok=false with a "
        "reason (e.g. insufficient_credits, source_too_long, source_private) if it can't be clipped. Costs nothing. "
        "Supported: YouTube, Vimeo, Twitch, Loom, X/Twitter, TikTok, Instagram, Facebook, Google Drive, Dropbox, "
        "Rumble, Kick. One video per link (no playlists or live streams)."),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
    async def estimate_video(ctx: Context, url: Annotated[str, Field(description="Full https link to one video.",
                                                                     max_length=2000)]) -> str:
        def fn(db, user, _key):
            try:
                return service.estimate_url(db, user, url)
            except ApiError as exc:
                if exc.status == 400:
                    return {"ok": False, "reason": exc.code, "message": exc.message}
                raise
        return await _call(ctx, ("jobs:read",), fn)

    @server.tool(description=(
        "Start a clipping job for one video link. Reserves credits (ceil of source minutes) immediately; unused "
        "credits are returned if the job fails or is canceled. Returns the job (status 'queued'); poll get_job. "
        "Ask the user to confirm the cost from estimate_video first."),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def create_clips(
        ctx: Context,
        url: Annotated[str, Field(description="Full https link to one video the user has rights to.", max_length=2000)],
        clips: Annotated[int, Field(ge=1, le=20, description="How many Shorts to cut (plan limits apply).")] = 3,
        language: Annotated[Literal["auto", "en", "hi"], Field(description="Spoken language; auto-detect by default.")] = "auto",
        layout: Annotated[Literal["framed", "fill"], Field(description="framed: whole frame over a blurred backdrop; fill: crop to fill 9:16.")] = "framed",
        captions: Annotated[bool, Field(description="Burn in word-by-word captions.")] = True,
        broll: Annotated[bool, Field(description="Add stock B-roll cutaways.")] = True,
        memes: Annotated[bool, Field(description="Add reaction meme overlays on punchlines.")] = True,
        sfx: Annotated[bool, Field(description="Add sound effects on cuts and emphasis.")] = True,
        music: Annotated[bool, Field(description="Add a background music bed.")] = False,
        max_clip_seconds: Annotated[int, Field(ge=15, le=90, description="Longest clip length in seconds.")] = 60,
    ) -> str:
        options = {"clips": clips, "language": language, "layout": layout, "captions": captions, "broll": broll,
                   "memes": memes, "sfx": sfx, "music": music, "max_clip_seconds": max_clip_seconds}

        def fn(db, user, key):
            job = service.create_job(db, user, source_url=url, options=options, via="mcp", api_key_id=key.id)
            return _job_summary(db, job, clips=False)
        return await _call(ctx, ("jobs:write",), fn)

    @server.tool(description=(
        "Get one job's status (queued, fetching, transcribing, selecting, rendering, done, failed, canceled), "
        "progress 0-1, credits charged and, when done, its clips with signed download URLs (valid ~1 hour)."),
        annotations=read_only)
    async def get_job(ctx: Context, job_id: Annotated[str, Field(description="Job id (job_...).", max_length=40)]) -> str:
        def fn(db, user, _key):
            return _job_summary(db, service.get_job(db, user.id, job_id), clips=True)
        return await _call(ctx, ("jobs:read",), fn)

    @server.tool(description="List recent jobs, newest first. Optional status filter.", annotations=read_only)
    async def list_jobs(
        ctx: Context,
        status: Annotated[Optional[Literal["queued", "fetching", "transcribing", "selecting", "rendering", "done",
                                           "failed", "canceled"]], Field(description="Only jobs in this status.")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> str:
        def fn(db, user, _key):
            return {"jobs": [_job_summary(db, j, clips=False)
                             for j in service.list_jobs(db, user.id, status=status, limit=limit)]}
        return await _call(ctx, ("jobs:read",), fn)

    @server.tool(description="List finished clips, newest first, optionally for one job. Includes titles, "
                             "descriptions, hashtags and signed download URLs.", annotations=read_only)
    async def list_clips(
        ctx: Context,
        job_id: Annotated[Optional[str], Field(description="Only clips from this job.", max_length=40)] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> str:
        def fn(db, user, _key):
            rows = service.list_clips(db, user.id, job_id=job_id, limit=limit)
            return {"clips": [{k: v for k, v in service.clip_view(c, signed=True).items()
                               if k in ("id", "job_id", "title", "description", "hashtags", "duration_s", "score",
                                        "download_url", "thumbnail_url")} for c in rows]}
        return await _call(ctx, ("clips:read",), fn)

    @server.tool(description="Fresh short-lived signed URLs (video for streaming, download as an attachment, "
                             "thumbnail) for one clip. No API key is needed to open them; they expire after ~1 hour.",
                 annotations=read_only)
    async def get_clip_links(ctx: Context,
                             clip_id: Annotated[str, Field(description="Clip id (clp_...).", max_length=40)]) -> str:
        def fn(db, user, _key):
            clip = service.get_clip(db, user.id, clip_id)
            video, exp = storage.signed_file_url(clip.id, "video")
            download, _ = storage.signed_file_url(clip.id, "download")
            thumb, _ = storage.signed_file_url(clip.id, "thumbnail")
            return {"clip_id": clip.id, "title": clip.title, "video_url": video, "download_url": download,
                    "thumbnail_url": thumb, "expires_at": exp}
        return await _call(ctx, ("clips:read",), fn)

    @server.tool(description="Cancel a queued or running job. Reserved credits are returned in full. Finished jobs "
                             "are unaffected.",
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True,
                                             openWorldHint=False))
    async def cancel_job(ctx: Context, job_id: Annotated[str, Field(description="Job id (job_...).", max_length=40)]) -> str:
        def fn(db, user, _key):
            return _job_summary(db, service.cancel_job(db, user.id, job_id), clips=False)
        return await _call(ctx, ("jobs:write",), fn)

    return server


class BearerGate:
    """ASGI app at /mcp: authenticate the Bearer key on every request, then hand off to the SDK."""

    def __init__(self, manager: StreamableHTTPSessionManager):
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        raw = keys_mod.bearer_token(request.headers.get("authorization"))

        def authenticate() -> Optional[str]:
            db = SessionLocal()
            try:
                principal = keys_mod.authenticate(db, raw, request, ())
                feats = catalog.features_for(db, principal.user)
                if not feats.get("mcp"):
                    raise ApiError(403, "plan_required", "MCP access is part of the Studio plan.")
                return principal.key.id
            finally:
                db.close()

        try:
            key_id = await anyio.to_thread.run_sync(authenticate)
        except ApiError as exc:
            if exc.status == 401:
                exc.headers = {**(exc.headers or {}), "WWW-Authenticate": 'Bearer realm="mcp"'}
            await exc.response()(scope, receive, send)
            return
        except Exception as exc:  # rate limiter's HTTPException and anything unexpected
            status = getattr(exc, "status_code", 500)
            detail = getattr(exc, "detail", None)
            body = detail if isinstance(detail, dict) and "error" in detail else {
                "error": "internal_error", "message": "Request failed."}
            if status == 500:
                log.exception("mcp auth failed")
            from fastapi.responses import JSONResponse

            await JSONResponse(body, status_code=status, headers=getattr(exc, "headers", None))(scope, receive, send)
            return
        scope[SCOPE_KEY] = key_id
        await self.manager.handle_request(scope, receive, send)


def mount(app: FastAPI) -> None:
    """Add /mcp to `app` and run the SDK's session manager inside the app's lifespan.

    Built per app (the session manager's run() is once-per-instance and tests create many apps).
    """
    if not settings.mcp_enabled:
        return
    server = build_server()
    # The SDK's DNS-rebinding guard protects unauthenticated local servers. Ours requires a Bearer key a
    # browser never attaches on its own, and it runs behind our own host/proxy config, so it is off.
    manager = StreamableHTTPSessionManager(app=server._lowlevel_server, json_response=True, stateless=True,
                                           security_settings=TransportSecuritySettings(
                                               enable_dns_rebinding_protection=False),
                                           max_request_body_size=MAX_BODY)
    app.router.routes.append(Route("/mcp", endpoint=BearerGate(manager), methods=["GET", "POST", "DELETE"]))
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        async with original(a) as state:
            async with manager.run():
                yield state

    app.router.lifespan_context = lifespan
