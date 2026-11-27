"""The clip editor API: read what a clip was made of, queue a re-render with an edit spec, list versions.

Clips are burned-in (captions, memes, sound are in the pixels and the mix), so an edit is a server-side
re-render (app.clipper.standalone.rerender_clip) from the bundle the engine keeps beside each clip:
`<clip>_manifest.json`, `<clip>_source.mp4` and `<clip>_assets/`. The spec format is the pipeline's
(app/clipper/editor.py `_SPEC_SHAPE`, documented in CONTRACT.md); here it is size- and shape-checked and
the engine validates it fully (a spec it can't apply fails the job with code `invalid_spec`, credits back).

* `GET /api/clips/{id}/edit` -> {clip_id, version, duration_s, spec, words, memes {in_clip, available},
  cutaways, beats, hook, logo {available, url}, limits}.
* `POST /api/clips/{id}/render {spec}` -> 202 the queued re-render job (kind "rerender"). The first
  SAAS_RERENDER_FREE_PER_CLIP re-renders of a clip are included; after that each reserves
  SAAS_RERENDER_CREDIT_COST credits (released if it fails). One re-render at a time per clip.
* `GET /api/clips/{id}/versions` and `GET /api/clips/{id}/versions/{n}/video`. The newest version is the
  clip's file; older ones stay until the clip's retention ends.
Every lookup is scoped to the caller (no IDOR).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import compat
from ..auth.deps import require_user, require_verified_user
from ..auth.errors import ApiError, ApiRoute
from ..billing import catalog, ledger
from ..common import utcnow
from ..config import settings
from ..db import get_db
from ..models.core import User
from ..models.jobs import ACTIVE_STATUSES, BrandAsset, Clip, ClipVersion, Job
from . import service, storage

router = APIRouter(route_class=ApiRoute)

SPEC_KEYS = {"trim", "captions", "hook", "logo", "memes", "sfx", "music", "broll", "watermark_text"}
MAX_SPEC_BYTES = 32 * 1024
MAX_ACTIVE_PER_USER = 3
# Mirrors app/clipper/editor.py (a test keeps them in step); the engine enforces the real ones.
SPEC_LIMITS = {"max_memes": 10, "max_caption_chars": 40, "max_hook_chars": 80, "max_watermark_chars": 40,
               "min_clip_s": 3.0, "meme_duration_s": [0.5, 4.0], "logo_scale": [0.06, 0.25],
               "music_volume_db": [-30.0, -6.0]}


class RenderIn(BaseModel):
    spec: dict


def _manifest(clip: Clip) -> dict:
    path = storage.resolve(clip.manifest_path)
    if path is None or not path.is_file():
        raise ApiError(409, "not_editable", "This clip can't be edited (it was made before editing existed).")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ApiError(409, "not_editable", "This clip's edit data is unreadable.")


def _rerenders_used(db: Session, clip: Clip) -> int:
    return int(db.query(func.count(Job.id)).filter(Job.kind == "rerender", Job.clip_id == clip.id,
                                                   Job.user_id == clip.user_id,
                                                   Job.status.notin_(("failed", "canceled"))).scalar() or 0)


def _limits(db: Session, user: User, clip: Clip) -> dict:
    used = _rerenders_used(db, clip)
    free = max(0, int(settings.rerender_free_per_clip))
    return {**SPEC_LIMITS, "free_rerenders": free, "rerenders_used": used, "free_left": max(0, free - used),
            "credit_cost_after_free": int(settings.rerender_credit_cost), "balance": ledger.balance(db, user.id)}


def _public_meme(m: dict) -> dict:
    return {k: m.get(k) for k in ("id", "title", "provider", "mood") if k in m}


def edit_view(db: Session, user: User, clip: Clip) -> dict:
    manifest = _manifest(clip)
    brand_memes = (db.query(BrandAsset).filter(BrandAsset.user_id == user.id, BrandAsset.kind == "meme",
                                               BrandAsset.deleted_at.is_(None), BrandAsset.meme_key.is_not(None))
                   .order_by(BrandAsset.created_at.desc()).all())
    logo = (db.query(BrandAsset.id).filter(BrandAsset.user_id == user.id, BrandAsset.kind == "logo",
                                           BrandAsset.deleted_at.is_(None)).first())
    available = [_public_meme(m) for m in manifest.get("memes_available") or [] if isinstance(m, dict)]
    available += [{"id": a.meme_key, "title": a.original_name, "provider": "custom",
                   "url": f"/api/brand/memes/{a.id}/file"} for a in brand_memes]
    return {
        "clip_id": clip.id,
        "version": clip.version,
        "duration_s": manifest.get("duration_s", clip.duration_s),
        "spec": clip.spec or {},
        "words": manifest.get("captions") or [],
        "memes": {"in_clip": manifest.get("memes") or [], "available": available},
        "cutaways": manifest.get("cutaways") or [],
        "beats": manifest.get("beats") or [],
        "hook": (manifest.get("moment") or {}).get("hook", ""),
        "layout": manifest.get("layout"),
        "logo": {"available": bool(logo or manifest.get("logo")),
                 "url": "/api/brand/logo/file" if logo else None},
        "limits": _limits(db, user, clip),
    }


def _check_spec(spec: Any) -> dict:
    if not isinstance(spec, dict):
        raise ApiError(400, "invalid_spec", "The edit must be an object.")
    unknown = sorted(set(spec) - SPEC_KEYS)
    if unknown:
        raise ApiError(400, "invalid_spec", f"Unknown edit field: {unknown[0]}.", allowed=sorted(SPEC_KEYS))
    if len(json.dumps(spec)) > MAX_SPEC_BYTES:
        raise ApiError(413, "spec_too_large", "That edit is too large.")
    return spec


def queue_rerender(db: Session, user: User, clip: Clip, spec: Any, *, via: str = "web") -> Job:
    spec = _check_spec(spec)
    _manifest(clip)
    source = storage.resolve(clip.source_path)
    if source is None or not source.is_file():
        raise ApiError(409, "not_editable", "This clip's source excerpt is gone, so it can't be re-rendered.")
    if user.status != "active":
        raise ApiError(403, "account_unavailable", "This account can't start jobs.")
    if user.email_verified_at is None:
        raise ApiError(403, "email_unverified", "Confirm your email address first.")
    if user.billing_hold:
        raise ApiError(403, "billing_hold", f"Your account has a billing hold. Contact {settings.support_email}.")
    if db.query(Job.id).filter(Job.kind == "rerender", Job.clip_id == clip.id, Job.user_id == user.id,
                               Job.status.in_(ACTIVE_STATUSES)).first():
        raise ApiError(409, "rerender_in_progress", "This clip is already being re-rendered. Wait for it to finish.")
    active = db.query(func.count(Job.id)).filter(Job.kind == "rerender", Job.user_id == user.id,
                                                 Job.status.in_(ACTIVE_STATUSES)).scalar()
    if active >= MAX_ACTIVE_PER_USER:
        raise ApiError(429, "too_many_rerenders", "You have several edits rendering already. Wait for one to finish.")
    feats = catalog.features_for(db, user)
    paid = _rerenders_used(db, clip) >= max(0, int(settings.rerender_free_per_clip))
    cost = max(0, int(settings.rerender_credit_cost)) if paid else 0
    job = Job(user_id=user.id, kind="rerender", clip_id=clip.id, status="queued", stage="queued", progress=0.0,
              source_kind="clip", source_title=clip.title, source_duration_s=clip.duration_s,
              options={"spec": spec}, priority=int(feats.get("priority", 0)) + 1, watermark=False, via=via,
              credits_reserved=cost, funding="credits" if cost else "included")
    db.add(job)
    db.flush()
    if cost:
        try:
            ledger.reserve(db, user.id, job.id, cost)
        except ledger.InsufficientCredits as exc:
            db.rollback()
            raise ApiError(402, "insufficient_credits",
                           f"Your free edits for this clip are used up; another one costs {cost} credit(s) and you have "
                           f"{exc.available}.", needed=cost, balance=exc.available)
    db.commit()
    return job


def versions(db: Session, user_id: str, clip: Clip) -> list[dict]:
    rows = (db.query(ClipVersion).filter(ClipVersion.clip_id == clip.id, ClipVersion.user_id == user_id,
                                         ClipVersion.deleted_at.is_(None)).order_by(ClipVersion.version.desc()).all())
    return [{"version": v.version, "current": v.version == clip.version, "created_at": v.created_at.isoformat(),
             "duration_s": v.duration_s, "size_bytes": v.size_bytes, "spec": v.spec or {},
             "credits_charged": v.credits_charged, "job_id": v.job_id,
             "video_url": f"/api/clips/{clip.id}/versions/{v.version}/video"} for v in rows]


def purge_clip_files(clip: Clip, db: Session) -> None:
    """A clip's every version, its manifest, source excerpt and assets folder (retention / delete)."""
    for v in db.query(ClipVersion).filter(ClipVersion.clip_id == clip.id, ClipVersion.deleted_at.is_(None)).all():
        storage.remove_file(v.path)
        storage.remove_file(v.thumbnail_path)
        v.deleted_at = utcnow()
    storage.remove_file(clip.source_path)
    manifest = storage.resolve(clip.manifest_path)
    if manifest is not None:
        if manifest.name.endswith("_manifest.json"):
            storage.remove_tree(manifest.with_name(manifest.name[: -len("_manifest.json")] + "_assets"))
        storage.remove_file(clip.manifest_path)


# ---- routes ---------------------------------------------------------------------------------------

@router.get("/api/clips/{clip_id}/edit")
def get_edit(clip_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return edit_view(db, user, service.get_clip(db, user.id, clip_id))


@router.post("/api/clips/{clip_id}/render", status_code=202)
def render_edit(clip_id: str, body: RenderIn, request: Request, user: User = Depends(require_verified_user),
                db: Session = Depends(get_db)):
    compat.rate_limit(request, "rerender", 30, 3600, key=f"user:{user.id}")
    clip = service.get_clip(db, user.id, clip_id)
    job = queue_rerender(db, user, clip, body.spec)
    return service.job_view(db, job, include_clips=False)


@router.get("/api/clips/{clip_id}/versions")
def get_versions(clip_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = service.get_clip(db, user.id, clip_id)
    return {"clip_id": clip.id, "current": clip.version, "versions": versions(db, user.id, clip)}


@router.get("/api/clips/{clip_id}/versions/{version}/video")
def version_video(clip_id: str, version: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = service.get_clip(db, user.id, clip_id)
    row: Optional[ClipVersion] = (db.query(ClipVersion)
                                  .filter(ClipVersion.clip_id == clip.id, ClipVersion.user_id == user.id,
                                          ClipVersion.version == version, ClipVersion.deleted_at.is_(None))
                                  .one_or_none())
    path = storage.resolve(row.path) if row else None
    if path is None or not path.is_file():
        raise ApiError(404, "not_found", "That version doesn't exist.")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})
