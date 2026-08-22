"""FastAPI app."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from .analytics import recompute_scores
from .config import settings
from .db import get_db, init_db
from .models import Creator, MetadataDraft, Video, VideoStatus
from . import quota, scheduler
from .schemas import (
    ApproveBody,
    HealthOut,
    PublishBody,
    QuotaOut,
    ScrapeBody,
    TransformBody,
    VideoOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Start autonomous scheduler in background
    try:
        scheduler.start_scheduler()
    except Exception as e:
        # Don't fail API startup if scheduler can't start
        import logging
        logging.getLogger(__name__).warning("Scheduler start failed: %s", e)
    yield
    try:
        scheduler.stop_scheduler()
    except Exception:
        pass


app = FastAPI(title="Doomscroller API", version="0.2.0", lifespan=lifespan)


@app.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    pending = db.scalar(
        select(func.count(Video.id)).where(Video.status == VideoStatus.PENDING)
    ) or 0
    return HealthOut(
        status="ok",
        use_mock=settings.use_mock,
        pending_count=int(pending),
    )


@app.get("/videos/pending", response_model=List[VideoOut])
def list_pending(limit: int = 20, db: Session = Depends(get_db)) -> List[VideoOut]:
    rows = db.execute(
        select(Video)
        .where(Video.status == VideoStatus.PENDING)
        .order_by(desc(Video.velocity_score), desc(Video.engagement_score))
        .limit(limit)
    ).scalars().all()
    return [VideoOut.model_validate(r) for r in rows]


@app.get("/videos", response_model=List[VideoOut])
def list_videos(limit: int = 50, offset: int = 0,
                status: str | None = None,
                db: Session = Depends(get_db)) -> List[VideoOut]:
    q = select(Video).order_by(desc(Video.scraped_at))
    if status:
        try:
            q = q.where(Video.status == VideoStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid status: {status}")
    q = q.limit(limit).offset(offset)
    rows = db.execute(q).scalars().all()
    return [VideoOut.model_validate(r) for r in rows]


@app.get("/videos/{video_id}", response_model=VideoOut)
def get_video(video_id: str, db: Session = Depends(get_db)) -> VideoOut:
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="not found")
    return VideoOut.model_validate(v)


@app.post("/videos/{video_id}/approve", response_model=VideoOut)
def approve_video(video_id: str, body: ApproveBody,
                  db: Session = Depends(get_db)) -> VideoOut:
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="not found")
    if v.status != VideoStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"cannot approve from status={v.status.value}")
    v.status = VideoStatus.APPROVED if body.approved else VideoStatus.REJECTED
    v.curated_at = datetime.now(timezone.utc)
    if body.note:
        v.meta = {**(v.meta or {}), "curation_note": body.note}
    db.commit()
    db.refresh(v)
    return VideoOut.model_validate(v)


@app.post("/videos/{video_id}/transform", response_model=dict)
def queue_transform(video_id: str, body: TransformBody,
                    db: Session = Depends(get_db)) -> dict:
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="not found")
    if v.status not in (VideoStatus.APPROVED, VideoStatus.PENDING, VideoStatus.FAILED):
        raise HTTPException(status_code=400, detail=f"cannot transform from status={v.status.value}")
    recipe = body.model_dump(exclude_none=True)
    from tasks.pipeline import transform_task_async, transform_task
    # If Celery is wired up, .delay() enqueues; otherwise it runs sync.
    try:
        result = transform_task_async.delay(video_id, recipe)
        if hasattr(result, "id"):
            return {"task_id": result.id, "video_id": video_id, "queued": True}
        return {"task_id": None, "video_id": video_id, "sync": True, "result": result}
    except Exception as e:
        out = transform_task(video_id, recipe)
        return {"task_id": None, "video_id": video_id, "sync": True, "result": out}


@app.get("/videos/{video_id}/download")
def download_video(video_id: str, db: Session = Depends(get_db)):
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="not found")
    path = v.transformed_path or v.local_path
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="file not found on disk")
    filename = f"{v.id}.mp4"
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.post("/videos/{video_id}/publish", response_model=dict)
def queue_publish(video_id: str, body: PublishBody,
                  db: Session = Depends(get_db)) -> dict:
    if not settings.enable_publish:
        raise HTTPException(status_code=404, detail="publishing disabled in MVP")
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="not found")
    if v.status != VideoStatus.TRANSFORMED:
        raise HTTPException(status_code=400, detail=f"cannot publish from status={v.status.value}")
    meta = body.model_dump(exclude_none=True)
    from tasks.pipeline import publish_task_async, publish_task
    try:
        result = publish_task_async.delay(video_id, meta)
        if hasattr(result, "id"):
            return {"task_id": result.id, "video_id": video_id, "queued": True}
        return {"task_id": None, "video_id": video_id, "sync": True, "result": result}
    except Exception:
        out = publish_task(video_id, meta)
        return {"task_id": None, "video_id": video_id, "sync": True, "result": out}


@app.post("/scrape", response_model=dict)
def trigger_scrape(body: ScrapeBody | None = None) -> dict:
    body = body or ScrapeBody()
    payload = {
        "handles": body.handles or settings.handles_list,
        "hashtags": body.hashtags or settings.hashtags_list,
        "max_per_run": body.max_per_run or settings.scrape_max_per_run,
    }
    from tasks.pipeline import scrape_task_async, scrape_task
    try:
        result = scrape_task_async.delay(**payload)
        if hasattr(result, "id"):
            return {"task_id": result.id, "queued": True, "params": payload}
        return {"task_id": None, "sync": True, "result": result, "params": payload}
    except Exception:
        out = scrape_task(**payload)
        return {"task_id": None, "sync": True, "result": out, "params": payload}


@app.post("/scores/recompute", response_model=dict)
def recompute(db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(Video)).scalars().all()
    recompute_scores(rows)
    db.commit()
    return {"updated": len(rows)}


@app.get("/quota", response_model=QuotaOut)
def get_quota(db: Session = Depends(get_db)) -> QuotaOut:
    snap = quota.snapshot(db)
    return QuotaOut(**snap)


# ==================== Autonomous scheduler endpoints ====================
@app.get("/scheduler/jobs", response_model=dict)
def list_jobs() -> dict:
    """List scheduled jobs and their next run times."""
    try:
        sched = scheduler._scheduler
        if sched is None:
            return {"running": False, "jobs": []}
        jobs = []
        for j in sched.get_jobs():
            jobs.append({
                "id": j.id,
                "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
                "trigger": str(j.trigger),
            })
        return {"running": True, "jobs": jobs}
    except Exception as e:
        return {"running": False, "error": str(e)}


@app.post("/scheduler/run/{job_id}", response_model=dict)
def run_job_now(job_id: str) -> dict:
    """Trigger a scheduler job to run immediately."""
    if job_id == "publish" and not settings.enable_publish:
        raise HTTPException(status_code=404, detail="publishing disabled in MVP")
    if job_id == "chop_long" and not settings.enable_publish:
        raise HTTPException(status_code=404, detail="chop disabled in MVP")
    fn_map = {
        "scrape": scheduler.scrape_job,
        "discovery": scheduler.discovery_job,
        "publish": scheduler.publish_job,
        "cleanup": scheduler.cleanup_job,
        "chop_long": getattr(scheduler, "chop_long_job", scheduler.cleanup_job),
        "recompute": scheduler.recompute_scores_job,
    }
    fn = fn_map.get(job_id)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    out = fn()
    return {"job": job_id, "result": out}


# ==================== Metadata endpoints ====================
class MetadataBody:
    pass


@app.get("/metadata/{video_id}", response_model=dict)
def get_metadata(video_id: str, db: Session = Depends(get_db)) -> dict:
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="video not found")
    md = db.execute(
        select(MetadataDraft).where(MetadataDraft.video_id == video_id)
    ).scalar_one_or_none()
    if md is None:
        return {"video_id": video_id, "exists": False}
    return {
        "video_id": video_id, "exists": True,
        "title": md.title, "description": md.description,
        "tags": md.tags, "language": md.detected_language,
        "edited": md.edited, "generated_at": md.generated_at.isoformat(),
    }


@app.post("/metadata/{video_id}/generate", response_model=dict)
def generate_metadata_endpoint(video_id: str, db: Session = Depends(get_db)) -> dict:
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="video not found")
    from tasks.pipeline import generate_metadata_for_video
    return generate_metadata_for_video(video_id)


@app.put("/metadata/{video_id}", response_model=dict)
def update_metadata(video_id: str, body: dict, db: Session = Depends(get_db)) -> dict:
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="video not found")
    md = db.execute(
        select(MetadataDraft).where(MetadataDraft.video_id == video_id)
    ).scalar_one_or_none()
    if md is None:
        md = MetadataDraft(video_id=video_id, title="", description="", tags=[])
        db.add(md)
    if "title" in body:
        md.title = str(body["title"])[:200]
    if "description" in body:
        md.description = str(body["description"])[:5000]
    if "tags" in body and isinstance(body["tags"], list):
        md.tags = [str(t) for t in body["tags"]][:15]
    md.edited = True
    db.commit()
    return {"ok": True, "video_id": video_id}


# ==================== Creators endpoints ====================
@app.get("/creators", response_model=list)
def list_creators(limit: int = 50, db: Session = Depends(get_db)) -> list:
    rows = db.execute(
        select(Creator).order_by(desc(Creator.score), desc(Creator.is_seed)).limit(limit)
    ).scalars().all()
    return [
        {
            "id": c.id, "handle": c.handle, "platform": c.platform,
            "is_seed": c.is_seed, "score": c.score,
            "videos_scraped": c.videos_scraped,
            "last_scraped_at": c.last_scraped_at.isoformat() if c.last_scraped_at else None,
            "discovered_from": c.discovered_from,
        }
        for c in rows
    ]


# ==================== Chop long videos into Shorts ====================
@app.post("/videos/{video_id}/chop", response_model=dict)
def chop_long_to_shorts(video_id: str, db: Session = Depends(get_db)) -> dict:
    """Smart-split one long video into multiple Shorts using hot-clip heuristics."""
    if not settings.enable_publish:
        raise HTTPException(status_code=404, detail="chop disabled in MVP")
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(status_code=404, detail="not found")
    if not v.local_path or not os.path.exists(v.local_path):
        raise HTTPException(status_code=400, detail="source file not on disk")
    from tasks.pipeline import long_to_shorts_task
    return long_to_shorts_task(video_id)


# ==================== Stats endpoint ====================
@app.get("/stats", response_model=dict)
def stats(db: Session = Depends(get_db)) -> dict:
    by_status = db.execute(
        select(Video.status, func.count(Video.id)).group_by(Video.status)
    ).all()
    total_creators = db.scalar(select(func.count(Creator.id))) or 0
    return {
        "by_status": {s.value: c for s, c in by_status},
        "total_creators": total_creators,
        "use_mock": settings.use_mock,
        "uploads_per_day": settings.uploads_per_day,
        "auto_publish": settings.auto_publish,
        "enable_publish": settings.enable_publish,
        "enabled_platforms": settings.enabled_platforms_list,
    }
