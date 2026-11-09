"""The owner's approval step. While it is on, a clip goes out only after a person says yes.

Clips are cut into PENDING. Approving one moves it straight to TRANSFORMED, since a rendered clip
needs no transform, and the autopilot posts it in a later window. A title typed on approval
becomes an edited draft, which the autopilot posts as written rather than restyling.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

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


def _card(video: Video, db) -> dict:
    meta = video.meta or {}
    draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video.id)).scalar_one_or_none()
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


def queue() -> dict:
    from .autopilot import ready_clips
    from .factors import channel_zone

    with session_scope() as db:
        cards = [_card(v, db) for v in awaiting_review(db)]
        approved = len(ready_clips(db))
        today = datetime.now(channel_zone()).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        posted = db.execute(select(Video).where(Video.status == VideoStatus.PUBLISHED,
                                                Video.published_at >= today)).scalars().all()
        plan = db.get(GrowthState, "plan")
        waiting = [p for p in (plan.value or {}).get("posts", []) if p.get("state") == "waiting"] if plan else []
        return {
            "clips": cards,
            "approved_waiting": approved,
            "posted_today": len(posted),
            "next_post": min((p["due"] for p in waiting), default=""),
            "posted": recent_posts(db),
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
