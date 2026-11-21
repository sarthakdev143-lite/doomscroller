"""Long-form compilation queue worker, supply/autopilot loops (1/day cadence), and panel actions."""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import desc, func, select, update

from app import quota
from app.clipper import job as clip_job
from app.config import settings
from app.db import session_scope
from app.growth import channels, safety
from app.growth.factors import channel_zone
from app.metadata import compose, finished_title, youtube_description
from app.models import Channel, GrowthEvent, LongJob, MetadataDraft, Video, VideoStatus
from app.winproc import reclaim_memory

log = logging.getLogger(__name__)

_worker_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_TERMINAL_STATUSES = ("done", "succeeded", "failed", "cancelled", "canceled")
_FORMAT_ROTATION = ("best_of", "vs", "theme")
_STOCK_CAP = 2


def _parse_targets(format_kind: str, target: str, targets: Sequence[str]) -> tuple[list[str], str]:
    clean_list = [str(t).strip() for t in targets if str(t).strip()]
    raw = str(target or "").strip()
    if format_kind == "vs" and not clean_list and raw:
        parts = [p.strip() for p in re.split(r"\s+\bvs\b\s+|,|;|\|", raw, flags=re.IGNORECASE) if p.strip()]
        clean_list = parts[:2]
    elif format_kind == "best_of" and not clean_list and raw:
        clean_list = [raw]
    elif format_kind == "theme" and raw and not clean_list:
        return [], raw
    return clean_list, ""


def enqueue_long_job(
    channel_slug: str = "",
    *,
    format_kind: str = "best_of",
    target: str = "",
    targets: Sequence[str] = (),
    theme: str = "",
    target_minutes: int = 8,
    auto_publish: Optional[bool] = None,
) -> str:
    """Enqueue a 16:9 compilation build job for a managed channel (or default channel when omitted)."""
    fmt = str(format_kind or "best_of").strip().lower()
    if fmt not in _FORMAT_ROTATION:
        raise ValueError(f"unsupported long-form format {format_kind!r}; use best_of, vs, or theme")
    mins = max(6, min(10, int(target_minutes or 8)))
    parsed_targets, parsed_theme = _parse_targets(fmt, target, targets)
    final_theme = str(theme or parsed_theme or "").strip()

    with session_scope() as db:
        channels._ensure_seeded(db)
        slug = str(channel_slug or "").strip()
        channel = channels.channel_by_slug(db, slug) if slug else channels.default_channel(db)
        if channel is None:
            raise ValueError(f"unknown channel {channel_slug!r}")
        effective_auto = channels.long_auto_publish(channel) if auto_publish is None else bool(auto_publish)
        job = LongJob(
            channel_id=channel.id,
            format=fmt,
            targets=parsed_targets,
            theme=final_theme,
            target_minutes=mins,
            auto_publish=effective_auto,
        )
        db.add(job)
        db.flush()
        return job.id


def _job_view(job: LongJob, slug_map: dict[str, str]) -> dict:
    return {
        "id": job.id,
        "channel_id": job.channel_id,
        "channel_slug": slug_map.get(job.channel_id or "", ""),
        "format": job.format,
        "targets": list(job.targets or []),
        "theme": job.theme or "",
        "part_number": job.part_number,
        "target_minutes": job.target_minutes,
        "auto_publish": bool(job.auto_publish),
        "status": job.status,
        "stage": job.stage or "",
        "video_id": job.video_id,
        "error": job.error,
        "result": job.result or {},
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _slug_map(db, jobs: Sequence[LongJob]) -> dict[str, str]:
    ids = {j.channel_id for j in jobs if j.channel_id}
    if not ids:
        return {}
    return {cid: slug for cid, slug in db.execute(select(Channel.id, Channel.slug).where(Channel.id.in_(ids))).all()}


def list_long_jobs(limit: int = 20) -> list[dict]:
    capped = max(1, min(100, int(limit)))
    with session_scope() as db:
        rows = db.execute(select(LongJob).order_by(desc(LongJob.created_at), LongJob.id).limit(capped * 3)).scalars().all()
        visible = [j for j in rows if not isinstance(j.result, dict) or not j.result.get("dismissed")][:capped]
        slugs = _slug_map(db, visible)
        return [_job_view(j, slugs) for j in visible]


def retry_long_job(job_id: str) -> dict:
    with session_scope() as db:
        job = db.get(LongJob, job_id)
        if job is None:
            raise LookupError("no such long job")
        if job.status != "failed":
            raise ValueError(f"cannot retry a {job.status} job")
        job.status = "queued"
        job.stage = ""
        job.error = None
        job.started_at = None
        job.finished_at = None
        job.result = {}
        return _job_view(job, _slug_map(db, [job]))


def dismiss_long_job(job_id: str) -> dict:
    with session_scope() as db:
        job = db.get(LongJob, job_id)
        if job is None:
            raise LookupError("no such long job")
        if job.status == "running":
            raise ValueError("cannot dismiss a running job")
        if str(job.status or "").lower() in _TERMINAL_STATUSES:
            job.result = {**(job.result or {}), "dismissed": True}
        else:
            db.delete(job)
    return {"id": job_id, "dismissed": True}


def _set_stage(job_id: str, stage: str) -> None:
    with session_scope() as db:
        job = db.get(LongJob, job_id)
        if job is not None:
            job.stage = stage[:160]


def run_next_long_job(*, now: Optional[datetime] = None) -> Optional[dict]:
    """Claim and build the oldest queued LongJob under the shared CPU RENDER_LOCK."""
    if not clip_job.RENDER_LOCK.acquire(blocking=False):
        return {"busy": True}
    now = now or datetime.now(timezone.utc)
    job_id = None
    try:
        with session_scope() as db:
            job = db.execute(
                select(LongJob).where(LongJob.status == "queued").order_by(LongJob.created_at, LongJob.id).limit(1)
            ).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.stage = "planning"
            job.attempts = (job.attempts or 0) + 1
            job.started_at = now
            job_id = job.id
            channel = channels.get_channel(db, job.channel_id) or channels.default_channel(db)
            if channel is None:
                raise ValueError("no managed channel found")
            ch_id = channel.id
            ch_slug = channel.slug
            ch_niche = channel.niche or ""
            ch_brand = (settings.brand_name if channels.is_default(channel) else channel.brand_name) or ""
            feeds = channels.feeds_for(db, channel)
            fmt = job.format
            targets = list(job.targets or [])
            theme = job.theme or ""
            target_mins = job.target_minutes
            auto_pub = bool(job.auto_publish)

        from . import planner, render, thumbnails

        with session_scope() as db:
            plan = planner.plan_compilation(
                format_kind=fmt,
                channel_slug=ch_slug,
                targets=targets,
                theme=theme,
                feeds=feeds,
                target_minutes=target_mins,
                db=db,
                channel_id=ch_id,
                niche=ch_niche,
            )

        _set_stage(job_id, f"rendering {len(plan.segments)} segments")
        out_dir = settings.content_path / "longform" / ch_slug
        out_path = out_dir / f"{job_id}.mp4"
        rendered = render.render_compilation(plan, str(out_path), brand_name=ch_brand)

        _set_stage(job_id, "generating thumbnails")
        thumbs_dir = out_dir / f"{job_id}_thumbs"
        thumbs, thumb_prompt = thumbnails.generate_thumbnails_with_prompt(
            rendered.output_path,
            out_dir=thumbs_dir,
            title=plan.title,
            format_kind=plan.format_kind,
            chapters=rendered.chapters,
            timestamps=rendered.timestamps_for_thumbs,
            part_number=plan.part_number,
            creator_labels=plan.targets,
        )

        full_desc = youtube_description(
            f"{plan.description}\n\nChapters:\n{rendered.chapter_text}".strip()
            if rendered.chapter_text else plan.description
        )
        targets_key = "|".join(sorted({t.casefold() for t in plan.targets if t}))
        stored_segments = [
            {
                "source_id": s.source_id,
                "source_url": s.source_url,
                "creator": s.creator,
                "start": s.start,
                "end": s.end,
                "title": s.moment_title,
                "score": s.score,
            }
            for s in plan.segments
        ]
        selected = next((t for t in thumbs if t["id"] == "v4"), thumbs[0] if thumbs else None)
        status = VideoStatus.TRANSFORMED if auto_pub else VideoStatus.PENDING
        done_at = datetime.now(timezone.utc)

        with session_scope() as db:
            video = Video(
                source="longform",
                source_id=job_id,
                source_url=plan.segments[0].source_url if plan.segments else "",
                author=", ".join(plan.targets)[:128],
                author_name=", ".join(plan.targets)[:128],
                title=plan.title,
                description=full_desc,
                language=plan.language,
                keywords=list(plan.tags),
                local_path=rendered.output_path,
                transformed_path=rendered.output_path,
                duration_sec=int(round(rendered.duration)),
                width=render.WIDTH,
                height=render.HEIGHT,
                status=status,
                channel_id=ch_id,
                transformed_at=done_at if status == VideoStatus.TRANSFORMED else None,
                meta={
                    "long_form": True,
                    "clip_rendered": False,
                    "long_job": job_id,
                    "long_format": plan.format_kind,
                    "long_targets": list(plan.targets),
                    "long_targets_key": targets_key,
                    "part_number": plan.part_number,
                    "long_theme": plan.theme,
                    "long_segments": stored_segments,
                    "chapters": [{"sec": round(sec, 2), "title": lbl} for sec, lbl in rendered.chapters],
                    "thumbnails": thumbs,
                    "thumbnail_prompt": thumb_prompt,
                    "selected_thumbnail": selected["path"] if selected else None,
                    "selected_thumbnail_id": selected["id"] if selected else None,
                    "thumb_version": 1,
                },
            )
            db.add(video)
            db.flush()
            db.add(MetadataDraft(
                video_id=video.id,
                title=finished_title(plan.title, shorts_tag=False),
                description=full_desc,
                tags=list(plan.tags),
                detected_language=plan.language,
                edited=False,
            ))
            job_row = db.get(LongJob, job_id)
            if job_row is not None:
                job_row.part_number = plan.part_number
                job_row.status = "done"
                job_row.stage = "ready"
                job_row.video_id = video.id
                job_row.finished_at = done_at
                job_row.result = {"ok": True, "duration": rendered.duration, "segments": len(plan.segments)}
            return {"ok": True, "job_id": job_id, "video_id": video.id, "duration": rendered.duration}
    except Exception as exc:
        log.exception("longform.job: failed job %s", job_id)
        if job_id is not None:
            with session_scope() as db:
                job_row = db.get(LongJob, job_id)
                if job_row is not None:
                    job_row.status = "failed"
                    job_row.stage = "failed"
                    job_row.error = f"{type(exc).__name__}: {exc}"[:1000]
                    job_row.finished_at = datetime.now(timezone.utc)
        return {"ok": False, "job_id": job_id, "error": str(exc)}
    finally:
        clip_job.RENDER_LOCK.release()
        reclaim_memory()


def _worker_loop() -> None:
    while True:
        res = run_next_long_job()
        if res is None or res.get("busy"):
            return


def start_long_worker() -> bool:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _worker_thread = threading.Thread(target=_worker_loop, name="longform-worker", daemon=True)
        _worker_thread.start()
        return True


def _pick_auto_format(db, channel: Channel) -> tuple[str, list[str], str]:
    last = db.execute(
        select(LongJob).where(LongJob.channel_id == channel.id, LongJob.status != "failed")
        .order_by(desc(LongJob.created_at), LongJob.id).limit(1)
    ).scalar_one_or_none()
    if last and last.format in _FORMAT_ROTATION:
        next_idx = (_FORMAT_ROTATION.index(last.format) + 1) % len(_FORMAT_ROTATION)
        fmt = _FORMAT_ROTATION[next_idx]
    else:
        fmt = "best_of"
    feeds = channels.feeds_for(db, channel)
    if fmt == "vs" and len(feeds) >= 2:
        return "vs", feeds[:2], ""
    if fmt == "theme" and feeds:
        return "theme", [], (channel.niche or "Best Moments")
    return "best_of", (feeds[:1] if feeds else []), ""


def long_supply_job(*, now: Optional[datetime] = None) -> dict:
    """Keep up to 2 16:9 compilations built ahead per enabled channel with long_uploads_per_day > 0."""
    with session_scope() as db:
        active = [c for c in channels.active_channels(db) if channels.long_uploads_per_day(c) > 0]
        if not active:
            return {"skipped": "no channels configured for long-form"}
        def run_one(channel: Channel) -> dict:
            with session_scope() as s:
                ch = s.get(Channel, channel.id) or channel
                if safety.pause_reason(s, ch):
                    return {"skipped": "paused"}
                scope = channels.video_scope(ch)
                waiting_rows = s.execute(
                    select(Video).where(scope, Video.status.in_((VideoStatus.PENDING, VideoStatus.TRANSFORMED)))
                ).scalars().all()
                stock = sum(
                    1 for v in waiting_rows
                    if (v.meta or {}).get("long_form") and v.transformed_path and os.path.isfile(v.transformed_path)
                )
                active_jobs = s.scalar(
                    select(func.count(LongJob.id)).where(LongJob.channel_id == ch.id, LongJob.status.in_(("queued", "running")))
                ) or 0
                if stock >= _STOCK_CAP or active_jobs > 0:
                    return {"ok": True, "queued": False, "stock": stock, "active_jobs": active_jobs}
                fmt, targets, theme = _pick_auto_format(s, ch)
                if not targets and not theme and not channels.feeds_for(s, ch):
                    return {"skipped": "no feeds"}
                job = LongJob(
                    channel_id=ch.id,
                    format=fmt,
                    targets=targets,
                    theme=theme,
                    target_minutes=8,
                    auto_publish=channels.long_auto_publish(ch),
                )
                s.add(job)
                s.flush()
                jid = job.id
            start_long_worker()
            return {"ok": True, "queued": True, "job_id": jid, "format": fmt}
        return channels.for_each(active, run_one)


def long_autopilot_job(*, now: Optional[datetime] = None) -> dict:
    """Publish ready (or opt-in auto-published) long-form compilations up to long_uploads_per_day/day."""
    from tasks.pipeline import publish_task

    now = now or datetime.now(timezone.utc)
    with session_scope() as db:
        active = [c for c in channels.active_channels(db) if channels.long_uploads_per_day(c) > 0]
    if not active:
        return {"skipped": "no channels configured for long-form"}

    def run_one(channel: Channel) -> dict:
        cap = channels.long_uploads_per_day(channel)
        with session_scope() as db:
            ch = db.get(Channel, channel.id) or channel
            if safety.pause_reason(db, ch) or safety.active_hold(db, now, ch):
                return {"skipped": "paused or held"}
            if quota.remaining(db, ch.id) <= 0:
                return {"skipped": "daily quota exhausted"}
            scope = channels.video_scope(ch)
            midnight_utc = now.astimezone(channel_zone(ch)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(timezone.utc)
            todays_rows = db.execute(
                select(Video).where(scope, Video.status == VideoStatus.PUBLISHED, Video.published_at >= midnight_utc)
            ).scalars().all()
            posted_today = sum(1 for v in todays_rows if (v.meta or {}).get("long_form"))
            if posted_today >= cap:
                return {"skipped": "daily_cap", "posted_today": posted_today, "cap": cap}

            all_long = db.execute(
                select(Video).where(scope, Video.status.in_((VideoStatus.TRANSFORMED, VideoStatus.PENDING)))
                .order_by(Video.scraped_at)
            ).scalars().all()
            ready = [v for v in all_long if v.status == VideoStatus.TRANSFORMED and (v.meta or {}).get("long_form")
                     and v.transformed_path and os.path.isfile(v.transformed_path)]
            target_video: Optional[Video] = ready[0] if ready else None
            if target_video is None and channels.long_auto_publish(ch):
                pending = [v for v in all_long if v.status == VideoStatus.PENDING and (v.meta or {}).get("long_form")
                           and v.transformed_path and os.path.isfile(v.transformed_path)]
                if pending:
                    target_video = pending[0]
                    target_video.status = VideoStatus.TRANSFORMED
                    target_video.transformed_at = now
                    target_video.meta = {**(target_video.meta or {}), "long_auto_published": now.isoformat()}
            if target_video is None:
                return {"skipped": "none_ready"}
            vid = target_video.id
            vtitle = target_video.title or ""

        res = publish_task(vid)
        if res.get("ok") and res.get("youtube_id"):
            with session_scope() as db:
                db.add(GrowthEvent(
                    kind="long_posted",
                    video_id=vid,
                    channel_id=channels.event_channel(channel),
                    summary=f"Published 16:9 compilation: {vtitle[:60]}",
                    data={"youtube_id": res["youtube_id"]},
                ))
        return res

    return channels.for_each(active, run_one)


def _long_video(db, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if video is None or not (video.meta or {}).get("long_form"):
        raise LookupError("no such long-form video")
    return video


def thumbnail_path(video_id: str, variant_id: str) -> Optional[str]:
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None or not (video.meta or {}).get("long_form"):
            return None
        for t in (video.meta or {}).get("thumbnails") or []:
            if isinstance(t, dict) and t.get("id") == variant_id and t.get("path") and os.path.isfile(t["path"]):
                return t["path"]
        return None


def select_thumbnail(video_id: str, variant_id: str) -> dict:
    with session_scope() as db:
        video = _long_video(db, video_id)
        meta = dict(video.meta or {})
        match = next((t for t in meta.get("thumbnails") or [] if isinstance(t, dict) and t.get("id") == variant_id), None)
        if match is None or not os.path.isfile(str(match.get("path") or "")):
            raise ValueError(f"thumbnail variant {variant_id!r} not found")
        meta["selected_thumbnail"] = match["path"]
        meta["selected_thumbnail_id"] = variant_id
        video.meta = meta
        return {"id": video_id, "selected_thumbnail_id": variant_id}


def upload_custom_thumbnail(video_id: str, raw_bytes: bytes) -> dict:
    from . import thumbnails

    with session_scope() as db:
        video = _long_video(db, video_id)
        if video.status == VideoStatus.PUBLISHING:
            raise ValueError("video is currently publishing")
        meta = dict(video.meta or {})
        ch = channels.get_channel(db, video.channel_id)
        slug = ch.slug if ch else "default"
        dest = settings.content_path / "longform" / slug / f"{video.id}_custom_thumb.jpg"
        saved_path = thumbnails.save_custom_thumbnail(dest, raw_bytes)
        thumbs = [t for t in (meta.get("thumbnails") or []) if isinstance(t, dict) and t.get("id") != "custom"]
        thumbs.append({"id": "custom", "label": "Custom Upload", "path": saved_path})
        ver = int(meta.get("thumb_version") or 1) + 1
        meta["thumbnails"] = thumbs
        meta["selected_thumbnail"] = saved_path
        meta["selected_thumbnail_id"] = "custom"
        meta["thumb_version"] = ver
        video.meta = meta
        return {"id": video_id, "selected_thumbnail_id": "custom", "thumb_version": ver}


def update_long_metadata(
    video_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> dict:
    with session_scope() as db:
        video = _long_video(db, video_id)
        if video.status in (VideoStatus.PUBLISHING, VideoStatus.PUBLISHED):
            raise ValueError("already published")
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video.id)).scalar_one_or_none()
        if draft is None:
            draft = MetadataDraft(video_id=video.id, detected_language=video.language or "en")
            db.add(draft)
        if title is not None:
            clean = finished_title(title, shorts_tag=False)
            if not clean or clean == "Watch this":
                raise ValueError("title cannot be empty")
            if not str(title).strip():
                raise ValueError("title cannot be empty")
            video.title = clean
            draft.title = clean
        if description is not None:
            clean_desc = youtube_description(description)
            if not clean_desc:
                raise ValueError("description cannot be empty")
            video.description = clean_desc
            draft.description = clean_desc
        if tags is not None:
            composed = compose(title=video.title or "Compilation", description="", keywords=list(tags), is_short=False)
            video.keywords = list(composed.tags)
            draft.tags = list(composed.tags)
        draft.edited = True
        return {
            "id": video.id,
            "title": draft.title or video.title,
            "description": draft.description or video.description,
            "tags": list(draft.tags or []),
        }


def approve_long(
    video_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    variant_id: Optional[str] = None,
) -> dict:
    if any(x is not None for x in (title, description, tags)):
        update_long_metadata(video_id, title=title, description=description, tags=tags)
    if variant_id:
        select_thumbnail(video_id, variant_id)
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        video = _long_video(db, video_id)
        if not video.transformed_path or not os.path.isfile(video.transformed_path):
            raise ValueError("rendered compilation file is missing")
        if video.status not in (VideoStatus.PENDING, VideoStatus.TRANSFORMED):
            raise ValueError(f"cannot approve a {video.status.value} compilation")
        video.status = VideoStatus.TRANSFORMED
        video.transformed_at = video.transformed_at or now
        video.curated_at = now
        video.meta = {**(video.meta or {}), "owner_approved": now.isoformat()}
        return {"id": video.id, "status": video.status.value}


def reject_long(video_id: str) -> dict:
    with session_scope() as db:
        video = _long_video(db, video_id)
        if video.status in (VideoStatus.PUBLISHING, VideoStatus.PUBLISHED) or video.youtube_video_id:
            raise ValueError("already published")
        if video.status not in (VideoStatus.PENDING, VideoStatus.TRANSFORMED):
            raise ValueError(f"cannot reject a {video.status.value} compilation")
        video.status = VideoStatus.REJECTED
        video.curated_at = datetime.now(timezone.utc)
        video.meta = {**(video.meta or {}), "owner_rejected": True}
        return {"id": video.id, "status": video.status.value}


def _long_card(db, video: Video, slug_names: dict[str, tuple[str, str]]) -> dict:
    meta = video.meta or {}
    draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video.id)).scalar_one_or_none()
    slug, cname = slug_names.get(video.channel_id or "", ("default", "default"))
    ver = int(meta.get("thumb_version") or 1)
    thumbs = [
        {
            "id": t["id"],
            "label": t.get("label") or t["id"],
            "url": f"/api/long/{video.id}/thumb/{t['id']}?v={ver}",
        }
        for t in (meta.get("thumbnails") or [])
        if isinstance(t, dict) and t.get("id") and t.get("path") and os.path.isfile(t["path"])
    ]
    return {
        "id": video.id,
        "channel_slug": slug,
        "channel_name": cname,
        "format": meta.get("long_format") or "best_of",
        "part_number": meta.get("part_number"),
        "targets": list(meta.get("long_targets") or []),
        "theme": meta.get("long_theme") or "",
        "title": (draft.title if draft and draft.title else video.title) or "",
        "description": (draft.description if draft and draft.description else video.description) or "",
        "tags": list((draft.tags if draft and draft.tags is not None else video.keywords) or []),
        "duration_sec": int(video.duration_sec or 0),
        "width": int(video.width or 1920),
        "height": int(video.height or 1080),
        "status": video.status.value,
        "thumbnails": thumbs,
        "thumbnail_prompt": meta.get("thumbnail_prompt") or "",
        "selected_thumbnail_id": meta.get("selected_thumbnail_id") or (thumbs[0]["id"] if thumbs else ""),
        "chapters": list(meta.get("chapters") or []),
        "video_url": f"/api/media/{video.id}" if video.transformed_path and os.path.isfile(video.transformed_path) else "",
        "youtube_url": f"https://www.youtube.com/watch?v={video.youtube_video_id}" if video.youtube_video_id and not video.youtube_video_id.startswith("MOCK_") else "",
        "views": int(video.views or 0),
        "thumb_version": ver,
        "created_at": video.scraped_at.isoformat() if video.scraped_at else "",
        "published_at": video.published_at.isoformat() if video.published_at else "",
    }


def long_panel_data() -> dict:
    """Data payload backing the Control Panel's Long-Form tab."""
    with session_scope() as db:
        channels._ensure_seeded(db)
        ch_rows = channels.all_channels(db)
        slug_names = {c.id: (c.slug, c.name or c.slug) for c in ch_rows}
        rows = db.execute(select(Video).order_by(desc(Video.scraped_at)).limit(120)).scalars().all()
        long_rows = [v for v in rows if (v.meta or {}).get("long_form")]
        pending = [_long_card(db, v, slug_names) for v in long_rows if v.status == VideoStatus.PENDING]
        ready = [_long_card(db, v, slug_names) for v in long_rows if v.status == VideoStatus.TRANSFORMED]
        posted = [_long_card(db, v, slug_names) for v in long_rows if v.status == VideoStatus.PUBLISHED][:12]
        channel_list = [
            {
                "slug": c.slug,
                "name": c.name or c.slug,
                "long_uploads_per_day": channels.long_uploads_per_day(c),
                "long_auto_publish": channels.long_auto_publish(c),
                "feeds": channels.feeds_for(db, c),
            }
            for c in ch_rows
        ]
    return {
        "pending": pending,
        "ready": ready,
        "posted": posted,
        "jobs": list_long_jobs(),
        "channels": channel_list,
    }
