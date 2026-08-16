"""Autonomous scheduler using APScheduler.

Replaces Celery+Redis for local mode. Runs inside the FastAPI process.
Schedules:
  - scrape_job: every SCRAPE_INTERVAL_HOURS (default 6h)
  - discovery_job: every DISCOVERY_INTERVAL_HOURS (default 24h)
  - publish_job: every UPLOAD_INTERVAL_HOURS (default 8h) — picks next transformed video and publishes
  - cleanup_job: daily — deletes old source video files
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import desc, select

from .config import settings
from .db import session_scope, init_db
from .models import Creator, Video, VideoStatus
from . import discovery, quota
from .analytics import recompute_scores

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


# ---------- Job impls (importable so they can be triggered manually too) ----------
def scrape_job(handles: list[str] | None = None,
               hashtags: list[str] | None = None,
               max_per_run: int | None = None) -> dict:
    """Run a scrape cycle. Delegates to tasks.pipeline.scrape_task for
    multi-platform support ( Snapchat via Playwright + yt-dlp )."""
    from tasks.pipeline import scrape_task
    return scrape_task(handles=handles, hashtags=hashtags, max_per_run=max_per_run)


def discovery_job() -> dict:
    """Discover new creators from recent viral videos."""
    with session_scope() as db:
        new = discovery.discover_creators(db, days=7, top_n=5)
    log.info("discovery_job: discovered=%d", len(new))
    return {"discovered": len(new)}


def publish_job() -> dict:
    """Pick the next transformed video and publish it, if quota allows."""
    if not settings.auto_publish:
        return {"skipped": "auto_publish disabled"}
    with session_scope() as db:
        # Quota check
        if quota.remaining(db) <= 0:
            return {"skipped": "quota exhausted"}
        # Find next transformed video (oldest first)
        v = db.execute(
            select(Video)
            .where(Video.status == VideoStatus.TRANSFORMED)
            .order_by(Video.transformed_at)
            .limit(1)
        ).scalar_one_or_none()
        if v is None:
            return {"skipped": "no transformed videos ready"}
        # Make sure we haven't already published today's allotment
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        todays = db.execute(
            select(Video).where(
                Video.status == VideoStatus.PUBLISHED,
                Video.published_at.isnot(None),
            )
        ).scalars().all()
        todays_count = sum(
            1 for t in todays
            if t.published_at and t.published_at.strftime("%Y-%m-%d") == today
        )
        if todays_count >= settings.uploads_per_day:
            return {"skipped": f"daily cap reached ({todays_count}/{settings.uploads_per_day})"}
        video_id = v.id

    # Delegate to publish_task (runs synchronously — no Celery needed)
    from tasks.pipeline import publish_task, generate_metadata_for_video
    # Ensure metadata exists
    generate_metadata_for_video(video_id)
    result = publish_task(video_id, meta={})
    log.info("publish_job: video=%s result=%s", video_id, result)
    return result


def cleanup_job() -> dict:
    """Delete downloaded source video files older than SOURCE_VIDEO_MAX_AGE_DAYS."""
    from .config import settings
    max_age_days = settings.source_video_max_age_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    deleted = 0
    with session_scope() as db:
        rows = db.execute(
            select(Video).where(
                Video.local_path.isnot(None),
                Video.scraped_at < cutoff,
                Video.status.in_([VideoStatus.PUBLISHED, VideoStatus.REJECTED]),
            )
        ).scalars().all()
        for v in rows:
            if v.local_path and os.path.exists(v.local_path):
                try:
                    os.remove(v.local_path)
                    v.local_path = None
                    deleted += 1
                except OSError:
                    pass
    log.info("cleanup_job: deleted=%d old source files", deleted)
    return {"deleted": deleted}


def chop_long_job() -> dict:
    """Auto-chop long videos pending/approved that exceed duration threshold."""
    from .config import settings
    from tasks.pipeline import long_to_shorts_task

    min_dur = getattr(settings, "long_video_min_duration_sec", 120)
    batch = 5
    processed, clips = 0, 0
    with session_scope() as db:
        long_vids = db.execute(
            select(Video).where(
                Video.duration_sec >= min_dur,
                Video.status.in_([VideoStatus.PENDING, VideoStatus.APPROVED]),
                Video.local_path.isnot(None),
            )
        ).scalars().all()
    for v in long_vids[:batch]:
        result = long_to_shorts_task(v.id)
        if result.get("ok"):
            clips += result.get("clips_created", 0)
        processed += 1
    log.info("chop_long_job: processed=%d new_clips=%d", processed, clips)
    return {"processed": processed, "clips_created": clips}


def recompute_scores_job() -> dict:
    """Refresh all engagement/velocity scores."""
    with session_scope() as db:
        rows = db.execute(select(Video)).scalars().all()
        recompute_scores(rows)
    return {"updated": len(rows)}


# ---------- Scheduler lifecycle ----------
def start_scheduler() -> BackgroundScheduler:
    """Start the background scheduler. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    init_db()

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        scrape_job,
        IntervalTrigger(hours=settings.scrape_interval_hours),
        id="scrape",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),  # run once at startup
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        discovery_job,
        IntervalTrigger(hours=settings.discovery_interval_hours),
        id="discovery",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        publish_job,
        IntervalTrigger(hours=settings.upload_interval_hours),
        id="publish",
        # Don't auto-publish on startup — wait for the first scheduled slot.
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        cleanup_job,
        IntervalTrigger(hours=24),
        id="cleanup",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        chop_long_job,
        IntervalTrigger(hours=6),
        id="chop_long",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        recompute_scores_job,
        IntervalTrigger(hours=1),
        id="recompute",
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    _scheduler = sched
    log.info("Scheduler started: scrape=%dh discovery=%dh publish=%dh cleanup=24h",
             settings.scrape_interval_hours, settings.discovery_interval_hours,
             settings.upload_interval_hours)
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
