"""Autonomous scheduler using APScheduler.

Replaces Celery+Redis for local mode. Runs inside the FastAPI process.
Uses BackgroundScheduler from the REAL apscheduler package (not a local stub).

Scheduled:
  - scrape      : every SCRAPE_INTERVAL_HOURS (default 6h, first run ~10s after startup)
  - discovery   : every DISCOVERY_INTERVAL_HOURS (default 24h)
  - transform   : every 1h — auto-transforms APPROVED videos
  - publish     : every UPLOAD_INTERVAL_HOURS (default 3h; 24 / uploads_per_day)
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
from sqlalchemy import desc, func, or_, select

from .logging_config import last_run_times, record_job_run
from .config import settings
from .db import session_scope, init_db
from .models import ClipJob, Creator, Video, VideoStatus
from . import discovery, quota
from .analytics import recompute_scores

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


# ---------- Job instrumentation ----------
def instrumented(job_id: str, fn: "callable"):
    """Wrap a job so every run is timed, logged and recorded.

    The previous version of this built a ``_JOBS`` registry and a ``_run_job``
    wrapper that ``_make_scheduler`` never used -- it registered the raw
    functions instead. The wrapper was dead code, so no job history was ever
    written. Anything scheduled must go through here.
    """
    import functools
    import time

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.monotonic()
        log.info("job[%s] start", job_id)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            elapsed = time.monotonic() - started
            log.exception("job[%s] crashed after %.1fs", job_id, elapsed)
            record_job_run(job_id, None, duration_sec=elapsed, ok=False,
                           error=str(exc))
            return {"error": str(exc)}
        elapsed = time.monotonic() - started
        # A job that returns {"error": ...} failed without raising.
        ok = not (isinstance(result, dict) and result.get("error"))
        log.info("job[%s] done in %.1fs -> %s", job_id, elapsed, result)
        record_job_run(job_id, result, duration_sec=elapsed, ok=ok,
                       error=(result or {}).get("error") if isinstance(result, dict) else None)
        return result

    return wrapper


# ---------- Job impls ----------

def _scraper_pending(db) -> int:
    """Pending rows the scraper is responsible for. Finished clips are not its backlog."""
    return db.scalar(
        select(func.count(Video.id)).where(
            Video.status == VideoStatus.PENDING,
            Video.meta["clip_rendered"].as_boolean().isnot(True),
        )
    ) or 0


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
                pending = _scraper_pending(db)
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
            final_pending = _scraper_pending(db)
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
                    # Clips arrive already rendered, and transform_task only marks them ready.
                    or_(Video.transformed_path.is_(None), Video.meta["clip_rendered"].as_boolean().is_(True)),
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
    """Approve pending videos that clear the quality and virality bars.

    Videos are held for ``AUTO_APPROVE_VETO_HOURS`` after being scraped before
    they become eligible, which is the window a human has to reject something
    the filters would otherwise wave through. Set the veto window to 0 to
    approve immediately.
    """
    if not settings.auto_approve:
        return {"skipped": "auto_approve disabled"}

    from tasks.pipeline import (
        channel_focus_reason, content_safety_reason, _item_view, _probe_quality,
    )

    approved = 0
    reasons: dict[str, int] = {}

    def note(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    veto_cutoff = datetime.now(timezone.utc) - timedelta(
        hours=max(0, settings.auto_approve_veto_hours)
    )

    try:
        with session_scope() as db:
            rows = db.execute(
                select(Video)
                .where(Video.status == VideoStatus.PENDING)
                .order_by(desc(Video.viral_score), desc(Video.reach_score))
                .limit(40)
            ).scalars().all()

            for video in rows:
                scraped_at = video.scraped_at
                if scraped_at is not None and scraped_at.tzinfo is None:
                    scraped_at = scraped_at.replace(tzinfo=timezone.utc)
                if scraped_at is not None and scraped_at > veto_cutoff:
                    note("inside veto window")
                    continue

                # Re-check safety and channel focus *before* touching the disk.
                # Both may have changed since this video was scraped, and
                # unattended approval is the last gate before an upload goes
                # out. A disqualified video is rejected whether or not its file
                # is still present -- otherwise it sits pending forever.
                view = _item_view(video)
                disqualified = content_safety_reason(view) or channel_focus_reason(view)
                if disqualified:
                    video.status = VideoStatus.REJECTED
                    video.curated_at = datetime.now(timezone.utc)
                    video.meta = {
                        **(video.meta or {}),
                        "auto_rejected": disqualified,
                    }
                    note(disqualified.split(":")[0])
                    continue

                # Clips have no views to judge, and CLIP_AUTO_QUEUE is the switch for skipping review.
                if (video.meta or {}).get("clip_rendered"):
                    note("clip awaits review")
                    continue

                if not video.local_path or not os.path.exists(video.local_path):
                    note("no local file")
                    continue

                height = video.height
                if not height:
                    height = _probe_quality(video.local_path, source=video.source)["height"]
                if height and height < settings.min_source_height:
                    note("resolution too low")
                    continue

                if video.views and video.views < settings.viral_min_views:
                    note("below reach floor")
                    continue

                if video.viral_score < settings.auto_approve_min_viral_score:
                    note("below viral score")
                    continue

                video.status = VideoStatus.APPROVED
                video.curated_at = datetime.now(timezone.utc)
                video.meta = {
                    **(video.meta or {}),
                    "auto_approved": True,
                    "auto_viral_score": video.viral_score,
                }
                approved += 1

        log.info("auto_approve_job: approved=%d reasons=%s", approved, reasons or {})
        return {"approved": approved, "reasons": reasons}
    except Exception as exc:
        log.exception("auto_approve_job failure: %s", exc)
        return {"error": str(exc), "approved": approved}


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


def clip_queue_job() -> dict:
    """Cut any queued long videos into clips."""
    from .clipper.job import clip_queue_job as run_queue

    return run_queue()


def _recover_stuck_publishes() -> None:
    try:
        from tasks.pipeline import recover_stuck_publishes
        recover_stuck_publishes()
    except Exception as exc:
        log.warning("publish recovery failed: %s", exc)


def publish_job() -> dict:
    """Auto-publish next TRANSFORMED video with jittered pacing (anti-spam)."""
    if not settings.enable_publish:
        return {"skipped": "publishing disabled (enable_publish=false)"}
    if settings.growth_enabled:
        return {"skipped": "the growth loop owns publishing"}
    # A claim is held for hours and the startup sweep has passed by then. This is the only later look with the loop off.
    _recover_stuck_publishes()
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

            ready = db.execute(
                select(Video)
                .where(Video.status == VideoStatus.TRANSFORMED)
                .order_by(Video.transformed_at)
            ).scalars().all()

            if not ready:
                return {"skipped": "no transformed videos ready"}
            from tasks.pipeline import least_failed
            v = least_failed(ready)[0]

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


# Files younger than this are never treated as orphans -- a download in flight
# has no database row yet.
_ORPHAN_GRACE = timedelta(hours=6)


def _content_files() -> list[str]:
    """Every media file under the content directory, including transforms."""
    import glob

    root = str(settings.content_path)
    return [
        path for pattern in ("*.mp4", "transformed/*.mp4", "clips/*/*.mp4")
        for path in glob.glob(os.path.join(root, pattern))
        # Leading underscore marks internal scaffolding such as the mock
        # master clip, which has no database row by design.
        if not os.path.basename(path).startswith("_")
    ]


_TRANSCRIPT_SUFFIX = ".transcript.json"


def _source_cache_files() -> list[str]:
    """Clipper downloads, stock footage and transcripts. No row references these, so orphan rules do not fit."""
    import glob

    root = str(settings.content_path)
    return [
        path for pattern in ("long/*", "broll/*", f"*{_TRANSCRIPT_SUFFIX}", "transcripts/*.json")
        for path in glob.glob(os.path.join(root, pattern))
        if os.path.isfile(path)
    ]


def _remove(path: str) -> int:
    """Delete a file, returning the bytes reclaimed (0 on failure)."""
    try:
        size = os.path.getsize(path)
        os.remove(path)
        return size
    except OSError as exc:
        log.warning("cleanup: could not delete %s: %s", path, exc)
        return 0


def cleanup_job(dry_run: bool = False) -> dict:
    """Reclaim disk from finished videos and orphaned files.

    Three distinct leaks, only the first of which the original handled:

    1. Source files of PUBLISHED/REJECTED videos past the age limit.
    2. Transformed outputs of PUBLISHED videos -- they were never deleted at
       all, and they are the larger files.
    3. Files on disk that no database row references. A download that failed
       its quality gate, or a row deleted later, leaves bytes behind that a
       row-driven cleanup can never see.
    """
    try:
        def drop(path: str) -> int:
            if dry_run:
                try:
                    return os.path.getsize(path)
                except OSError:
                    return 0
            return _remove(path)

        max_age_days = settings.source_video_max_age_days
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max_age_days)
        reclaimed = 0
        removed_sources = removed_transforms = removed_orphans = 0
        referenced: set[str] = set()

        with session_scope() as db:
            rows = db.execute(select(Video)).scalars().all()

            for video in rows:
                curated = video.published_at or video.curated_at or video.scraped_at
                if curated is not None and curated.tzinfo is None:
                    curated = curated.replace(tzinfo=timezone.utc)
                expired = curated is not None and curated < cutoff
                finished = video.status in (VideoStatus.PUBLISHED, VideoStatus.REJECTED)

                if expired and finished and video.local_path:
                    if os.path.exists(video.local_path):
                        reclaimed += drop(video.local_path)
                        removed_sources += 1
                    if not dry_run:
                        video.local_path = None

                # A published video no longer needs its rendered output either.
                if (expired and video.status == VideoStatus.PUBLISHED
                        and video.transformed_path):
                    if os.path.exists(video.transformed_path):
                        reclaimed += drop(video.transformed_path)
                        removed_transforms += 1
                    if not dry_run:
                        video.transformed_path = None

                for path in (video.local_path, video.transformed_path):
                    if path:
                        referenced.add(os.path.normcase(os.path.abspath(path)))

        # Orphans: on disk, referenced by nothing, old enough to be safe.
        for path in _content_files():
            key = os.path.normcase(os.path.abspath(path))
            if key in referenced:
                continue
            try:
                modified = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
            except OSError:
                continue
            if now - modified < _ORPHAN_GRACE:
                continue
            reclaimed += drop(path)
            removed_orphans += 1

        # Downloads expire like other sources: a 6h limit would throw away a paid
        # transcript and could delete a file mid-render. They carry no job id, so
        # leave them all alone while any clip job is waiting or running.
        removed_cache = 0
        with session_scope() as db:
            clip_jobs_active = db.scalar(
                select(func.count(ClipJob.id)).where(ClipJob.status.in_(("queued", "running")))
            )
        if not clip_jobs_active:
            cache_age = max(timedelta(days=max_age_days), _ORPHAN_GRACE)
            files = _source_cache_files()
            gone: set[str] = set()

            def too_old(path: str) -> bool:
                try:
                    return now - datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc) >= cache_age
                except OSError:
                    return False

            for path in files:
                if not path.endswith(_TRANSCRIPT_SUFFIX) and too_old(path):
                    reclaimed += drop(path)
                    removed_cache += 1
                    gone.add(path)
            # A sidecar transcript goes with its video, and is kept for as long as the video is.
            for path in files:
                if path.endswith(_TRANSCRIPT_SUFFIX):
                    video = path[:-len(_TRANSCRIPT_SUFFIX)]
                    if video in gone or not os.path.exists(video):
                        reclaimed += drop(path)
                        removed_cache += 1

        result = {
            "sources_deleted": removed_sources,
            "transforms_deleted": removed_transforms,
            "orphans_deleted": removed_orphans,
            "cache_deleted": removed_cache,
            "mb_reclaimed": round(reclaimed / 1e6, 1),
            "dry_run": dry_run,
        }
        log.info("cleanup_job: %s", result)
        return result
    except Exception as exc:
        log.exception("cleanup_job failure: %s", exc)
        return {"error": str(exc)}


def storage_report() -> dict:
    """Current disk usage under the content directory."""
    referenced: set[str] = set()
    with session_scope() as db:
        for video in db.execute(select(Video)).scalars().all():
            for path in (video.local_path, video.transformed_path):
                if path:
                    referenced.add(os.path.normcase(os.path.abspath(path)))

    total = orphan_bytes = 0
    files = orphans = 0
    for path in _content_files():
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        total += size
        files += 1
        if os.path.normcase(os.path.abspath(path)) not in referenced:
            orphan_bytes += size
            orphans += 1
    for path in _source_cache_files():
        try:
            total += os.path.getsize(path)
            files += 1
        except OSError:
            continue
    return {
        "files": files,
        "total_mb": round(total / 1e6, 1),
        "orphan_files": orphans,
        "orphan_mb": round(orphan_bytes / 1e6, 1),
        "content_dir": str(settings.content_path),
    }


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


def growth_autopilot_job() -> dict:
    from .growth.autopilot import autopilot_job
    return autopilot_job()


def growth_collect_job() -> dict:
    from .growth.collect import collect_job
    return collect_job()


def growth_learn_job() -> dict:
    from .growth.learn import learn_job
    return learn_job()


def growth_notify_job() -> dict:
    from .growth.notify import notify_job
    return notify_job()


def growth_supply_job() -> dict:
    from .growth.supply import supply_job
    return supply_job()


# ---------- Job registry ----------
# The single source of truth for job ids. Both _make_scheduler() and the
# POST /scheduler/run/{job_id} endpoint read this, so the two can never drift.
JOB_FUNCTIONS: dict[str, "callable"] = {
    "scrape": scrape_job,
    "discovery": discovery_job,
    "auto_approve": auto_approve_job,
    "transform": transform_job,
    "publish": publish_job,
    "cleanup": cleanup_job,
    "clip_queue": clip_queue_job,
    "recompute": recompute_scores_job,
    "analytics_pull": analytics_pull_job,
    "growth_autopilot": growth_autopilot_job,
    "growth_collect": growth_collect_job,
    "growth_learn": growth_learn_job,
    "growth_supply": growth_supply_job,
    "growth_notify": growth_notify_job,
}


def run_job(job_id: str) -> dict:
    """Run a registered job by id. Raises KeyError for an unknown id.

    Manual runs are instrumented exactly like scheduled ones, so the history
    shows everything that touched the pipeline, not just timer-driven work.
    """
    fn = JOB_FUNCTIONS[job_id]
    return instrumented(job_id, fn)()


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
                target.add_job(instrumented("publish", publish_job), IntervalTrigger(hours=interval_h), id="publish", next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5), max_instances=1, coalesce=True, replace_existing=True)
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
        job_defaults={"misfire_grace_time": 6 * 3600, "coalesce": True},
    )

    now = datetime.now(timezone.utc)

    # The growth loop posts finished clips only, so scraped stock would just fill the disk.
    if not settings.growth_enabled:
        sched.add_job(instrumented("scrape", scrape_job), IntervalTrigger(hours=settings.scrape_interval_hours), id="scrape", next_run_time=now + timedelta(seconds=10), max_instances=1, coalesce=True, replace_existing=True)
    if settings.auto_discovery:
        sched.add_job(instrumented("discovery", discovery_job), IntervalTrigger(hours=settings.discovery_interval_hours), id="discovery", next_run_time=now + timedelta(minutes=2), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(instrumented("auto_approve", auto_approve_job), IntervalTrigger(minutes=15), id="auto_approve", next_run_time=now + timedelta(minutes=1), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(instrumented("transform", transform_job), IntervalTrigger(hours=1), id="transform", next_run_time=now + timedelta(minutes=3), max_instances=1, coalesce=True, replace_existing=True)
    if settings.enable_publish:
        interval_h = max(1, 24 / max(1, settings.uploads_per_day))
        sched.add_job(instrumented("publish", publish_job), IntervalTrigger(hours=interval_h), id="publish", next_run_time=now + timedelta(minutes=5), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(instrumented("cleanup", cleanup_job), IntervalTrigger(hours=24), id="cleanup", next_run_time=now + timedelta(hours=1), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(instrumented("clip_queue", clip_queue_job), IntervalTrigger(minutes=5), id="clip_queue", next_run_time=now + timedelta(minutes=1), max_instances=1, coalesce=True, replace_existing=True)
    sched.add_job(instrumented("recompute", recompute_scores_job), IntervalTrigger(hours=1), id="recompute", next_run_time=now + timedelta(minutes=4), max_instances=1, coalesce=True, replace_existing=True)
    if settings.growth_enabled:
        for job_id, every, first in (("growth_autopilot", timedelta(minutes=10), timedelta(minutes=2)),
                                     ("growth_collect", timedelta(hours=3), timedelta(minutes=6)),
                                     ("growth_learn", timedelta(hours=6), timedelta(minutes=8)),
                                     ("growth_supply", timedelta(hours=1), timedelta(minutes=1)),
                                     ("growth_notify", timedelta(minutes=10), timedelta(minutes=3))):
            sched.add_job(instrumented(job_id, JOB_FUNCTIONS[job_id]), IntervalTrigger(seconds=int(every.total_seconds())), id=job_id, next_run_time=now + first, max_instances=1, coalesce=True, replace_existing=True)
    else:
        sched.add_job(instrumented("analytics_pull", analytics_pull_job), IntervalTrigger(hours=6), id="analytics_pull", next_run_time=now + timedelta(minutes=10), max_instances=1, coalesce=True, replace_existing=True)
    return sched


# How long after its due time a job may still be run on catch-up. Beyond this
# the work is stale enough that running it adds nothing.
_CATCHUP_MAX_LATE = timedelta(hours=48)

# Nominal interval per job id, used only to decide what is overdue.
def _job_intervals() -> dict[str, timedelta]:
    return {
        "scrape": timedelta(hours=settings.scrape_interval_hours),
        "discovery": timedelta(hours=settings.discovery_interval_hours),
        "auto_approve": timedelta(minutes=15),
        "transform": timedelta(hours=1),
        "publish": timedelta(hours=max(1, 24 / max(1, settings.uploads_per_day))),
        "cleanup": timedelta(hours=24),
        "clip_queue": timedelta(minutes=5),
        "recompute": timedelta(hours=1),
        "analytics_pull": timedelta(hours=6),
        "growth_autopilot": timedelta(minutes=10),
        "growth_collect": timedelta(hours=3),
        "growth_learn": timedelta(hours=6),
        "growth_supply": timedelta(hours=1),
        "growth_notify": timedelta(minutes=10),
    }


def overdue_jobs(now: datetime | None = None) -> list[str]:
    """Jobs whose last successful run is older than their interval.

    APScheduler drops a fire it missed by more than ``misfire_grace_time``
    rather than running it late, so a laptop that slept for ten hours simply
    skips that work. This reads the run history and reports what to catch up.
    """
    now = now or datetime.now(timezone.utc)
    history = last_run_times()
    stale: list[str] = []
    for job_id, interval in _job_intervals().items():
        if job_id not in JOB_FUNCTIONS:
            continue
        # Only catch up on what this mode schedules.
        if (job_id.startswith("growth_") and not settings.growth_enabled) or (
                job_id in ("scrape", "analytics_pull") and settings.growth_enabled):
            continue
        last = history.get(job_id)
        if last is None:
            continue  # never run: the normal first-run schedule covers it
        age = now - last
        if interval < age <= _CATCHUP_MAX_LATE:
            stale.append(job_id)
    return stale


def schedule_catchup(sched: BackgroundScheduler) -> list[str]:
    """Queue overdue jobs shortly after startup, staggered."""
    stale = overdue_jobs()
    for index, job_id in enumerate(stale):
        run_at = datetime.now(timezone.utc) + timedelta(seconds=45 + index * 90)
        sched.add_job(
            instrumented(job_id, JOB_FUNCTIONS[job_id]),
            "date", run_date=run_at, id=f"catchup_{job_id}",
            max_instances=1, replace_existing=True,
        )
    if stale:
        log.info("catch-up scheduled for overdue jobs: %s", ", ".join(stale))
    return stale


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

    # Rows a crash left mid-upload get settled before anything can post again.
    _recover_stuck_publishes()

    sched = _make_scheduler()
    sched.start()
    _scheduler = sched

    # Anything the machine slept through gets picked up now.
    try:
        schedule_catchup(sched)
    except Exception as exc:
        log.warning("catch-up scheduling failed: %s", exc)

    interval = 24 / max(1, settings.uploads_per_day) if settings.enable_publish else 0
    log.info("Scheduler started: scrape=%dh discovery=%s auto_approve=%s transform=1h publish=%.1fh±%dm (%d/day jitter) cleanup=24h recompute=1h analytics=6h (enable_publish=%s auto_discovery=%s)", settings.scrape_interval_hours, "auto" if settings.auto_discovery else "manual", "on" if settings.auto_approve else "off", interval, settings.upload_jitter_minutes, settings.uploads_per_day, settings.enable_publish, settings.auto_discovery)
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