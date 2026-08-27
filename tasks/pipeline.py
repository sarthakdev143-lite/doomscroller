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

from app import analytics
from app.analytics import recompute_scores
from app.config import settings
from app.db import session_scope
from app.models import Creator, MetadataDraft, TransformJob, Video, VideoStatus
from app.publishers import get_publisher
from app.scrapers import get_scraper
from app.scrapers.base import ScrapedItem
from app.transformers import get_transformer
from app.transformers.ffmpeg_transformer import recipe_from_dict
from app.transformers.variation import plan_variation
from app import discovery, quota
from app.metadata import generate_metadata, GeneratedMetadata

log = logging.getLogger(__name__)

# ---------- Content selection ----------
# Terms that make a clip a poor fit for an automated YouTube Shorts channel --
# either because YouTube restricts them or because they invite a strike. Matched
# against title, description and the platform's own keywords.
_BLOCKED_TERMS = frozenset({
    "nsfw", "onlyfans", "nude", "nudity", "porn", "xxx", "sexual",
    "escort", "fetish", "lingerie", "strip tease", "webcam girl",
    "gore", "graphic violence", "beheading", "suicide", "self harm",
    "drug deal", "cocaine", "meth lab",
    "betting", "gambling", "casino", "1xbet",
    "crypto giveaway", "forex signals", "get rich quick",
    "sensitive content",
})


def content_safety_reason(item: ScrapedItem) -> str | None:
    """Return why an item is unsafe to republish, or ``None`` if it looks fine."""
    parts = [str(item.title or ""), str(item.description or "")]
    parts.extend(str(k) for k in (item.keywords or []))
    parts.extend(str(h) for h in (item.meta or {}).get("hashtags") or [])
    haystack = " ".join(parts).lower()
    for term in _BLOCKED_TERMS:
        if term in haystack:
            return f"blocked term: {term}"
    return None


def _shorts_shape_reason(item: ScrapedItem) -> str | None:
    """Return why an item is the wrong shape for a Short, or ``None``."""
    duration = item.duration_sec or 0
    if not settings.min_source_duration_sec <= duration <= settings.max_source_duration_sec:
        return f"duration: {duration}s out of range"
    # Landscape sources would need heavy reframing to work as a Short.
    if item.width and item.height and not item.is_vertical:
        return f"not vertical: {item.width}x{item.height}"
    return None


def _topic_haystack(item: ScrapedItem) -> str:
    """All the text worth matching a topic against, lowercased."""
    parts = [str(item.title or ""), str(item.description or "")]
    parts.extend(str(k) for k in (item.keywords or []))
    parts.extend(str(h) for h in (item.meta or {}).get("hashtags") or [])
    return " ".join(parts).lower()


def channel_focus_reason(item: ScrapedItem) -> str | None:
    """Return why an item is off-channel, or ``None`` if it fits.

    A channel that posts six languages and no consistent subject gives YouTube
    nothing to recommend against. This is the filter that keeps one identity.
    """
    languages = settings.languages_list
    if languages:
        language = (item.language or "").lower()
        if not language:
            if not settings.allow_unknown_language:
                return "language: unknown"
        elif language not in languages:
            return f"language: {language}"

    haystack = _topic_haystack(item)

    excluded = settings.excluded_topics_list
    for topic in excluded:
        if topic and topic in haystack:
            return f"excluded topic: {topic}"

    required = settings.topics_list
    if required and not any(topic in haystack for topic in required if topic):
        return "topic: no match"

    return None


def _is_viral_candidate(item: ScrapedItem, engagement: float, velocity: float) -> bool:
    """Whether an item clears the virality bar for its source.

    Snapchat Spotlight is already algorithmically filtered upstream, so the bar
    there is about absolute reach; other sources are judged on engagement rate.
    """
    if item.source == "mock":
        return True
    if item.source == "snapchat":
        # Items with no stats at all cannot be judged here; let the quality
        # gate downstream decide once the file is on disk.
        if item.views == 0 and item.likes == 0:
            return True
        return item.views >= settings.viral_min_views
    return (
        engagement >= settings.viral_min_engagement
        or velocity >= settings.viral_min_velocity
    )


def _score_probe(item: ScrapedItem) -> Video:
    """Build a throwaway Video so the analytics helpers can score a raw item."""
    return Video(
        source=item.source, source_id=item.source_id, source_url=item.source_url,
        views=item.views, likes=item.likes, comments=item.comments,
        shares=item.shares, duration_sec=item.duration_sec,
        source_published_at=item.published_at,
        scraped_at=item.published_at or datetime.now(timezone.utc),
    )


def select_candidates(items: list[ScrapedItem], limit: int) -> tuple[list[ScrapedItem], dict]:
    """Filter and rank scraped items *before* spending bandwidth on downloads.

    Snapchat hands us duration, dimensions and engagement up front, so every
    rejection here is a download we never had to make.
    """
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        key = reason.split(":")[0]
        rejected[key] = rejected.get(key, 0) + 1

    survivors: list[tuple[float, ScrapedItem]] = []
    for item in items:
        reason = (
            content_safety_reason(item)
            or channel_focus_reason(item)
            or _shorts_shape_reason(item)
        )
        if reason:
            log.debug("scrape: skip %s (%s)", item.source_id[:12], reason)
            reject(reason)
            continue
        probe = _score_probe(item)
        engagement = analytics.engagement_score(probe)
        velocity = analytics.velocity_score(probe)
        if not _is_viral_candidate(item, engagement, velocity):
            reject("below virality bar")
            continue
        survivors.append((analytics.viral_score(probe), item))

    survivors.sort(key=lambda pair: pair[0], reverse=True)
    chosen = [item for _score, item in survivors[:limit]]
    if len(survivors) > limit:
        rejected["over limit"] = len(survivors) - limit
    return chosen, rejected


# ---------- Persistence ----------
def persist_scraped_items(items: list[ScrapedItem]) -> dict:
    """Persist scraped items as pending Videos.

    Items already known to the database get their engagement stats refreshed
    rather than being dropped -- view counts move fast, and a video we saw
    yesterday may have gone viral since.
    """
    created = skipped = refreshed = 0
    with session_scope() as db:
        for item in items:
            existing = db.execute(
                select(Video).where(
                    Video.source == item.source, Video.source_id == item.source_id
                )
            ).scalar_one_or_none()

            if existing is not None:
                # Refresh volatile counters; never touch curation state.
                existing.views = max(existing.views, item.views)
                existing.likes = max(existing.likes, item.likes)
                existing.comments = max(existing.comments, item.comments)
                existing.shares = max(existing.shares, item.shares)
                if item.local_path and not existing.local_path:
                    existing.local_path = item.local_path
                recompute_scores([existing])
                refreshed += 1
                continue

            if not _is_quality_short(item):
                skipped += 1
                continue

            video = Video(
                source=item.source,
                source_id=item.source_id,
                source_url=item.source_url,
                author=item.author,
                author_name=item.author_name,
                title=item.title,
                description=item.description,
                thumbnail_url=item.thumbnail_url,
                media_url=item.media_url,
                local_path=item.local_path,
                language=item.language,
                keywords=list(item.keywords or []),
                views=item.views,
                likes=item.likes,
                comments=item.comments,
                shares=item.shares,
                duration_sec=item.duration_sec,
                width=item.width,
                height=item.height,
                source_published_at=item.published_at,
                status=VideoStatus.PENDING,
                meta=dict(item.meta or {}),
            )
            recompute_scores([video])
            db.add(video)
            created += 1
    return {"created": created, "skipped": skipped, "refreshed": refreshed}


# ---------- Queue maintenance ----------
def _item_view(video: Video) -> ScrapedItem:
    """Adapt a stored Video to the ScrapedItem shape the filters expect."""
    return ScrapedItem(
        source=video.source,
        source_id=video.source_id,
        source_url=video.source_url or "",
        author=video.author or "",
        title=video.title or "",
        description=video.description or "",
        keywords=list(video.keywords or []),
        language=video.language or "",
        views=video.views or 0,
        likes=video.likes or 0,
        comments=video.comments or 0,
        shares=video.shares or 0,
        duration_sec=video.duration_sec or 0,
        width=video.width or 0,
        height=video.height or 0,
        meta=dict(video.meta or {}),
    )


def apply_queue_filters(dry_run: bool = False) -> dict:
    """Re-check every PENDING video against the *current* filters.

    Filters change -- the safety blocklist grows, the channel narrows to one
    language -- but videos already in the queue were admitted under the old
    rules. This brings the backlog in line without re-scraping, and is what the
    dashboard "apply filters" control calls.
    """
    rejected: dict[str, int] = {}
    kept = 0
    changed: list[str] = []

    with session_scope() as db:
        rows = db.execute(
            select(Video).where(Video.status == VideoStatus.PENDING)
        ).scalars().all()

        for video in rows:
            item = _item_view(video)
            reason = (
                content_safety_reason(item)
                or channel_focus_reason(item)
                or _shorts_shape_reason(item)
            )
            if reason is None:
                kept += 1
                continue
            key = reason.split(":")[0]
            rejected[key] = rejected.get(key, 0) + 1
            changed.append(video.id)
            if not dry_run:
                video.status = VideoStatus.REJECTED
                video.curated_at = datetime.now(timezone.utc)
                video.meta = {**(video.meta or {}), "rejected_reason": reason}

        if dry_run:
            db.expunge_all()

    result = {
        "checked": len(rows),
        "kept": kept,
        "rejected": sum(rejected.values()),
        "reasons": rejected,
        "dry_run": dry_run,
    }
    log.info("apply_queue_filters: %s", result)
    return result


# ---------- Scrape ----------
def scrape_task(handles: list[str] | None = None,
                hashtags: list[str] | None = None,
                max_per_run: int | None = None) -> dict:
    """Scrape one batch of candidate videos and persist the survivors.

    Order of operations matters: metadata is gathered first, then filtered and
    ranked while it is still free, and only the winners are downloaded.
    """
    handles = handles if handles is not None else settings.handles_list
    hashtags = hashtags if hashtags is not None else settings.hashtags_list
    max_per_run = max_per_run or settings.scrape_max_per_run

    if settings.use_mock:
        return _scrape_mock(handles, hashtags, max_per_run)

    with session_scope() as db:
        discovery.ensure_seed_creators(db, handles, platform="snapchat")
        if settings.auto_discovery:
            active = discovery.list_active_creators(db, limit=20)
            handles = list(
                {*handles, *(c.handle for c in active if c.platform == "snapchat")}
            )

    try:
        from app.scrapers.snapchat_scraper import SnapchatScraper
    except Exception as exc:
        log.error("scrape_task: Snapchat scraper unavailable: %s", exc)
        return {"created": 0, "skipped": 0, "refreshed": 0, "fetched": 0, "error": str(exc)}

    fetched: list[ScrapedItem] = []
    chosen: list[ScrapedItem] = []
    rejected: dict = {}

    with SnapchatScraper() as scraper:
        # Spotlight is the firehose: one request yields 25 hydrated stories.
        try:
            fetched.extend(
                scraper.scrape_hashtags(hashtags, max_per_run * 2, download=False)
            )
        except Exception as exc:
            log.warning("scrape_task: spotlight crawl failed: %s", exc)

        # Followed creators cost an extra request per video, so only top up.
        if handles and len(fetched) < max_per_run:
            try:
                fetched.extend(
                    scraper.scrape_handles(
                        handles, max_per_run - len(fetched), download=False
                    )
                )
            except Exception as exc:
                log.warning("scrape_task: profile crawl failed: %s", exc)

        fetched = scraper._dedup(fetched)
        chosen, rejected = select_candidates(fetched, max_per_run)
        log.info(
            "scrape_task: fetched=%d chosen=%d rejected=%s",
            len(fetched), len(chosen), rejected or {},
        )
        if chosen:
            scraper.download(chosen)

    downloaded = [item for item in chosen if item.local_path]
    result = persist_scraped_items(downloaded)
    result.update(
        fetched=len(fetched),
        chosen=len(chosen),
        downloaded=len(downloaded),
        rejected=rejected,
    )
    log.info("scrape_task: %s", result)
    return result


def _scrape_mock(handles: list[str], hashtags: list[str], max_per_run: int) -> dict:
    """Mock-mode scrape: instant fake videos, no network."""
    scraper = get_scraper()
    items = scraper.scrape_handles(handles or ["@mockstar"], max_per_run, download=True)
    items += scraper.scrape_hashtags(
        hashtags or ["viral"], max(1, max_per_run // 2), download=True
    )
    items = scraper._dedup(items)[:max_per_run]
    result = persist_scraped_items(items)
    result.update(
        fetched=len(items), chosen=len(items), downloaded=len(items), rejected={}
    )
    return result



# ---------- Metadata generation ----------
def generate_metadata_for_video(video_id: str, *, force: bool = False) -> dict:
    """Generate or refresh the YouTube metadata draft for a video.

    A draft the user has edited by hand is never overwritten unless ``force``
    is set -- their edit is the whole point of having a draft.
    """
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None:
            return {"ok": False, "error": "video not found"}
        draft = db.execute(
            select(MetadataDraft).where(MetadataDraft.video_id == video_id)
        ).scalar_one_or_none()
        if draft is not None and draft.edited and not force:
            return {
                "ok": True, "video_id": video_id, "skipped": "user-edited draft",
                "title": draft.title, "tags": draft.tags,
            }
        meta = dict(video.meta or {})
        params = {
            "source_title": video.title or "",
            "source_desc": video.description or "",
            "author": video.author or "",
            "author_name": video.author_name or "",
            "platform": video.source or "",
            "source_url": video.source_url or "",
            "keywords": list(video.keywords or []),
            "language": video.language or "",
            "platform_title": meta.get("llm_title") or "",
            "platform_description": meta.get("llm_description") or "",
            # Cropping removes the on-screen credit, so the description one
            # stops being optional.
            "require_credit": settings.source_crop_active,
        }

    generated: GeneratedMetadata = generate_metadata(**params)

    with session_scope() as db:
        draft = db.execute(
            select(MetadataDraft).where(MetadataDraft.video_id == video_id)
        ).scalar_one_or_none()
        if draft is None:
            draft = MetadataDraft(video_id=video_id)
            db.add(draft)
        draft.title = generated.title
        draft.description = generated.description
        draft.tags = list(generated.tags)
        draft.detected_language = generated.detected_language
        draft.generated_at = datetime.now(timezone.utc)
        draft.edited = False

    return {
        "ok": True,
        "video_id": video_id,
        "title": generated.title,
        "description": generated.description[:160],
        "tags": generated.tags,
        "language": generated.detected_language,
        "source": generated.source,
    }


# 9:16 is the Shorts canvas. Sources within this tolerance need no letterboxing.
_TARGET_RATIO = 9 / 16
_RATIO_TOLERANCE = 0.03


def _aspect_matches_shorts(width: int, height: int) -> bool:
    """Whether a source is already (near enough) 9:16."""
    if width <= 0 or height <= 0:
        return False
    return abs((width / height) - _TARGET_RATIO) < _RATIO_TOLERANCE


def plan_transform(width: int, height: int) -> dict:
    """Decide how to get a source onto the Shorts canvas.

    Three cases, and the distinction matters for both quality and CPU cost:

    ``copy``
        Already 9:16 at (or above) the target resolution -- remuxing it would
        only cost a generation of quality.
    ``scale``
        Already 9:16 but smaller than the canvas (Snapchat serves 540x960).
        It needs upscaling, but a blurred backdrop would be invisible behind a
        foreground that covers the frame, so we skip that filter entirely.
    ``letterbox``
        Not 9:16, so it gets the blurred-background treatment to fill the canvas
        without black bars.
    """
    target_w, target_h = settings.transform_width, settings.transform_height
    if not _aspect_matches_shorts(width, height):
        return {"action": "letterbox", "add_blur_background": True}
    if width >= target_w and height >= target_h:
        return {"action": "copy", "add_blur_background": False}
    return {"action": "scale", "add_blur_background": False}


# ---------- Transform ----------
def transform_task(video_id: str, recipe_dict: dict | None = None) -> dict:
    """Render a source video onto the vertical Shorts canvas."""
    explicit_recipe = bool(recipe_dict)
    recipe_dict = dict(recipe_dict or {})

    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None:
            return {"ok": False, "error": "video not found"}
        if not video.local_path:
            video.local_path = _ensure_mock_input(video)
        if not os.path.exists(video.local_path):
            video.status = VideoStatus.FAILED
            video.error = "transform: source file missing from disk"
            return {"ok": False, "error": video.error}

        video.status = VideoStatus.TRANSFORMING
        video.error = None
        job = TransformJob(
            video_id=video.id, recipe=recipe_dict, status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.flush()

        job_id = job.id
        video_pk = video.id
        input_path = video.local_path
        known_width, known_height = video.width or 0, video.height or 0
        variation_title = video.title or ""
        variation_keywords = list(video.keywords or [])

    out_dir = os.path.join(os.path.dirname(input_path) or ".", "transformed")
    os.makedirs(out_dir, exist_ok=True)
    output_path = os.path.join(out_dir, f"{video_pk}.mp4")

    try:
        if settings.use_mock or not _ffmpeg_available():
            import shutil

            shutil.copyfile(input_path, output_path)
            plan = {"action": "copy (mock/no-ffmpeg)"}
        else:
            width, height = known_width, known_height
            if not (width and height):
                probe = _probe_quality(input_path, source="snapchat")
                width, height = probe["width"], probe["height"]
            plan = plan_transform(width, height)

            variation = plan_variation(
                video_id=video_pk,
                title=variation_title,
                keywords=variation_keywords,
            )
            # A verbatim copy is only safe when we are deliberately not
            # differentiating the output; otherwise always re-render.
            plain_copy = (
                plan["action"] == "copy"
                and not explicit_recipe
                and not settings.transform_variation
            )
            if plain_copy:
                import shutil

                shutil.copyfile(input_path, output_path)
            else:
                if not explicit_recipe:
                    recipe_dict["add_blur_background"] = plan["add_blur_background"]
                    recipe_dict["add_caption"] = False
                recipe_dict.setdefault("width", settings.transform_width)
                recipe_dict.setdefault("height", settings.transform_height)
                get_transformer().transform(
                    input_path, output_path, recipe_from_dict(recipe_dict),
                    plan=variation,
                )
            log.info(
                "transform: %s %dx%d -> %s (%s) %s",
                video_pk[:8], width, height, plan["action"],
                "blur" if recipe_dict.get("add_blur_background") else "no blur",
                "copied" if plain_copy else variation.summary(),
            )

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            raise RuntimeError("transform produced no usable output")

        with session_scope() as db:
            video = db.get(Video, video_pk)
            job = db.get(TransformJob, job_id)
            video.transformed_path = output_path
            video.status = VideoStatus.TRANSFORMED
            video.transformed_at = datetime.now(timezone.utc)
            video.meta = {
                **(video.meta or {}),
                "transform_action": plan.get("action"),
                "variation": variation.summary() if not plain_copy else "copied",
            }
            job.status = "done"
            job.output_path = output_path
            job.finished_at = datetime.now(timezone.utc)

        # Draft the YouTube copy now, so a publish never has to wait on it.
        try:
            generate_metadata_for_video(video_pk)
        except Exception as exc:
            log.warning("transform: metadata generation failed for %s: %s", video_pk, exc)

        return {
            "ok": True, "video_id": video_pk, "output_path": output_path,
            "action": plan.get("action"),
        }

    except Exception as exc:
        log.exception("transform failed for %s", video_pk)
        with session_scope() as db:
            video = db.get(Video, video_pk)
            job = db.get(TransformJob, job_id)
            if video is not None:
                video.status = VideoStatus.FAILED
                video.error = f"transform: {exc}"
            if job is not None:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = datetime.now(timezone.utc)
        return {"ok": False, "error": str(exc)}


def _is_already_9x16(path: str) -> bool:
    """Back-compat shim: 9:16 *and* already at least Shorts resolution."""
    probe = _probe_quality(path, source="snapchat")
    plan = plan_transform(probe["width"], probe["height"])
    return plan["action"] == "copy"


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

        # A file is "valid" if ffprobe found a real video stream with plausible
        # dimensions and a non-trivial size. Editorial judgement (duration,
        # aspect, resolution floor) belongs to _is_quality_short, not here.
        result["valid"] = (
            result["duration"] >= 1.0
            and result["width"] >= 120
            and result["height"] >= 120
            and result["size_bytes"] >= settings.min_file_size_bytes
        )
    except Exception:
        pass
    return result


def _is_quality_short(item: ScrapedItem) -> bool:
    """Confirm a downloaded file is a real, playable, vertical short.

    Shape and engagement were already vetted from platform metadata before the
    download; this is the on-disk check that the bytes we actually received are
    a usable video and not an error page or a truncated stream.
    """
    if not item.local_path or not os.path.exists(item.local_path):
        return False

    size = os.path.getsize(item.local_path)
    if settings.use_mock:
        return size >= 1000 and 3 <= item.duration_sec <= 179

    if size < settings.min_file_size_bytes:
        log.debug("quality: %s too small (%d bytes)", item.source_id[:12], size)
        return False

    probe = _probe_quality(item.local_path, source=item.source)
    if not probe["valid"]:
        log.debug("quality: %s failed ffprobe", item.source_id[:12])
        return False

    width, height = probe["width"], probe["height"]
    if height and height < settings.min_source_height:
        log.debug("quality: %s too low-res (%dx%d)", item.source_id[:12], width, height)
        return False
    if width and height and width > height:
        log.debug("quality: %s is landscape (%dx%d)", item.source_id[:12], width, height)
        return False

    # Trust the probed duration over the platform metadata once we have the file.
    duration = int(probe["duration"]) or item.duration_sec
    if not settings.min_source_duration_sec <= duration <= settings.max_source_duration_sec:
        log.debug("quality: %s duration %ss out of range", item.source_id[:12], duration)
        return False
    if duration and not item.duration_sec:
        item.duration_sec = duration
    if width and not item.width:
        item.width, item.height = width, height
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
# Substrings that mark a failure as *our configuration*, not the video. These
# must leave the video in TRANSFORMED so it publishes on the next attempt once
# credentials are in place -- marking it FAILED would silently drop content.
_RETRYABLE_ERROR_MARKERS = (
    "token missing",
    "token refresh failed",
    "oauth not configured",
    "client libs not installed",
    "quota",
    "ratelimitexceeded",
    "backenderror",
    "internalservererror",
    "service unavailable",
    "timed out",
    "timeout",
    "connection",
    "ssl",
)


def _is_retryable_publish_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _RETRYABLE_ERROR_MARKERS)


def publish_task(video_id: str, meta: dict | None = None) -> dict:
    """Upload a transformed video to YouTube."""
    if not settings.enable_publish:
        return {"ok": False, "error": "publishing disabled (enable_publish=false)"}
    meta = meta or {}

    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None:
            return {"ok": False, "error": "video not found"}
        if not video.transformed_path:
            return {"ok": False, "error": "video not transformed yet"}
        if not os.path.exists(video.transformed_path):
            video.status = VideoStatus.FAILED
            video.error = "publish: transformed file missing from disk"
            return {"ok": False, "error": video.error}
        if quota.remaining(db) <= 0:
            return {"ok": False, "error": "daily quota exhausted"}

        video.status = VideoStatus.PUBLISHING
        video.error = None
        video_pk = video.id
        file_path = video.transformed_path

        draft = db.execute(
            select(MetadataDraft).where(MetadataDraft.video_id == video_id)
        ).scalar_one_or_none()
        if draft is not None and draft.title:
            title = meta.get("title") or draft.title
            description = meta.get("description") or draft.description
            tags = meta.get("tags") or list(draft.tags or [])
        else:
            title = meta.get("title") or f"{video.title} #Shorts".strip()
            description = meta.get("description") or video.description or ""
            tags = meta.get("tags") or settings.tags_list
        privacy = meta.get("privacy") or settings.youtube_default_privacy

    try:
        result = get_publisher().upload(
            file_path=file_path,
            title=title, description=description, tags=tags,
            category=settings.youtube_default_category,
            privacy=privacy,
        )
        with session_scope() as db:
            video = db.get(Video, video_pk)
            video.youtube_video_id = result.video_id
            video.status = VideoStatus.PUBLISHED
            video.published_at = datetime.now(timezone.utc)
            quota.consume_one(db, note=f"video {video_pk}")
            # Reward the creator whose video made it all the way through.
            if video.author:
                discovery.boost_creator(db, video.source, video.author, amount=1.0)
        log.info("publish: %s -> youtube %s", video_pk[:8], result.video_id)
        return {"ok": True, "video_id": video_pk, "youtube_id": result.video_id}

    except Exception as exc:
        message = str(exc)
        retryable = _is_retryable_publish_error(message)
        log.error(
            "publish failed for %s (%s): %s",
            video_pk[:8], "will retry" if retryable else "permanent", message,
        )
        if not retryable:
            log.debug("publish traceback", exc_info=True)
        with session_scope() as db:
            video = db.get(Video, video_pk)
            if video is not None:
                # Roll back to TRANSFORMED so the scheduler picks it up again.
                video.status = (
                    VideoStatus.TRANSFORMED if retryable else VideoStatus.FAILED
                )
                video.error = f"publish: {message}"
        return {"ok": False, "error": message, "retryable": retryable}


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
