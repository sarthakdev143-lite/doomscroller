"""Autonomous scheduler using APScheduler.

Replaces Celery+Redis for local mode. Runs inside the FastAPI process.
Uses BackgroundScheduler from the REAL apscheduler package (not a local stub).

Scheduled:
  - scrape      : every SCRAPE_INTERVAL_HOURS (default 6h, first run ~10s after startup)
  - discovery   : every DISCOVERY_INTERVAL_HOURS (default 24h)
  - transform   : every 1h — auto-transforms APPROVED videos
  - publish     : every UPLOAD_INTERVAL_HOURS (default 8h)
  - cleanup     : every 24h — removes old source files
  - recompute   : every 1h — refreshes engagement/velocity scores
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.executors.pool import ThreadPoolExecutor
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
    """Run a scrape cycle."""
    from tasks.pipeline import scrape_task
    log.info("scrape_job: handles=%s hashtags=%s max=%s", handles, hashtags, max_per_run)
    try:
        result = scrape_task(handles=handles, hashtags=hashtags, max_per_run=max_per_run)
        log.info("scrape_job: done result=%s", result)
        return result
    except Exception as e:
        log.exception("scrape_job failed: %s", e)
        return {"error": str(e)}


def discovery_job() -> dict:
    """Discover new creators from recent viral videos."""
    try:
        with session_scope() as db:
            new = discovery.discover_creators(db, days=7, top_n=5)
        log.info("discovery_job: discovered=%d", len(new))
        return {"discovered": len(new)}
    except Exception as e:
        log.exception("discovery_job failed: %s", e)
        return {"error": str(e)}


def transform_job() -> dict:
    """Auto-transform up to 3 APPROVED → TRANSFORMED per run."""
    from tasks.pipeline import transform_task
    BATCH = 3
    processed = 0
    errors: list[dict] = []
    try:
        with session_scope() as db:
            rows = db.execute(
                select(Video)
                .where(
                    Video.status == VideoStatus.APPROVED,
                    Video.transformed_path.is_(None),
                    Video.local_path.isnot(None),
                )
                .order_by(Video.curated_at)
                .limit(BATCH)
            ).scalars().all()
            video_ids = [r.id for r in rows]

        for vid in video_ids:
            try:
                log.info("transform_job: video=%s", vid)
                result = transform_task(vid)
                processed += 1
                log.info("transform_job: video=%s ok=%s", vid, result.get("ok"))
            except Exception as e:
                log.exception("transform_job: video=%s error=%s", vid, e)
                errors.append({"video_id": vid, "error": str(e)})

        log.info("transform_job: transformed=%d errors=%d", processed, len(errors))
        return {"transformed": processed, "errors": errors}
    except Exception as e:
        log.exception("transform_job outer failure: %s", e)
        return {"error": str(e), "transformed": 0}


def publish_job() -> dict:
    """Auto-publish next TRANSFORMED video if quota allows."""
    if not settings.auto_publish:
        return {"skipped": "auto_publish disabled"}

    try:
        with session_scope() as db:
            if quota.remaining(db) <= 0:
                return {"skipped": "quota exhausted"}

            v = db.execute(
                select(Video)
                .where(Video.status == VideoStatus.TRANSFORMED)
                .order_by(Video.transformed_at)
                .limit(1)
            ).scalar_one_or_none()

            if v is None:
                return {"skipped": "no transformed videos ready"}

            # Daily cap check — count today's published videos
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            todays = db.execute(
                select(Video.id).where(
                    Video.status == VideoStatus.PUBLISHED,
                    Video.published_at >= today_start,
                )
            ).scalars().all()
            todays_count = len(todays)

            if todays_count >= settings.uploads_per_day:
                return {"skipped": f"daily cap {todays_count}/{settings.uploads_per_day}"}

            video_id = v.id

        from tasks.pipeline import publish_task, generate_metadata_for_video
        try:
            generate_metadata_for_video(video_id)
        except Exception as e:
            log.warning("publish_job: metadata gen failed %s: %s", video_id, e)

        result = publish_task(video_id, meta={})
        log.info("publish_job: video=%s result=%s", video_id, result)
        return result

    except Exception as e:
        log.exception("publish_job failure: %s", e)
        return {"error": str(e)}


def cleanup_job() -> dict:
    """Delete source files older than SOURCE_VIDEO_MAX_AGE_DAYS."""
    try:
        max_age = settings.source_video_max_age_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
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
                    except OSError as e:
                        log.warning("cleanup_job: delete failed %s: %s", v.local_path, e)
        log.info("cleanup_job: deleted=%d", deleted)
        return {"deleted": deleted}
    except Exception as e:
        log.exception("cleanup_job failure: %s", e)
        return {"error": str(e)}


def recompute_scores_job() -> dict:
    """Refresh all engagement/velocity scores."""
    try:
        with session_scope() as db:
            rows = db.execute(select(Video)).scalars().all()
            recompute_scores(rows)
            db.commit()
        return {"updated": len(rows)}
    except Exception as e:
        log.exception("recompute_scores_job failure: %s", e)
        return {"error": str(e)}


# ---------- Scheduler lifecycle ----------

def _make_scheduler() -> BackgroundScheduler:
    """Build a configured BackgroundScheduler (does NOT start it)."""
    executors = {
        "default": ThreadPoolExecutor(max_workers=8),
    }
    sched = BackgroundScheduler(
        timezone="UTC",
        executors=executors,
    )

    now = datetime.now(timezone.utc)

    sched.add_job(
        scrape_job,
        IntervalTrigger(hours=settings.scrape_interval_hours),
        id="scrape",
        next_run_time=now + timedelta(seconds=10),
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        discovery_job,
        IntervalTrigger(hours=settings.discovery_interval_hours),
        id="discovery",
        next_run_time=now + timedelta(minutes=2),
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        transform_job,
        IntervalTrigger(hours=1),
        id="transform",
        next_run_time=now + timedelta(minutes=3),
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        publish_job,
        IntervalTrigger(hours=settings.upload_interval_hours),
        id="publish",
        next_run_time=now + timedelta(minutes=5),
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        cleanup_job,
        IntervalTrigger(hours=24),
        id="cleanup",
        next_run_time=now + timedelta(hours=1),
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    sched.add_job(
        recompute_scores_job,
        IntervalTrigger(hours=1),
        id="recompute",
        next_run_time=now + timedelta(minutes=4),
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return sched


def start_scheduler() -> BackgroundScheduler:
    """Start the APScheduler BackgroundScheduler. Idempotent and restart-safe."""
    global _scheduler

    # Shut down any previous run if it's still alive (module-reload safe)
    if _scheduler is not None and _scheduler.running:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None

    init_db()

    sched = _make_scheduler()
    sched.start()
    _scheduler = sched

    log.info(
        "Scheduler started: scrape=%dh discovery=%dh transform=1h publish=%dh cleanup=24h recompute=1h",
        settings.scrape_interval_hours,
        settings.discovery_interval_hours,
        settings.upload_interval_hours,
    )
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            log.warning("Scheduler shutdown error: %s", e)
        _scheduler = None
        log.info("Scheduler stopped")