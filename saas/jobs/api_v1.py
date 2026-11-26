"""Public API v1: Bearer API keys (`Authorization: Bearer dsk_live_...`), scoped, rate-limited per key.

Mirrors the web API for jobs, clips and balance. File links are short-lived signed URLs, so scripts
and AI clients can download without holding a session or sending the key to a CDN.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import compat
from ..auth.deps import ApiPrincipal, require_api_key
from ..auth.errors import ApiRoute
from ..billing import catalog, ledger
from ..config import settings
from ..db import get_db
from . import service, storage

router = APIRouter(prefix="/api/v1", route_class=ApiRoute)


class EstimateIn(BaseModel):
    url: str = Field(max_length=2000)


class JobIn(BaseModel):
    source_url: str = Field(max_length=2000)
    options: Optional[dict] = None


class ClipPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)
    hashtags: Optional[list[str]] = Field(default=None, max_length=30)


@router.get("/balance")
def balance(p: ApiPrincipal = Depends(require_api_key("billing:read")), db: Session = Depends(get_db)):
    plan = catalog.plan_for_user(db, p.user)
    return {"balance": ledger.balance(db, p.user_id), "plan": plan.slug if plan else p.user.plan_slug,
            "unit": "1 credit = 1 minute of source video"}


@router.post("/estimate")
def estimate(body: EstimateIn, request: Request, p: ApiPrincipal = Depends(require_api_key("jobs:read")),
             db: Session = Depends(get_db)):
    compat.rate_limit(request, "estimate", settings.api_estimate_per_hour, 3600, key=f"user:{p.user_id}")
    return service.estimate_url(db, p.user, body.url)


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, request: Request, p: ApiPrincipal = Depends(require_api_key("jobs:write")),
               db: Session = Depends(get_db)):
    compat.rate_limit(request, "job_create", 30, 3600, key=f"user:{p.user_id}")
    job = service.create_job(db, p.user, source_url=body.source_url, options=body.options, via="api",
                             api_key_id=p.key.id)
    return service.job_view(db, job, signed=True)


@router.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 20, before: Optional[str] = None,
              p: ApiPrincipal = Depends(require_api_key("jobs:read")), db: Session = Depends(get_db)):
    rows = service.list_jobs(db, p.user_id, status=status, limit=limit, before=before)
    return {"jobs": [service.job_view(db, j, include_clips=False) for j in rows],
            "next": rows[-1].id if len(rows) == max(1, min(limit, 100)) else None}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, p: ApiPrincipal = Depends(require_api_key("jobs:read")), db: Session = Depends(get_db)):
    return service.job_view(db, service.get_job(db, p.user_id, job_id), signed=True)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, p: ApiPrincipal = Depends(require_api_key("jobs:write")), db: Session = Depends(get_db)):
    return service.job_view(db, service.cancel_job(db, p.user_id, job_id), signed=True)


@router.get("/clips")
def list_clips(job_id: Optional[str] = None, limit: int = 50, before: Optional[str] = None,
               p: ApiPrincipal = Depends(require_api_key("clips:read")), db: Session = Depends(get_db)):
    rows = service.list_clips(db, p.user_id, job_id=job_id, limit=limit, before=before)
    return {"clips": [service.clip_view(c, signed=True) for c in rows],
            "next": rows[-1].id if len(rows) == max(1, min(limit, 200)) else None}


@router.get("/clips/{clip_id}")
def get_clip(clip_id: str, p: ApiPrincipal = Depends(require_api_key("clips:read")), db: Session = Depends(get_db)):
    return service.clip_view(service.get_clip(db, p.user_id, clip_id), signed=True)


@router.get("/clips/{clip_id}/links")
def clip_links(clip_id: str, p: ApiPrincipal = Depends(require_api_key("clips:read")), db: Session = Depends(get_db)):
    clip = service.get_clip(db, p.user_id, clip_id)
    video, exp = storage.signed_file_url(clip.id, "video")
    download, _ = storage.signed_file_url(clip.id, "download")
    thumb, _ = storage.signed_file_url(clip.id, "thumbnail")
    return {"clip_id": clip.id, "video_url": video, "download_url": download, "thumbnail_url": thumb,
            "expires_at": exp}


@router.get("/clips/{clip_id}/package")
def clip_package(clip_id: str, tz: Optional[str] = None, p: ApiPrincipal = Depends(require_api_key("clips:read")),
                 db: Session = Depends(get_db)):
    from . import package

    return package.build(service.get_clip(db, p.user_id, clip_id), tz or p.user.timezone, signed=True)


@router.patch("/clips/{clip_id}")
def patch_clip(clip_id: str, body: ClipPatch, p: ApiPrincipal = Depends(require_api_key("clips:write")),
               db: Session = Depends(get_db)):
    clip = service.update_clip(db, p.user_id, clip_id, title=body.title, description=body.description,
                               hashtags=body.hashtags)
    return service.clip_view(clip, signed=True)


@router.delete("/clips/{clip_id}")
def delete_clip(clip_id: str, p: ApiPrincipal = Depends(require_api_key("clips:write")), db: Session = Depends(get_db)):
    service.delete_clip(db, p.user_id, clip_id)
    return {"ok": True}
