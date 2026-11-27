"""The clip editor API: read what a clip was made of, queue a re-render with an edit spec, list versions.

Clips are burned-in (captions, memes, sound are in the pixels and the mix), so an edit is a server-side
re-render (app.clipper.standalone.rerender_clip) from the bundle the engine keeps beside each clip:
`<clip>_manifest.json`, `<clip>_source.mp4` and `<clip>_assets/`. The spec format is the pipeline's
(app/clipper/editor.py `_SPEC_SHAPE`, documented in CONTRACT.md); here it is size- and shape-checked and
the engine validates it fully (a spec it can't apply fails the job with code `invalid_spec`, credits back).

* `GET /api/clips/{id}/edit` -> {clip_id, version, duration_s, spec, options, words, memes {in_clip,
  available, editable}, cutaways, beats, hook, layout, logo {available, url, in_clip, showing}, active_job_id,
  limits}.
* `GET /api/clips/{id}/memes/{meme_id}/file`: a library meme's picture (only ids the manifest lists, only
  files inside the clip's `<stem>_assets/`).
* `POST /api/clips/{id}/render {spec}` -> 202 the queued re-render job (kind "rerender"). The first
  SAAS_RERENDER_FREE_PER_CLIP re-renders of a clip are included; after that each reserves
  SAAS_RERENDER_CREDIT_COST credits (released if it fails). One re-render at a time per clip.
* `GET /api/clips/{id}/versions` and `GET /api/clips/{id}/versions/{n}/video`. The newest version is the
  clip's file; older ones stay until the clip's retention ends.
Every lookup is scoped to the caller (no IDOR).

The watermark is never the customer's to set: `watermark_text` is dropped from every posted and stored
spec, and the worker fills it from the customer's plan when it renders (`plan_watermark`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

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
# Spec keys only the server sets. A posted or stored value is dropped, never passed on.
SERVER_SPEC_KEYS = frozenset({"watermark_text"})
MAX_SPEC_BYTES = 32 * 1024
MAX_ACTIVE_PER_USER = 3
# Mirrors app/clipper/editor.py (a test keeps them in step); the engine enforces the real ones.
SPEC_LIMITS = {"max_memes": 10, "max_caption_chars": 40, "max_hook_chars": 80, "max_watermark_chars": 40,
               "min_clip_s": 3.0, "meme_duration_s": [0.5, 4.0], "logo_scale": [0.06, 0.25],
               "music_volume_db": [-30.0, -6.0]}


# What a library meme's file may be served as (by suffix; anything else is not served).
_MEME_TYPES = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


class RenderIn(BaseModel):
    spec: dict


def customer_spec(spec: Any) -> dict:
    """`spec` without the server-only keys: what is stored, shown, and handed back to the engine."""
    if not isinstance(spec, dict):
        return {}
    return {k: v for k, v in spec.items() if k not in SERVER_SPEC_KEYS}


def plan_watermark(db: Session, user: User) -> str:
    """The watermark a render for this customer carries: the brand on the free tier, none once they pay.
    Fails closed: a plan that doesn't say keeps the watermark. Always a string, so the engine never falls
    back to the watermark the clip was first made with."""
    if not catalog.features_for(db, user).get("watermark", True):
        return ""
    return " ".join(str(settings.brand_name or "").split())[: SPEC_LIMITS["max_watermark_chars"]]


def logo_wanted(manifest: dict, spec: dict) -> bool:
    """Whether a re-render gets the brand-kit logo: only when the clip already had a logo or the spec turns
    one on, and never when the spec turns it off (a clip must not gain a logo just by being re-rendered)."""
    part = spec.get("logo")
    enabled = part.get("enabled") if isinstance(part, dict) else None
    if enabled is False:
        return False
    return bool(manifest.get("logo")) or enabled is True


def _bundle(clip: Clip) -> tuple[Path, dict]:
    path = storage.resolve(clip.manifest_path)
    if path is None or not path.is_file():
        raise ApiError(409, "not_editable", "This clip can't be edited (it was made before editing existed).")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ApiError(409, "not_editable", "This clip's edit data is unreadable.")
    if not isinstance(data, dict):
        raise ApiError(409, "not_editable", "This clip's edit data is unreadable.")
    return path, data


def _manifest(clip: Clip) -> dict:
    return _bundle(clip)[1]


def _assets_dir(manifest_path: Path) -> Path:
    name = manifest_path.name
    stem = name[: -len("_manifest.json")] if name.endswith("_manifest.json") else manifest_path.stem
    return manifest_path.parent / f"{stem}_assets"


def _meme_file(manifest_path: Path, manifest: dict, meme_id: str) -> Optional[tuple[Path, str]]:
    """(file, media type) of a library meme the manifest lists, confined to the clip's assets folder."""
    for m in manifest.get("memes_available") or []:
        if not isinstance(m, dict) or m.get("id") != meme_id or not isinstance(m.get("path"), str):
            continue
        p = (manifest_path.parent / m["path"]).resolve()
        media = _MEME_TYPES.get(p.suffix.lower())
        if media and storage.within(p, _assets_dir(manifest_path)) and p.is_file():
            return p, media
        return None
    return None


def _active_rerender(db: Session, user_id: str, clip: Clip) -> Optional[Job]:
    return (db.query(Job).filter(Job.kind == "rerender", Job.clip_id == clip.id, Job.user_id == user_id,
                                 Job.status.in_(ACTIVE_STATUSES)).order_by(Job.created_at.desc()).first())


def _rerenders_used(db: Session, clip: Clip) -> int:
    return int(db.query(func.count(Job.id)).filter(Job.kind == "rerender", Job.clip_id == clip.id,
                                                   Job.user_id == clip.user_id,
                                                   Job.status.notin_(("failed", "canceled"))).scalar() or 0)


def _limits(db: Session, user: User, clip: Clip) -> dict:
    used = _rerenders_used(db, clip)
    free = max(0, int(settings.rerender_free_per_clip))
    shown = {k: v for k, v in SPEC_LIMITS.items() if k != "max_watermark_chars"}  # not the customer's to set
    return {**shown, "free_rerenders": free, "rerenders_used": used, "free_left": max(0, free - used),
            "credit_cost_after_free": int(settings.rerender_credit_cost), "balance": ledger.balance(db, user.id)}


def _public_meme(clip: Clip, manifest_path: Path, manifest: dict, m: dict) -> dict:
    out = {k: m.get(k) for k in ("id", "title", "provider", "mood") if k in m}
    if isinstance(out.get("provider"), str):
        out["provider"] = out["provider"].lower()  # the pipeline says "Custom"; the brand kit says "custom"
    found = _meme_file(manifest_path, manifest, m["id"])
    out["thumb_url"] = f"/api/clips/{clip.id}/memes/{quote(m['id'], safe='')}/file" if found else None
    out["media_type"] = found[1] if found else None
    return out


def edit_view(db: Session, user: User, clip: Clip) -> dict:
    manifest_path, manifest = _bundle(clip)
    brand_memes = (db.query(BrandAsset).filter(BrandAsset.user_id == user.id, BrandAsset.kind == "meme",
                                               BrandAsset.deleted_at.is_(None), BrandAsset.meme_key.is_not(None))
                   .order_by(BrandAsset.created_at.desc()).all())
    logo = (db.query(BrandAsset.id).filter(BrandAsset.user_id == user.id, BrandAsset.kind == "logo",
                                           BrandAsset.deleted_at.is_(None)).first())
    available = [_public_meme(clip, manifest_path, manifest, m) for m in manifest.get("memes_available") or []
                 if isinstance(m, dict) and isinstance(m.get("id"), str)]
    seen = {m["id"] for m in available}
    # The engine copies the customer's own memes into the bundle too, so one id can come from both places.
    available += [{"id": a.meme_key, "title": a.original_name, "provider": "custom", "media_type": a.media_type,
                   "url": f"/api/brand/memes/{a.id}/file"} for a in brand_memes if a.meme_key not in seen]
    spec = customer_spec(clip.spec)
    in_clip = bool(manifest.get("logo"))
    logo_part = spec.get("logo") if isinstance(spec.get("logo"), dict) else {}
    showing = logo_part["enabled"] if isinstance(logo_part.get("enabled"), bool) else in_clip
    active = _active_rerender(db, user.id, clip)
    options = manifest.get("options")
    return {
        "clip_id": clip.id,
        "version": clip.version,
        "duration_s": manifest.get("duration_s", clip.duration_s),
        "spec": spec,
        "options": options if isinstance(options, dict) else {},
        "words": manifest.get("captions") or [],
        "memes": {"in_clip": manifest.get("memes") or [], "available": available,
                  "editable": manifest.get("edit") is not None},
        "cutaways": manifest.get("cutaways") or [],
        "beats": manifest.get("beats") or [],
        "hook": (manifest.get("moment") or {}).get("hook", ""),
        "layout": manifest.get("layout"),
        "logo": {"available": bool(logo or in_clip), "url": "/api/brand/logo/file" if logo else None,
                 "in_clip": in_clip, "showing": bool(showing)},
        "active_job_id": active.id if active else None,
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
    spec = customer_spec(_check_spec(spec))
    manifest = _manifest(clip)
    if "logo" in spec and not isinstance(spec["logo"], dict):
        raise ApiError(400, "invalid_spec", "logo must be an object.")
    part = spec.get("logo") or {}
    if not manifest.get("logo") and part.get("enabled") is None and {"position", "scale"} & set(part):
        raise ApiError(400, "invalid_spec", "This clip has no logo. Set logo.enabled to true to add one.")
    if logo_wanted(manifest, spec) and not manifest.get("logo") and not (
            db.query(BrandAsset.id).filter(BrandAsset.user_id == user.id, BrandAsset.kind == "logo",
                                           BrandAsset.deleted_at.is_(None)).first()):
        raise ApiError(400, "invalid_spec", "This clip has no logo to show. Add one to your brand kit first.")
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
              options={"spec": spec}, priority=int(feats.get("priority", 0)) + 1,
              watermark=bool(feats.get("watermark", True)), via=via,
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
             "duration_s": v.duration_s, "size_bytes": v.size_bytes, "spec": customer_spec(v.spec),
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


@router.get("/api/clips/{clip_id}/memes/{meme_id}/file")
def meme_file(clip_id: str, meme_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = service.get_clip(db, user.id, clip_id)
    manifest_path, manifest = _bundle(clip)
    found = _meme_file(manifest_path, manifest, meme_id)
    if found is None:
        raise ApiError(404, "not_found", "That meme isn't in this clip's library.")
    path, media = found
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=3600"})


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
