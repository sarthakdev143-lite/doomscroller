"""Pipeline tasks: scrape → transform → publish.

Each task is a plain Python function. If Celery is available and a broker is
running, `*_async` variants enqueue. Otherwise, call the function directly.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics import recompute_scores
from app.config import settings
from app.db import session_scope
from app.models import Creator, MetadataDraft, TransformJob, Video, VideoStatus
from app.publishers import get_publisher
from app.scrapers import get_scraper
from app.scrapers.base import ScrapedItem
from app.transformers import get_transformer
from app.transformers.ffmpeg_transformer import recipe_from_dict
from app import discovery, quota
from app.metadata import generate_metadata, GeneratedMetadata

log = logging.getLogger(__name__)

# ---------- Platform detection ----------

def _is_snapchat_handle(handle: str) -> bool:
    h = handle.lower()
    if "snapchat.com" in h:
        return True
    # Snapchat usernames typically don't start with @; @xxx on TikTok/IG
    if h.startswith("@"):
        return False
    # Plain username without platform indicator → treat as non-snapchat
    return False


def _split_handles_by_platform(handles: list[str]) -> tuple[list[str], list[str]]:
    """Split handles into (snapchat_handles, other_handles)."""
    snap: list[str] = []
    other: list[str] = []
    for h in handles:
        if _is_snapchat_handle(h):
            snap.append(h)
        else:
            other.append(h)
    return snap, other


# ---------- Download helper ----------

def _download_with_yt_dlp(items: list[ScrapedItem]) -> None:
    """Download any items lacking local_path using YtDlpScraper (in-place)."""
    if settings.use_mock:
        return
    try:
        from app.scrapers.yt_dlp_scraper import YtDlpScraper  # noqa: F401
        downloader = YtDlpScraper()
    except Exception as exc:
        log.warning("yt-dlp downloader unavailable: %s", exc)
        return

    for it in items:
        if it.local_path:
            continue
        try:
            path = downloader._download(it)
            if path:
                it.local_path = path
        except Exception as exc:
            log.warning("yt-dlp download failed for %r: %s", it.source_url, exc)


# ---------- Persistence helper ----------
# Source priority: Reels + Spotlight bubble to the top of the queue.
# Higher value = bigger engagement boost after scoring.
SOURCE_PRIORITY: dict[str, float] = {
    "instagram": 0.30,   # Reels — primary target
    "snapchat": 0.30,    # Spotlight — primary target
    "youtube": 0.10,     # Shorts only — secondary
    "reddit": 0.0,
}


def _is_viral_candidate(it: ScrapedItem, engagement: float, velocity: float) -> bool:
    if it.source == "snapchat":
        if it.views == 0 and it.likes == 0 and it.comments == 0:
            return True
        if it.views > 0 and it.views < 1000:
            return False
        if engagement < settings.viral_min_engagement and velocity < settings.viral_min_velocity:
            return False
        if it.views >= 50000 and 6 <= it.duration_sec <= 12:
            return True
        return True
    return engagement >= settings.viral_min_engagement or velocity >= settings.viral_min_velocity


def persist_scraped_items(items: list[ScrapedItem]) -> tuple[int, int]:
    """Persist scraped items as pending Videos. Returns (created, skipped)."""
    created = 0
    skipped = 0
    with session_scope() as db:
        for it in items:
            if not _is_quality_short(it):
                skipped += 1
                continue
            tmp_video = Video(
                source=it.source, source_id=it.source_id, source_url=it.source_url,
                author=it.author, title=it.title, description=it.description,
                views=it.views, likes=it.likes, comments=it.comments, shares=it.shares,
                duration_sec=it.duration_sec, status=VideoStatus.PENDING,
            )
            from app.analytics import engagement_score as _eng, velocity_score as _vel
            eng = _eng(tmp_video)
            vel = _vel(tmp_video)
            if not _is_viral_candidate(it, eng, vel):
                skipped += 1
                continue
            existing = db.execute(
                select(Video).where(Video.source == it.source, Video.source_id == it.source_id)
            ).scalar_one_or_none()
            if existing:
                skipped += 1
                continue
            v = Video(
                source=it.source,
                source_id=it.source_id,
                source_url=it.source_url,
                author=it.author,
                title=it.title,
                description=it.description,
                thumbnail_url=it.thumbnail_url,
                local_path=it.local_path,
                views=it.views,
                likes=it.likes,
                comments=it.comments,
                shares=it.shares,
                duration_sec=it.duration_sec,
                status=VideoStatus.PENDING,
                meta=it.meta,
            )
            recompute_scores([v])
            # Boost primary sources (Reels, Spotlight) so they surface first
            priority = SOURCE_PRIORITY.get(v.source, 0.0)
            v.engagement_score = min(1.0, v.engagement_score + priority)
            db.add(v)
            created += 1
    return created, skipped


# ---------- Scrape ----------
def scrape_task(handles: list[str] | None = None,
                hashtags: list[str] | None = None,
                max_per_run: int | None = None) -> dict:
    """Snapchat-only scrape: Spotlight + handles via SnapchatScraper."""
    handles = handles if handles is not None else settings.handles_list
    hashtags = hashtags if hashtags is not None else settings.hashtags_list
    max_per_run = max_per_run or settings.scrape_max_per_run

    enabled = [p.lower() for p in settings.enabled_platforms_list]
    snap_only = enabled == ["snapchat"]

    with session_scope() as db:
        discovery.ensure_seed_creators(db, handles, platform="snapchat")
        active = discovery.list_active_creators(db, limit=20)
        snap_handles_all = [c.handle for c in active if c.platform == "snapchat"]
        all_handles = list({*handles, *snap_handles_all})

    if snap_only:
        snap_handles, other_handles = (all_handles, [])
    else:
        snap_handles, other_handles = _split_handles_by_platform(all_handles)

    default_scraper = get_scraper()
    try:
        from app.scrapers.snapchat_scraper import SnapchatScraper
        snap_scraper = SnapchatScraper()
    except Exception:
        snap_scraper = None
    items: list[ScrapedItem] = []

    # 1. Scrape non-Snapchat handles (disabled in snapchat-only MVP)
    if other_handles and not snap_only and not settings.use_mock:
        per = max(1, max_per_run // max(len(snap_handles) + len(other_handles), 1))
        items.extend(default_scraper.scrape_handles(other_handles, per * len(other_handles), download=True))
    elif other_handles and not snap_only:
        items.extend(default_scraper.scrape_handles(other_handles, max_per_run, download=False))

    # 2. Scrape Snapchat handles (enumerate URLs only; download step below)
    if snap_handles and snap_scraper is not None:
        denom = max(len(snap_handles) + (len(other_handles) if not snap_only else 0), 1)
        per = max(1, max_per_run // denom)
        snap_items = snap_scraper.scrape_handles(snap_handles, per * len(snap_handles), download=False)
        items.extend(snap_items)

    # 3. Hashtags
    if hashtags and snap_scraper is not None:
        per_snap = max(1, max_per_run // max(len(hashtags), 1))
        if snap_only:
            per_snap = max(1, max_per_run // max(len(hashtags), 1))
        items.extend(
            snap_scraper.scrape_hashtags(
                hashtags, per_snap * len(hashtags), download=False
            )
        )
    # 3b. Default hashtags (disabled in MVP)
    if hashtags and not snap_only:
        per = max(1, max_per_run // max(len(snap_handles) + len(other_handles) + len(hashtags), 1))
        items.extend(default_scraper.scrape_hashtags(hashtags, per * len(hashtags), download=not settings.use_mock))

    # 4a. Snapchat CDN (yt-dlp doesn't understand bolt-gcdn / cf-st URLs)
    if snap_scraper is not None and not settings.use_mock:
        snap_scraper._download_snapchat_cdn(items)
    # 4b. Remaining downloads (disabled in snapchat-only MVP unless fallback needed)
    if not settings.use_mock and not snap_only:
        _download_with_yt_dlp(items)
    elif not settings.use_mock and snap_only:
        snap_items_only = [it for it in items if it.source == "snapchat" and not it.local_path]
        _download_with_yt_dlp(snap_items_only)

    # Sort by viral potential before persist so top hooks win when capped
    if items:
        from app.analytics import engagement_score as _eng2, velocity_score as _vel2
        def _viral_sort(it: ScrapedItem) -> float:
            tmp = Video(source=it.source, source_id=it.source_id, source_url=it.source_url,
                        views=it.views, likes=it.likes, comments=it.comments, shares=it.shares,
                        duration_sec=it.duration_sec)
            return _vel2(tmp) * 0.7 + _eng2(tmp) * 0.3
        items = sorted(items, key=_viral_sort, reverse=True)[:max_per_run * 2]

    created, skipped = persist_scraped_items(items)
    return {"created": created, "skipped": skipped, "fetched": len(items)}


# ---------- Metadata generation ----------
def generate_metadata_for_video(video_id: str) -> dict:
    """Generate or refresh LLM metadata for a video, store as MetadataDraft."""
    with session_scope() as db:
        v = db.get(Video, video_id)
        if v is None:
            return {"ok": False, "error": "video not found"}
        md = db.execute(
            select(MetadataDraft).where(MetadataDraft.video_id == video_id)
        ).scalar_one_or_none()
        if md is not None and not md.edited:
            # Regenerate
            db.delete(md)
            db.flush()
        source_title = v.title
        source_desc = v.description or ""
        author = v.author
        platform = v.source

    gen: GeneratedMetadata = generate_metadata(
        source_title=source_title, source_desc=source_desc,
        author=author, platform=platform,
    )
    with session_scope() as db:
        # Delete any existing draft (we may have deleted above; do again to be safe)
        existing = db.execute(
            select(MetadataDraft).where(MetadataDraft.video_id == video_id)
        ).scalar_one_or_none()
        if existing:
            db.delete(existing)
            db.flush()
        md = MetadataDraft(
            video_id=video_id,
            title=gen.title,
            description=gen.description,
            tags=gen.tags,
            detected_language=gen.detected_language,
        )
        db.add(md)
    return {
        "ok": True, "video_id": video_id,
        "title": gen.title, "description": gen.description[:120],
        "tags": gen.tags, "language": gen.detected_language,
        "source": gen.source,
    }


def _is_already_9x16(path: str) -> bool:
    q = _probe_quality(path, source="snapchat")
    w, h = q["width"], q["height"]
    if w <= 0 or h <= 0:
        return False
    ratio = w / h
    target = 9 / 16
    return abs(ratio - target) < 0.03 and h >= 1280 and w >= 720


# ---------- Transform ----------
def transform_task(video_id: str, recipe_dict: dict | None = None) -> dict:
    """Transform a video into a vertical Shorts-friendly format. Blur only if not 9:16."""
    recipe_dict = dict(recipe_dict or {})
    with session_scope() as db:
        v = db.get(Video, video_id)
        if v is None:
            return {"ok": False, "error": "video not found"}
        if not v.local_path:
            v.local_path = _ensure_mock_input(v)
        v.status = VideoStatus.TRANSFORMING
        job = TransformJob(
            video_id=v.id, recipe=recipe_dict, status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.flush()
        job_id = job.id
        input_path = v.local_path
        out_dir = os.path.join(os.path.dirname(input_path) or ".", "transformed")
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, f"{v.id}.mp4")
        v_id = v.id

    try:
        if settings.use_mock or not _ffmpeg_available():
            import shutil
            shutil.copyfile(input_path, output_path)
        else:
            already_vertical = _is_already_9x16(input_path)
            if already_vertical and not recipe_dict:
                import shutil
                shutil.copyfile(input_path, output_path)
            else:
                if not recipe_dict:
                    recipe_dict["add_blur_background"] = not already_vertical
                    recipe_dict["add_caption"] = False
                transformer = get_transformer()
                transformer.transform(input_path, output_path, recipe_from_dict(recipe_dict))
        with session_scope() as db:
            v = db.get(Video, v_id)
            job = db.get(TransformJob, job_id)
            v.transformed_path = output_path
            v.status = VideoStatus.TRANSFORMED
            v.transformed_at = datetime.now(timezone.utc)
            job.status = "done"
            job.output_path = output_path
            job.finished_at = datetime.now(timezone.utc)
        if settings.use_llm_metadata and settings.enable_publish:
            try:
                generate_metadata_for_video(v_id)
            except Exception as e:
                log.warning("metadata gen failed for %s: %s", v_id, e)
        return {"ok": True, "video_id": v_id, "output_path": output_path}
    except Exception as e:
        log.exception("transform failed")
        with session_scope() as db:
            v = db.get(Video, v_id)
            job = db.get(TransformJob, job_id)
            v.status = VideoStatus.FAILED
            v.error = f"transform: {e}"
            job.status = "failed"
            job.error = str(e)
            job.finished_at = datetime.now(timezone.utc)
        return {"ok": False, "error": str(e)}


def _ffmpeg_available() -> bool:
    import shutil
    return shutil.which("ffmpeg") is not None


def _probe_quality(path: str, source: str = "") -> dict:
    """Return video quality metrics via ffprobe: duration, width, height, bit_rate, size_bytes."""
    import subprocess, os
    result = {
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "bit_rate": 0,
        "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
        "valid": False,
    }
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=duration,width,height,bit_rate",
             "-of", "default=noprint_wrappers=1",
             path],
            capture_output=True, text=True, check=True,
        )
        for line in out.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k == "duration":
                    result["duration"] = float(v)
                elif k == "width":
                    result["width"] = int(v)
                elif k == "height":
                    result["height"] = int(v)
                elif k == "bit_rate":
                    result["bit_rate"] = int(v)

        # Native short-form platforms (Snapchat Spotlight, Instagram Reels)
        # are already vertically encoded and algorithmically filtered upstream.
        # Use looser thresholds so we don't throw away good content.
        if source == "snapchat":
            if (result["duration"] >= 3 and
                result["width"] >= 240 and result["height"] >= 240 and
                result["bit_rate"] >= 100_000 and
                result["size_bytes"] >= 50_000):
                result["valid"] = True
        else:
            # Generic Shorts gate (YouTube, TikTok imports, etc.)
            if (result["duration"] >= 10 and
                result["width"] >= 240 and result["height"] >= 240 and
                result["bit_rate"] >= 300_000 and
                result["size_bytes"] >= 200_000):
                result["valid"] = True
    except Exception:
        pass
    return result


def _is_quality_short(item: ScrapedItem) -> bool:
    """Hard gate: only allow actual watchable Shorts to enter the DB."""
    if not item.local_path or not os.path.exists(item.local_path):
        return False
    q = _probe_quality(item.local_path, source=item.source)
    if not q["valid"]:
        return False
    if item.source == "snapchat":
        if not (3 <= item.duration_sec <= 59):
            return False
        if q.get("width", 0) > q.get("height", 0):
            return False
    else:
        if not (8 <= item.duration_sec <= 59):
            return False
    if q["height"] < 360:
        return False
    return True


# ---------- Long-form to Shorts chopper ----------

def long_to_shorts_task(video_id: str) -> dict:
    """Split one long-form video into multiple Shorts clips.

    Requires ffmpeg on PATH.  Returns a summary dict including the IDs and
    timestamps of each generated clip.
    """
    from app.clips import clips_from_long
    return clips_from_long(video_id)


# ---------- Publish ----------
def publish_task(video_id: str, meta: dict | None = None) -> dict:
    """Publish the transformed video to YouTube. Disabled in MVP when enable_publish is false."""
    if not settings.enable_publish:
        return {"ok": False, "error": "publishing disabled in MVP (enable_publish=false)"}
    meta = meta or {}
    with session_scope() as db:
        v = db.get(Video, video_id)
        if v is None:
            return {"ok": False, "error": "video not found"}
        if not v.transformed_path:
            return {"ok": False, "error": "video not transformed yet"}
        if quota.remaining(db) <= 0:
            return {"ok": False, "error": "daily quota exhausted"}
        v.status = VideoStatus.PUBLISHING
        v_id = v.id
        file_path = v.transformed_path

        # Load metadata draft (LLM-generated or user-edited)
        md = db.execute(
            select(MetadataDraft).where(MetadataDraft.video_id == video_id)
        ).scalar_one_or_none()
        if md:
            title = meta.get("title") or md.title
            description = meta.get("description") or md.description
            tags = meta.get("tags") or md.tags
        else:
            title = meta.get("title") or f"{v.title} #shorts"
            description = meta.get("description") or v.description or ""
            tags = meta.get("tags") or settings.tags_list
        privacy = meta.get("privacy") or settings.youtube_default_privacy

    try:
        publisher = get_publisher()
        result = publisher.upload(
            file_path=file_path,
            title=title, description=description, tags=tags,
            category=settings.youtube_default_category,
            privacy=privacy,
        )
        with session_scope() as db:
            v = db.get(Video, v_id)
            v.youtube_video_id = result.video_id
            v.status = VideoStatus.PUBLISHED
            v.published_at = datetime.now(timezone.utc)
            quota.consume_one(db, note=f"video {v_id}")
            # Boost the source creator
            if v.author:
                discovery.boost_creator(db, v.source, v.author, amount=1.0)
        return {"ok": True, "video_id": v_id, "youtube_id": result.video_id}
    except Exception as e:
        log.exception("publish failed")
        with session_scope() as db:
            v = db.get(Video, v_id)
            v.status = VideoStatus.FAILED
            v.error = f"publish: {e}"
        return {"ok": False, "error": str(e)}


# ---------- Mock input helper ----------
def _ensure_mock_input(v: Video) -> str:
    """Create a tiny black mp4 so transform/publish can run in mock mode."""
    tmp_dir = os.path.join(tempfile.gettempdir(), "doomscroller_inputs")
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, f"{v.id}.mp4")
    if os.path.exists(path):
        return path
    import subprocess, shutil
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"color=c=black:s={settings.transform_width}x{settings.transform_height}:d=3",
             "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
             "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", path],
            check=True, capture_output=True,
        )
    else:
        open(path, "wb").close()
    return path


# ---------- Optional Celery wrappers ----------
try:
    from celery import shared_task  # type: ignore

    scrape_task_async = shared_task(name="tasks.pipeline.scrape_task")(scrape_task)
    transform_task_async = shared_task(name="tasks.pipeline.transform_task")(transform_task)
    publish_task_async = shared_task(name="tasks.pipeline.publish_task")(publish_task)
except Exception:
    # Celery not available — async variants fall back to sync via .delay() shim
    class _SyncDelay:
        def __init__(self, fn):
            self.fn = fn
        def delay(self, *a, **kw):
            return self.fn(*a, **kw)
        def apply(self, *a, **kw):
            class _R:
                def __init__(self, v): self.v = v
                def get(self, **_): return self.v
            return _R(self.fn(*a, **kw))

    scrape_task_async = _SyncDelay(scrape_task)
    transform_task_async = _SyncDelay(transform_task)
    publish_task_async = _SyncDelay(publish_task)
