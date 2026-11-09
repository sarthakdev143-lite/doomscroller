"""The owner's approval step. While it is on, a clip goes out only after a person says yes.

Clips are cut into PENDING. Approving one moves it straight to TRANSFORMED, since a rendered clip
needs no transform, and the autopilot posts it in a later window. A title typed on approval
becomes an edited draft, which the autopilot posts as written rather than restyling.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from app.db import session_scope
from app.metadata import finished_title
from app.models import GrowthState, MetadataDraft, Video, VideoStatus

_QUEUE_LIMIT = 40


class ReviewError(ValueError):
    pass


def awaiting_review(db, scope=None) -> list[Video]:
    conds = [Video.status == VideoStatus.PENDING]
    if scope is not None:
        conds.append(scope)
    rows = db.execute(select(Video).where(*conds)
                      .order_by(Video.scraped_at.desc()).limit(_QUEUE_LIMIT * 3)).scalars().all()
    return [v for v in rows if (v.meta or {}).get("clip_rendered") and v.transformed_path
            and os.path.exists(v.transformed_path)][:_QUEUE_LIMIT]


def _channel_of(db, channel_id) -> tuple[str, str]:
    """(display name, slug) for a clip's channel; empty for a legacy clip with none."""
    from . import channels

    channel = channels.get_channel(db, channel_id)
    return ((channel.name or channel.slug), channel.slug) if channel else ("", "")


def _card(video: Video, db) -> dict:
    meta = video.meta or {}
    draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video.id)).scalar_one_or_none()
    name, slug = _channel_of(db, video.channel_id)
    return {
        "id": video.id,
        "title": (draft.title if draft and draft.title else video.title) or "",
        "hook": meta.get("clip_hook") or "",
        "seconds": video.duration_sec or 0,
        "creator": video.author_name or video.author or "",
        "source": video.source_url or "",
        "kind": meta.get("clip_kind") or "",
        "why": meta.get("clip_reason") or "",
        "flag": meta.get("growth_flagged") or "",
        "channel": name,
        "channel_slug": slug,
        "made": (video.scraped_at.isoformat() if video.scraped_at else ""),
    }


_POSTED_LIMIT = 20


def _result(video: Video) -> dict:
    meta = video.meta or {}
    analytics = meta.get("yt_analytics") or {}
    diagnosis = meta.get("growth_diagnosis") or {}
    percent = analytics.get("averageViewPercentage")
    return {
        "title": video.title or "",
        "url": f"https://youtube.com/shorts/{video.youtube_video_id}",
        "posted": video.published_at.isoformat() if video.published_at else "",
        "views": video.views or 0,
        "likes": video.likes or 0,
        "watched": round(float(percent)) if percent is not None else None,
        "verdict": diagnosis.get("code", ""),
        "why": diagnosis.get("reason", ""),
    }


def recent_posts(db) -> list[dict]:
    """The latest real uploads with how they did, and the loop's reading of why."""
    rows = db.execute(select(Video).where(Video.status == VideoStatus.PUBLISHED, Video.youtube_video_id.isnot(None))
                      .order_by(Video.published_at.desc()).limit(_POSTED_LIMIT * 2)).scalars().all()
    return [_result(v) for v in rows if not v.youtube_video_id.startswith("MOCK_")][:_POSTED_LIMIT]


def _attention(db, channel, waiting: int, approved: int, now: datetime) -> str:
    """Why this channel needs the owner, or '' when it is fine. A halted channel must not read as merely idle."""
    from . import channels, safety, supply

    paused = safety.pause_reason(db)
    if paused:
        return "Paused — run resume"
    if safety.active_hold(db, now):
        return "Posting on hold"
    gap = db.get(GrowthState, channels.state_key(channel, supply.SUPPLY_GAP))
    cause = (gap.value or {}).get("cause") if gap else None
    if cause:
        return f"Supply: {cause[:80]}"
    if not waiting and not approved:
        return "No clips in stock"
    return ""


def _channel_status(db, channel) -> dict:
    """One channel's at-a-glance numbers for the review page's status strip."""
    from . import channels
    from .autopilot import ready_clips
    from .factors import channel_zone

    scope = channels.video_scope(channel)
    now = datetime.now(timezone.utc)
    today = datetime.now(channel_zone(channel)).replace(
        hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    posted_today = db.scalar(select(func.count(Video.id)).where(
        Video.status == VideoStatus.PUBLISHED, Video.published_at >= today, scope)) or 0
    plan = db.get(GrowthState, channels.state_key(channel, "plan"))
    waiting_posts = [p for p in (plan.value or {}).get("posts", []) if p.get("state") == "waiting"] if plan else []
    waiting = len(awaiting_review(db, scope))
    approved = len(ready_clips(db, scope))
    return {
        "slug": channel.slug,
        "name": channel.name or channel.slug,
        "waiting": waiting,
        "approved": approved,
        "posted_today": posted_today,
        "next_post": min((p["due"] for p in waiting_posts), default=""),
        "attention": _attention(db, channel, waiting, approved, now),
    }


def queue() -> dict:
    from . import channels

    with session_scope() as db:
        active = channels.active_channels(db)
        cards = [_card(v, db) for v in awaiting_review(db)]
        per = [_channel_status(db, c) for c in active]
        posted = recent_posts(db)
    # A single legacy channel keeps the flat view; several add a per-channel status strip and filter chips.
    multi = len(active) > 1 or (len(active) == 1 and not channels.is_default(active[0]))
    return {
        "clips": cards,
        "channels": per if multi else [],
        "approved_waiting": sum(c["approved"] for c in per),
        "posted_today": sum(c["posted_today"] for c in per),
        "next_post": min((c["next_post"] for c in per if c["next_post"]), default=""),
        "posted": posted,
    }


def _pending_clip(db, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if video is None or not (video.meta or {}).get("clip_rendered"):
        raise ReviewError("no such clip")
    if video.status != VideoStatus.PENDING:
        raise ReviewError(f"already {video.status.value}")
    return video


def approve(video_id: str, title: Optional[str] = None) -> dict:
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        video = _pending_clip(db, video_id)
        if not video.transformed_path or not os.path.exists(video.transformed_path):
            raise ReviewError("the clip's file is missing")
        video.status = VideoStatus.TRANSFORMED
        video.transformed_at = now
        video.curated_at = now
        video.meta = {**(video.meta or {}), "owner_approved": now.isoformat()}
        written = " ".join((title or "").split())
        if written:
            draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video_id)).scalar_one_or_none()
            if draft is None:
                draft = MetadataDraft(video_id=video_id)
                db.add(draft)
            draft.title = finished_title(written)
            draft.edited = True
        return {"id": video_id, "status": video.status.value}


def reject(video_id: str) -> dict:
    with session_scope() as db:
        video = _pending_clip(db, video_id)
        video.status = VideoStatus.REJECTED
        video.curated_at = datetime.now(timezone.utc)
        video.meta = {**(video.meta or {}), "owner_rejected": True}
        return {"id": video_id, "status": video.status.value}


def media_path(video_id: str) -> Optional[str]:
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None or not (video.meta or {}).get("clip_rendered"):
            return None
        path = video.transformed_path
        return path if path and os.path.exists(path) else None
