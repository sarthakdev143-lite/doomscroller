"""Jobs, uploads and clips for signed-in users (cookie session), plus signed file links.

The public API (/api/v1, Bearer keys) is in api_v1.py and included here. Every lookup is scoped to the
caller's user id in saas.jobs.service, so another account's ids answer 404.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import compat
from ..auth.deps import require_user, require_verified_user
from ..auth.errors import ApiError, ApiRoute
from ..config import settings
from ..db import get_db
from ..models.core import User
from ..models.jobs import Clip, Upload
from . import package, service, storage, uploads
from .api_v1 import router as v1_router
from .brand import router as brand_router

router = APIRouter(route_class=ApiRoute)
router.include_router(v1_router)
router.include_router(brand_router)


class EstimateIn(BaseModel):
    url: str = Field(max_length=2000)


class JobIn(BaseModel):
    source_url: Optional[str] = Field(default=None, max_length=2000)
    upload_id: Optional[str] = Field(default=None, max_length=40)
    options: Optional[dict] = None


class ClipPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)
    hashtags: Optional[list[str]] = Field(default=None, max_length=30)


def _download_name(clip: Clip) -> str:
    base = re.sub(r"[^A-Za-z0-9 _-]+", "", clip.title or "").strip().replace(" ", "_")[:60] or "clip"
    return f"{base}_{clip.idx + 1}.mp4"


async def serve_package_zip(clip: Clip, tz: Optional[str]) -> FileResponse:
    from fastapi.concurrency import run_in_threadpool
    from starlette.background import BackgroundTask

    try:
        path = await run_in_threadpool(package.build_zip, clip, tz)
    except FileNotFoundError:
        raise ApiError(404, "not_found", "File not found.")
    return FileResponse(path, media_type="application/zip", filename=package.zip_name(clip),
                        headers={"Cache-Control": "private, no-store"},
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


def serve_clip_file(clip: Clip, kind: str) -> FileResponse:
    path = service.clip_file(clip, kind)
    if kind == "thumbnail":
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
    headers = {"Cache-Control": "private, max-age=3600"}
    if kind == "download":
        return FileResponse(path, media_type="video/mp4", filename=_download_name(clip), headers=headers)
    return FileResponse(path, media_type="video/mp4", headers=headers)  # Starlette answers Range with 206


# ---- estimate / uploads / jobs --------------------------------------------------------------------

@router.post("/api/jobs/estimate")
def estimate(body: EstimateIn, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    compat.rate_limit(request, "estimate", settings.api_estimate_per_hour, 3600, key=f"user:{user.id}")
    try:
        return service.estimate_url(db, user, body.url)
    except ApiError as exc:
        if exc.status == 400:
            return {"ok": False, "reason": exc.code, "message": exc.message, "title": None, "duration_s": None,
                    "thumbnail": None, "credits": None}
        raise


@router.post("/api/uploads", status_code=201)
async def upload(request: Request, user: User = Depends(require_verified_user), db: Session = Depends(get_db)):
    compat.rate_limit(request, "upload", 20, 3600, key=f"user:{user.id}")
    row = await uploads.receive(request, db, user)
    return uploads.public(row)


@router.delete("/api/uploads/{upload_id}")
def delete_upload(upload_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ..common import utcnow

    up = db.query(Upload).filter(Upload.id == upload_id, Upload.user_id == user.id, Upload.deleted_at.is_(None)).one_or_none()
    if up is None:
        raise ApiError(404, "not_found", "Upload not found.")
    if up.job_id is None:
        storage.remove_file(up.path)
    up.deleted_at = utcnow()
    db.commit()
    return {"ok": True}


@router.post("/api/jobs", status_code=201)
def create_job(body: JobIn, request: Request, user: User = Depends(require_verified_user),
               db: Session = Depends(get_db)):
    compat.rate_limit(request, "job_create", 30, 3600, key=f"user:{user.id}")
    job = service.create_job(db, user, source_url=body.source_url, upload_id=body.upload_id, options=body.options,
                             via="web")
    return service.job_view(db, job)


@router.get("/api/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 20, before: Optional[str] = None,
              user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = service.list_jobs(db, user.id, status=status, limit=limit, before=before)
    return {"jobs": [service.job_view(db, j, include_clips=False) for j in rows],
            "next": rows[-1].id if len(rows) == max(1, min(limit, 100)) else None}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return service.job_view(db, service.get_job(db, user.id, job_id))


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return service.job_view(db, service.cancel_job(db, user.id, job_id))


# ---- clips ----------------------------------------------------------------------------------------

@router.get("/api/clips")
def list_clips(job_id: Optional[str] = None, limit: int = 50, before: Optional[str] = None,
               user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = service.list_clips(db, user.id, job_id=job_id, limit=limit, before=before)
    return {"clips": [service.clip_view(c) for c in rows],
            "next": rows[-1].id if len(rows) == max(1, min(limit, 200)) else None}


@router.get("/api/clips/{clip_id}")
def get_clip(clip_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return service.clip_view(service.get_clip(db, user.id, clip_id))


@router.patch("/api/clips/{clip_id}")
def patch_clip(clip_id: str, body: ClipPatch, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = service.update_clip(db, user.id, clip_id, title=body.title, description=body.description,
                               hashtags=body.hashtags)
    return service.clip_view(clip)


@router.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    service.delete_clip(db, user.id, clip_id)
    return {"ok": True}


@router.get("/api/clips/{clip_id}/video")
def clip_video(clip_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return serve_clip_file(service.get_clip(db, user.id, clip_id), "video")


@router.get("/api/clips/{clip_id}/download")
def clip_download(clip_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return serve_clip_file(service.get_clip(db, user.id, clip_id), "download")


@router.get("/api/clips/{clip_id}/thumbnail")
def clip_thumbnail(clip_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return serve_clip_file(service.get_clip(db, user.id, clip_id), "thumbnail")


@router.get("/api/clips/{clip_id}/package")
def clip_package(clip_id: str, tz: Optional[str] = None, user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    return package.build(service.get_clip(db, user.id, clip_id), tz or user.timezone)


@router.get("/api/clips/{clip_id}/package.zip")
async def clip_package_zip(clip_id: str, tz: Optional[str] = None, user: User = Depends(require_user),
                           db: Session = Depends(get_db)):
    clip = service.get_clip(db, user.id, clip_id)
    return await serve_package_zip(clip, tz or user.timezone)


# ---- signed links (API / MCP consumers, no cookie or key needed) -----------------------------------

@router.get("/files/clips/{clip_id}/{kind}")
async def signed_file(clip_id: str, kind: str, exp: int = 0, sig: str = "", db: Session = Depends(get_db)):
    if not storage.verify_file_signature(clip_id, kind, exp, sig):
        raise ApiError(403, "invalid_link", "This link is invalid or has expired. Ask for a fresh one.")
    clip = db.query(Clip).filter(Clip.id == clip_id, Clip.deleted_at.is_(None)).one_or_none()
    if clip is None:
        raise ApiError(404, "not_found", "Clip not found.")
    if kind == "package":
        owner = db.get(User, clip.user_id)
        return await serve_package_zip(clip, owner.timezone if owner else None)
    return serve_clip_file(clip, kind)
