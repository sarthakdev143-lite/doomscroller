"""Job and clip logic shared by the web API, the public API v1 and the MCP server.

Every query takes the caller's user id and filters on it, so an id from another account is simply
"not found" (no IDOR, no existence oracle). Credits are reserved when the job is created (ceil of
source minutes) inside the same transaction as the job row; the worker settles or releases.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from ..auth.errors import ApiError
from ..billing import catalog, ledger
from ..common import utcnow
from ..config import settings
from ..models.core import User
from ..models.jobs import ACTIVE_STATUSES, FINAL_STATUSES, Clip, Job, Upload
from . import probe, ssrf, storage

log = logging.getLogger("saas.jobs")


class JobOptions(BaseModel):
    model_config = {"extra": "forbid"}

    clips: int = Field(default=3, ge=1, le=20)
    language: Literal["auto", "en", "hi"] = "auto"
    layout: Literal["framed", "fill"] = "framed"
    captions: bool = True
    broll: bool = True
    memes: bool = True
    sfx: bool = True
    music: bool = False
    max_clip_seconds: int = Field(default=60, ge=15, le=90)
    logo: bool = True  # use the brand-kit logo, if one is uploaded
    custom_memes: bool = True  # offer the brand-kit memes before the library ones


def parse_options(raw: Optional[dict]) -> JobOptions:
    try:
        return JobOptions(**(raw or {}))
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", ()))
        raise ApiError(400, "invalid_options", f"Invalid option {field}: {first.get('msg', 'invalid')}.")
    except TypeError:
        raise ApiError(400, "invalid_options", "Options must be an object.")


def credits_for(duration_s: float) -> int:
    return max(1, math.ceil(float(duration_s) / 60.0))


# ---- estimate -------------------------------------------------------------------------------------

def estimate_url(db: Session, user: Optional[User], url: str) -> dict:
    meta = probe.ytdlp_metadata(url)
    credits = credits_for(meta["duration_s"])
    out = {"ok": True, "title": meta["title"], "duration_s": meta["duration_s"], "thumbnail": meta["thumbnail"],
           "credits": credits, "source": meta.get("extractor")}
    if user is not None:
        feats = catalog.features_for(db, user)
        max_s = int(feats.get("max_source_minutes", 30)) * 60
        bal = ledger.balance(db, user.id)
        free_m, free_s = ledger.free_balance(db, user.id)
        out["balance"] = bal
        out["free"] = {"minutes": free_m, "shorts": free_s}
        # Free meters fund a job only when they cover the minutes and at least one short.
        out["funding"] = "free" if (free_m >= credits and free_s >= 1) else "credits"
        if meta["duration_s"] > max_s:
            out.update(ok=False, reason="source_too_long",
                       message=f"Videos can be up to {max_s // 60} minutes on your plan.")
        elif out["funding"] == "credits" and bal < credits:
            out.update(ok=False, reason="insufficient_credits",
                       message=f"This video needs {credits} credits; you have {bal}.")
    return out


# ---- create ---------------------------------------------------------------------------------------

def _check_caps(db: Session, user: User, feats: dict, options: JobOptions) -> None:
    if user.status != "active":
        raise ApiError(403, "account_unavailable", "This account can't start jobs.")
    if user.email_verified_at is None:
        raise ApiError(403, "email_unverified", "Confirm your email address first.")
    if user.billing_hold:
        raise ApiError(403, "billing_hold", f"Your account has a billing hold. Contact {settings.support_email}.")
    if options.clips > int(feats.get("max_clips", 3)):
        raise ApiError(400, "invalid_options", f"Your plan allows up to {feats.get('max_clips')} clips per job.")
    active = db.query(func.count(Job.id)).filter(Job.user_id == user.id, Job.kind == "clip",
                                                 Job.status.in_(ACTIVE_STATUSES)).scalar()
    if active >= int(feats.get("concurrency", 1)):
        raise ApiError(429, "too_many_active_jobs",
                       f"You can run {feats.get('concurrency')} job(s) at once. Wait for one to finish.")
    since = utcnow() - timedelta(hours=24)
    today = db.query(func.count(Job.id)).filter(Job.user_id == user.id, Job.kind == "clip",
                                                Job.created_at >= since).scalar()
    if today >= int(feats.get("daily_jobs", 3)):
        raise ApiError(429, "daily_limit", f"Your plan allows {feats.get('daily_jobs')} jobs per day.")


def create_job(db: Session, user: User, *, source_url: Optional[str] = None, upload_id: Optional[str] = None,
               options: Optional[dict] = None, via: str = "web", api_key_id: Optional[str] = None) -> Job:
    if bool(source_url) == bool(upload_id):
        raise ApiError(400, "invalid_source", "Send exactly one of source_url or upload_id.")
    opts = parse_options(options)
    feats = catalog.features_for(db, user)
    _check_caps(db, user, feats, opts)
    max_s = int(feats.get("max_source_minutes", 30)) * 60

    job = Job(user_id=user.id, status="queued", stage="queued", progress=0.0, options=opts.model_dump(),
              priority=int(feats.get("priority", 0)), watermark=bool(feats.get("watermark", True)), via=via,
              api_key_id=api_key_id)
    if source_url:
        safe = ssrf.validate_url(source_url)
        meta = probe.ytdlp_metadata(safe)
        duration = meta["duration_s"]
        job.source_kind = "url"
        job.source_url = safe
        job.source_title = meta["title"]
        job.source_thumbnail = meta["thumbnail"]
    else:
        up = (db.query(Upload).filter(Upload.id == upload_id, Upload.user_id == user.id, Upload.deleted_at.is_(None))
              .one_or_none())
        if up is None or (up.expires_at is not None and up.expires_at <= utcnow()):
            raise ApiError(404, "not_found", "Upload not found.")
        if up.job_id is not None:
            raise ApiError(409, "upload_used", "That upload already has a job.")
        duration = up.duration_s  # the stored, ffprobe-measured duration; never client input
        job.source_kind = "upload"
        job.upload_id = up.id
        job.source_title = up.original_name
    if duration > max_s:
        raise ApiError(400, "source_too_long", f"Videos can be up to {max_s // 60} minutes on your plan.")
    job.source_duration_s = float(duration)
    credits = credits_for(duration)
    job.credits_reserved = credits
    db.add(job)
    db.flush()
    if upload_id:
        res = db.execute(update(Upload).where(Upload.id == upload_id, Upload.user_id == user.id,
                                              Upload.job_id.is_(None)).values(job_id=job.id))
        if res.rowcount != 1:
            db.rollback()
            raise ApiError(409, "upload_used", "That upload already has a job.")
    free_m, free_s = ledger.free_balance(db, user.id)
    try:
        entry = ledger.reserve(db, user.id, job.id, credits, shorts=opts.clips)
    except ledger.InsufficientCredits as exc:
        db.rollback()
        hint = ""
        if free_m or free_s:
            hint = (f" Your free tier has {free_m} minutes and {free_s} shorts left, which doesn't cover "
                    f"{credits} minutes and {opts.clips} shorts.")
        raise ApiError(402, "insufficient_credits",
                       f"This video needs {exc.needed} credits; you have {exc.available}.{hint}",
                       needed=exc.needed, balance=exc.available, free_minutes=free_m, free_shorts=free_s)
    if (entry.meta or {}).get("source") == "free":
        job.funding = "free"
        job.shorts_reserved = opts.clips
    db.commit()
    return job


# ---- read -----------------------------------------------------------------------------------------

def get_job(db: Session, user_id: str, job_id: str) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user_id).one_or_none()
    if job is None:
        raise ApiError(404, "not_found", "Job not found.")
    return job


def list_jobs(db: Session, user_id: str, *, status: Optional[str] = None, limit: int = 20,
              before: Optional[str] = None, kind: Optional[str] = "clip") -> list[Job]:
    q = db.query(Job).filter(Job.user_id == user_id)
    if kind:
        q = q.filter(Job.kind == kind)
    if status:
        q = q.filter(Job.status == status)
    if before:
        anchor = db.query(Job.created_at).filter(Job.id == before, Job.user_id == user_id).first()
        if anchor:
            q = q.filter(Job.created_at < anchor[0])
    return q.order_by(Job.created_at.desc()).limit(max(1, min(int(limit), 100))).all()


def clip_view(clip: Clip, *, signed: bool = False) -> dict:
    out = {"id": clip.id, "job_id": clip.job_id, "index": clip.idx, "title": clip.title,
           "description": clip.description, "hashtags": list(clip.hashtags or []), "duration_s": clip.duration_s,
           "start_s": clip.start_s, "end_s": clip.end_s, "score": clip.score, "language": clip.language,
           "size_bytes": clip.size_bytes, "created_at": clip.created_at.isoformat()}
    if signed:
        out["video_url"], exp = storage.signed_file_url(clip.id, "video")
        out["download_url"], _ = storage.signed_file_url(clip.id, "download")
        out["thumbnail_url"], _ = storage.signed_file_url(clip.id, "thumbnail")
        out["links_expire_at"] = exp
    else:
        out["video_url"] = f"/api/clips/{clip.id}/video"
        out["download_url"] = f"/api/clips/{clip.id}/download"
        out["thumbnail_url"] = f"/api/clips/{clip.id}/thumbnail"
    return out


def job_view(db: Session, job: Job, *, include_clips: bool = True, signed: bool = False) -> dict:
    out = {"id": job.id, "kind": job.kind, "clip_id": job.clip_id, "status": job.status, "stage": job.stage, "progress": round(float(job.progress or 0), 3),
           "source": {"kind": job.source_kind, "url": job.source_url, "upload_id": job.upload_id,
                      "title": job.source_title, "duration_s": job.source_duration_s,
                      "thumbnail": job.source_thumbnail},
           "options": job.options, "credits_reserved": job.credits_reserved, "credits_charged": job.credits_charged,
           "funding": job.funding, "shorts_reserved": job.shorts_reserved, "shorts_charged": job.shorts_charged,
           "watermark": job.watermark, "via": job.via,
           "created_at": job.created_at.isoformat(),
           "started_at": job.started_at.isoformat() if job.started_at else None,
           "finished_at": job.finished_at.isoformat() if job.finished_at else None,
           "cancel_requested": bool(job.cancel_requested), "notes": list(job.notes or []),
           "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None}
    if include_clips:
        clips = (db.query(Clip).filter(Clip.job_id == job.id, Clip.user_id == job.user_id, Clip.deleted_at.is_(None))
                 .order_by(Clip.idx).all())
        out["clips"] = [clip_view(c, signed=signed) for c in clips]
    return out


# ---- cancel ---------------------------------------------------------------------------------------

def cancel_job(db: Session, user_id: str, job_id: str) -> Job:
    job = get_job(db, user_id, job_id)
    if job.status in FINAL_STATUSES:
        return job
    now = utcnow()
    res = db.execute(update(Job).where(Job.id == job.id, Job.user_id == user_id, Job.status == "queued")
                     .values(status="canceled", stage="canceled", finished_at=now, cancel_requested=True,
                             error_code="canceled", error_message="Canceled before it started."))
    if res.rowcount == 1:
        ledger.release(db, job.id, note="Job canceled")
        _free_upload(db, job)
    else:  # running: the worker sees the flag at its next stage boundary
        db.execute(update(Job).where(Job.id == job.id, Job.user_id == user_id).values(cancel_requested=True))
    db.commit()
    db.refresh(job)
    return job


def _free_upload(db: Session, job: Job) -> None:
    if job.upload_id:
        up = db.query(Upload).filter(Upload.id == job.upload_id, Upload.user_id == job.user_id).one_or_none()
        if up is not None and up.deleted_at is None:
            storage.remove_file(up.path)
            up.deleted_at = utcnow()


# ---- clips ----------------------------------------------------------------------------------------

def get_clip(db: Session, user_id: str, clip_id: str) -> Clip:
    clip = (db.query(Clip).filter(Clip.id == clip_id, Clip.user_id == user_id, Clip.deleted_at.is_(None))
            .one_or_none())
    if clip is None:
        raise ApiError(404, "not_found", "Clip not found.")
    return clip


def list_clips(db: Session, user_id: str, *, job_id: Optional[str] = None, limit: int = 50,
               before: Optional[str] = None) -> list[Clip]:
    q = db.query(Clip).filter(Clip.user_id == user_id, Clip.deleted_at.is_(None))
    if job_id:
        q = q.filter(Clip.job_id == job_id)
    if before:
        anchor = db.query(Clip.created_at).filter(Clip.id == before, Clip.user_id == user_id).first()
        if anchor:
            q = q.filter(Clip.created_at < anchor[0])
    return q.order_by(Clip.created_at.desc(), Clip.idx).limit(max(1, min(int(limit), 200))).all()


def update_clip(db: Session, user_id: str, clip_id: str, *, title: Optional[str] = None,
                description: Optional[str] = None, hashtags: Optional[list[str]] = None) -> Clip:
    clip = get_clip(db, user_id, clip_id)
    if title is not None:
        clip.title = title.strip()[:100]
    if description is not None:
        clip.description = description[:5000]
    if hashtags is not None:
        tags = []
        for t in hashtags[:30]:
            t = "".join(ch for ch in str(t) if ch.isalnum() or ch in "_#")[:60]
            if t:
                tags.append(t if t.startswith("#") else f"#{t}")
        clip.hashtags = list(dict.fromkeys(tags))
    db.commit()
    return clip


def delete_clip(db: Session, user_id: str, clip_id: str) -> None:
    from . import editor

    clip = get_clip(db, user_id, clip_id)
    editor.purge_clip_files(clip, db)
    storage.remove_file(clip.path)
    storage.remove_file(clip.thumbnail_path)
    clip.deleted_at = utcnow()
    db.commit()


def clip_file(clip: Clip, kind: str):
    rel = clip.thumbnail_path if kind == "thumbnail" else clip.path
    path = storage.resolve(rel)
    if path is None or not path.is_file():
        raise ApiError(404, "not_found", "File not found.")
    return path


def recent_memes(db: Session, user_id: str, limit: int = 40) -> list[str]:
    rows = (db.query(Clip.memes_used).filter(Clip.user_id == user_id).order_by(Clip.created_at.desc()).limit(20).all())
    seen: list[str] = []
    for (memes,) in rows:
        for m in memes or []:
            if isinstance(m, str) and m not in seen:
                seen.append(m)
    return seen[:limit]


# ---- cleanup --------------------------------------------------------------------------------------

def purge_user_content(db: Session, user: User) -> None:
    """Account deletion: cancel live jobs (credits released), delete every file, soft-delete rows."""
    now = utcnow()
    for job in db.query(Job).filter(Job.user_id == user.id, Job.status.in_(ACTIVE_STATUSES)).all():
        db.execute(update(Job).where(Job.id == job.id).values(status="canceled", stage="canceled", finished_at=now,
                                                              cancel_requested=True, error_code="account_deleted",
                                                              error_message="Account deleted."))
        ledger.release(db, job.id, note="Account deleted")
    db.execute(update(Clip).where(Clip.user_id == user.id, Clip.deleted_at.is_(None)).values(deleted_at=now))
    db.execute(update(Upload).where(Upload.user_id == user.id, Upload.deleted_at.is_(None)).values(deleted_at=now))
    from ..models.jobs import BrandAsset

    db.execute(update(BrandAsset).where(BrandAsset.user_id == user.id, BrandAsset.deleted_at.is_(None))
               .values(deleted_at=now))
    try:
        storage.remove_tree(storage.user_dir(user.id))
    except ValueError:
        pass
    db.flush()


def cleanup_retention(db: Session) -> dict:
    """Delete clips past SAAS_CLIP_RETENTION_DAYS, unused uploads past their TTL, and, as a backstop for a
    worker that died mid-job, consumed uploads and scratch dirs of finished jobs plus stale temp zips."""
    import time as _time

    now = utcnow()
    out = {"clips": 0, "uploads": 0, "consumed_uploads": 0, "work_dirs": 0}
    consumed = (db.query(Upload).join(Job, Job.id == Upload.job_id)
                .filter(Upload.deleted_at.is_(None), Job.status.in_(FINAL_STATUSES)).limit(1000).all())
    for up in consumed:
        storage.remove_file(up.path)
        up.deleted_at = now
        out["consumed_uploads"] += 1
    for work in storage.root().glob("*/jobs/*/work"):
        job = db.get(Job, work.parent.name)
        if job is None or job.status in FINAL_STATUSES:
            storage.remove_tree(work)
            out["work_dirs"] += 1
    for stale in storage.tmp_dir().glob("*"):
        try:
            if stale.is_file() and _time.time() - stale.stat().st_mtime > 3600:
                stale.unlink()
        except OSError:
            pass
    cutoff = now - timedelta(days=settings.clip_retention_days)
    from . import editor

    for clip in db.query(Clip).filter(Clip.deleted_at.is_(None), Clip.created_at < cutoff).limit(1000).all():
        editor.purge_clip_files(clip, db)
        storage.remove_file(clip.path)
        storage.remove_file(clip.thumbnail_path)
        clip.deleted_at = now
        out["clips"] += 1
    for up in (db.query(Upload).filter(Upload.deleted_at.is_(None), Upload.job_id.is_(None),
                                       Upload.expires_at.is_not(None), Upload.expires_at < now).limit(1000).all()):
        storage.remove_file(up.path)
        up.deleted_at = now
        out["uploads"] += 1
    db.commit()
    return out
