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
from sqlalchemy import desc, func, select

from .logging_config import log_job_execution
from .config import settings
from .db import session_scope, init_db
from .models import Creator, Video, VideoStatus
from . import discovery, quota
from .analytics import recompute_scores

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


# ---------- Job wrappers ----------

_JOBS: dict[str, callable] = {}

def _register(job_id: str, fn: callable):
    _JOBS[job_id] = fn
    return fn


def _run_job(job_id: str):
    """Wrap a job to log its result."""
    def wrapper():
        log.info("job[%s] started", job_id)
        result = _JOBS[job_id]()
        log_job_execution(job_id, result)
        return result
    return wrapper


# ---------- Job impls ----------

def scrape_job(handles: list[str] | None = None,
               hashtags: list[str] | None = None,
               max_per_run: int | None = None) -> dict:
    """Run a scrape cycle until 20 pending videos are queued for review."""
    from tasks.pipeline import scrape_task
    target = getattr(settings, "scrape_target_pending", 20)
    max_attempts = 5
    total_created = 0
    total_skipped = 0
    total_fetched = 0
    attempts = 0
    log.info("scrape_job: handles=%s hashtags=%s max=%s target_pending=%s", handles, hashtags, max_per_run, target)
    try:
        while attempts < max_attempts:
            with session_scope() as db:
                pending = db.scalar(select(func.count(Video.id)).where(Video.status == VideoStatus.PENDING)) or 0
                if pending >= target:
                    log.info("scrape_job: target reached %s/%s pending, stopping", pending, target)
                    break
                need = target - pending
                batch_max = max_per_run or settings.scrape_max_per_run
                batch_max = min(batch_max, max(need + 5, need * 2))
            attempts += 1
            log.info("scrape_job: attempt %s/%s need=%s batch_max=%s", attempts, max_attempts, need, batch_max)
            result = scrape_task(handles=handles, hashtags=hashtags, max_per_run=batch_max)
            total_created += result.get("created", 0)
            total_skipped += result.get("skipped", 0)
            total_fetched += result.get("fetched", 0)
            log.info("scrape_job: attempt %s done created=%s total_created=%s", attempts, result.get("created"), total_created)
            if result.get("created", 0) == 0 and result.get("fetched", 0) == 0:
                log.warning("scrape_job: no progress on attempt %s, breaking to avoid infinite loop", attempts)
                break
            import time as _t
            _t.sleep(1)
        with session_scope() as db:
            final_pending = db.scalar(select(func.count(Video.id)).where(Video.status == VideoStatus.PENDING)) or 0
        log.info("scrape_job: finished attempts=%s created=%s pending=%s/%s", attempts, total_created, final_pending, target)
        return {"created": total_created, "skipped": total_skipped, "fetched": total_fetched, "attempts": attempts, "pending": final_pending, "target": target}
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


def auto_approve_job() -> dict:
    """Autonomously approve pending videos that pass viral/quality gate. Human can still reject within veto window."""
    if not settings.auto_approve:
        return {"skipped": "auto_approve disabled"}
    approved = 0
    skipped: list[str] = []
    try:
        from tasks.pipeline import _is_viral_candidate, _probe_quality
        from app.analytics import engagement_score, velocity_score
        with session_scope() as db:
            rows = db.execute(
                select(Video).where(Video.status == VideoStatus.PENDING).order_by(desc(Video.velocity_score), desc(Video.engagement_score)).limit(20)
            ).scalars().all()
            for v in rows:
                if not v.local_path or not os.path.exists(v.local_path):
                    continue
                q = _probe_quality(v.local_path, source=v.source)
                if q["height"] < 360:
                    continue
                if v.source == "snapchat" and not (3 <= (v.duration_sec or 0) <= 59):
                    continue
                eng = engagement_score(v)
                vel = velocity_score(v)
                from app.scrapers.base import ScrapedItem
                tmp = ScrapedItem(source=v.source, source_id=v.source_id, source_url=v.source_url, views=v.views, likes=v.likes, comments=v.comments, shares=v.shares, duration_sec=v.duration_sec or 0)
                if not _is_viral_candidate(tmp, eng, vel):
                    continue
                # 5-35s preferred for Shorts virality, but 3-59 allowed
                if not (settings.viral_preferred_min_duration <= (v.duration_sec or 0) <= settings.viral_preferred_max_duration):
                    # allow but don't boost
                    pass
                v.status = VideoStatus.APPROVED
                v.curated_at = datetime.now(timezone.utc)
                v.meta = {**(v.meta or {}), "auto_approved": True, "auto_eng": eng, "auto_vel": vel}
                approved += 1
        log.info("auto_approve_job: approved=%d", approved)
        return {"approved": approved, "skipped": skipped}
    except Exception as e:
        log.exception("auto_approve_job failure: %s", e)
        return {"error": str(e), "approved": approved}


def analytics_pull_job() -> dict:
    """Pull YouTube stats for published videos and boost creators (growth loop)."""
    try:
        from app.publishers.youtube_publisher import YouTubePublisher
        from app.db import session_scope as _ss
        from sqlalchemy import select as _sel
        pub = YouTubePublisher()
        # This will fail gracefully if no token; we just log
        updated = 0
        with _ss() as db:
            vids = db.execute(_sel(Video).where(Video.status == VideoStatus.PUBLISHED, Video.youtube_video_id.isnot(None)).limit(50)).scalars().all()
            ids = [v.youtube_video_id for v in vids if v.youtube_video_id and not v.youtube_video_id.startswith("MOCK_")]
            if not ids:
                return {"skipped": "no published youtube ids"}
            # Try analytics pull (best effort)
            try:
                stats = pub.fetch_stats(ids)  # may not exist, fallback to boost
            except Exception as e:
                log.warning("analytics fetch failed: %s", e)
                stats = {}
            for v in vids:
                info = stats.get(v.youtube_video_id, {})
                if info:
                    v.views = info.get("viewCount", v.views)
                    v.likes = info.get("likeCount", v.likes)
                    v.comments = info.get("commentCount", v.comments)
                    recompute_scores([v])
                    updated += 1
                # boost creator regardless of stats (published = success)
                if v.author:
                    try:
                        discovery.boost_creator(db, v.source, v.author, amount=1.0)
                    except Exception:
                        pass
        return {"updated": updated}
    except Exception as e:
        log.exception("analytics_pull_job failure: %s", e)
        return {"error": str(e)}


def publish_job() -> dict:
    """Auto-publish next TRANSFORMED video with jittered pacing (anti-spam)."""
    if not settings.enable_publish:
        return {"skipped": "publishing disabled (enable_publish=false)"}
    if not settings.auto_publish:
        return {"skipped": "auto_publish disabled"}
    import random
    jitter = getattr(settings, "upload_jitter_minutes", 45)
    delay = random.uniform(-jitter, jitter) * 60
    if abs(delay) > 5:
        log.info("publish_job: jitter %.0fs (jitter ±%dm) — soft-cap spread, not bursting", delay, jitter)
        import time as _time
        _time.sleep(min(abs(delay), 5))

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

            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            todays = db.execute(select(Video.id).where(Video.status == VideoStatus.PUBLISHED, Video.published_at >= today_start)).scalars().all()
            todays_count = len(todays)
            if todays_count >= settings.uploads_per_day:
                return {"skipped": f"daily soft-cap {todays_count}/{settings.uploads_per_day} (will spread to next 3h slot)"}

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

def reschedule_publish(sched: BackgroundScheduler | None = None) -> dict:
    target = _scheduler or sched
    if target is None:
        return {"error": "scheduler not running"}
    try:
        interval_h = max(1, 24 / max(1, settings.uploads_per_day))
        job = target.get_job("publish")
        if job:
            job.reschedule(trigger=IntervalTrigger(hours=interval_h))
            return {"rescheduled": True, "interval_hours": interval_h}
        else:
            # add if missing and publish enabled
            if settings.enable_publish:
                target.add_job(publish_job, IntervalTrigger(hours=interval_h), id="publish", next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5), max_instances=1, coalesce=True, replace_existing=True)
                return {"added": True, "interval_hours": interval_h}
        return {"skipped": "no publish job"}
    except Exception as e:
        return {"error": str(e)}


def _make_scheduler() -> BackgroundScheduler:
    """Build a configured BackgroundScheduler (does NOT start it)."""
    executors = {
        "default": ThreadPoolExecutor(max_workers=8),
    }
    sched = BackgroundScheduler(
        timezone="UTC",
        executors=executors,
        job_defaults={"misfire_grace_time": 3600, "coalesce": True},
    )

    now = datetime.now(timezone.utc)

    sched.add_job(scrape_job, IntervalTrigger(hours=settings.scrape_interval_hours), id="scrape", next_run_time=now + timedelta(seconds=10), max_instances=1, coalesce=True, replace_existing=True)
    if settings.auto_discovery:
        sched.add_job(discovery_job, IntervalTrigger(hours=settings.discovery_interval_hours), id="discovery", next_run_time=now + timedelta(minutes=2), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(auto_approve_job, IntervalTrigger(minutes=15), id="auto_approve", next_run_time=now + timedelta(minutes=1), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(transform_job, IntervalTrigger(hours=1), id="transform", next_run_time=now + timedelta(minutes=3), max_instances=1, coalesce=True, replace_existing=True)
    if settings.enable_publish:
        interval_h = max(1, 24 / max(1, settings.uploads_per_day))
        sched.add_job(publish_job, IntervalTrigger(hours=interval_h), id="publish", next_run_time=now + timedelta(minutes=5), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(cleanup_job, IntervalTrigger(hours=24), id="cleanup", next_run_time=now + timedelta(hours=1), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(recompute_scores_job, IntervalTrigger(hours=1), id="recompute", next_run_time=now + timedelta(minutes=4), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(analytics_pull_job, IntervalTrigger(hours=6), id="analytics_pull", next_run_time=now + timedelta(minutes=10), max_instances=1, coalesce=True, replace_existing=True)
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

    interval = 24 / max(1, settings.uploads_per_day) if settings.enable_publish else 0
    log.info("Scheduler started: scrape=%dh discovery=%s auto_approve=%s transform=1h publish=%.1fh±%dm (8/day jitter) cleanup=24h recompute=1h analytics=6h (enable_publish=%s auto_discovery=%s)", settings.scrape_interval_hours, "auto" if settings.auto_discovery else "manual", "on" if settings.auto_approve else "off", interval, settings.upload_jitter_minutes, settings.enable_publish, settings.auto_discovery)
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