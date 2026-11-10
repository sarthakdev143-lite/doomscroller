"""The owner's approval step. While it is on, a clip goes out only after a person says yes.

Clips are cut into PENDING. Approving one moves it straight to TRANSFORMED, since a rendered clip
needs no transform, and the autopilot posts it in a later window. A title typed on approval
becomes an edited draft, which the autopilot posts as written rather than restyling.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update

from app.db import session_scope
from app.metadata import finished_title
from app.models import GrowthEvent, GrowthState, MetadataDraft, Video, VideoStatus

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

    paused = safety.pause_state(db, channel)
    if paused:
        # The owner's own pause needs no explaining; a takedown has to say what happened before it is lifted.
        return "Paused by you" if paused.get("by") == "owner" else f"Paused: {(paused.get('reason') or '')[:90]}"
    if safety.active_hold(db, now, channel):
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
    from . import channels, safety
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
    pause = safety.pause_state(db, channel)
    return {
        "slug": channel.slug,
        "name": channel.name or channel.slug,
        "waiting": waiting,
        "approved": approved,
        "posted_today": posted_today,
        "next_post": min((p["due"] for p in waiting_posts), default=""),
        "attention": _attention(db, channel, waiting, approved, now),
        # For the page's pause button: lifting a takedown takes a second tap, lifting your own does not.
        "paused": pause is not None,
        "owner_paused": bool(pause and pause.get("by") == "owner"),
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


def _grant(db, video: Video, now: datetime, mark: str) -> None:
    """Move a clip to the shelf without a human decision. curated_at stays None so the upload gate still runs."""
    video.status = VideoStatus.TRANSFORMED
    video.transformed_at = now
    video.meta = {**(video.meta or {}), "auto_approved": mark}


def _trustable(db, channel) -> list[Video]:
    """Clips that may go out without a person: gate-clean, not flagged, and titled by an AI.

    A rule-based title is a raw first sentence or "<source> - part N", and a visual pick may be end
    credits, so a clip no AI looked at always waits for a person, whatever its score.
    """
    from . import channels
    from tasks.pipeline import clip_gate_reason

    return [v for v in awaiting_review(db, channels.video_scope(channel))
            if v.curated_at is None and (v.meta or {}).get("clip_ai_titled")
            and not (v.meta or {}).get("growth_flagged") and not clip_gate_reason(v)]


def auto_approve_sweep(channel) -> int:
    """Auto-approve a channel's rendered clips scoring at or above its trust threshold. 0 = off (a human reviews all)."""
    from . import channels

    threshold = channel.auto_approve_score or 0.0
    if threshold <= 0:
        return 0
    now = datetime.now(timezone.utc)
    approved = 0
    with session_scope() as db:
        for video in _trustable(db, channel):
            score = (video.meta or {}).get("clip_score", 0.0)
            if score >= threshold:
                _grant(db, video, now, f"score {round(score, 2)}")
                db.add(GrowthEvent(kind="auto_approved", video_id=video.id, channel_id=channels.event_channel(channel),
                                   summary=f"Auto-approved (score {score:.2f} >= {threshold:.2f}): {(video.title or '')[:60]}"))
                approved += 1
    return approved


def backstop_approve(channel) -> Optional[str]:
    """Keep a channel posting: when a window would be missed with clips in review, approve its single best one."""
    from . import channels

    if not channel.cadence_backstop:
        return None
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        pending = _trustable(db, channel)
        if not pending:
            return None
        best = max(pending, key=lambda v: (v.meta or {}).get("clip_score", 0.0))
        _grant(db, best, now, "cadence backstop")
        db.add(GrowthEvent(kind="auto_approved", video_id=best.id, channel_id=channels.event_channel(channel),
                           summary=f"Cadence backstop approved the best waiting clip to keep posting: {(best.title or '')[:60]}"))
        return best.id


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


_DECISION_KEYS = ("owner_approved", "owner_rejected", "auto_approved")


def undo(video_id: str) -> dict:
    """Reverse a review decision, as long as the clip has not been posted yet. Puts it back in the queue."""
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None or not (video.meta or {}).get("clip_rendered"):
            raise ReviewError("no such clip")
        if video.youtube_video_id or video.status in (VideoStatus.PUBLISHING, VideoStatus.PUBLISHED):
            raise ReviewError("already posted — too late to undo")
        if video.status not in (VideoStatus.TRANSFORMED, VideoStatus.REJECTED):
            raise ReviewError(f"nothing to undo on a {video.status.value} clip")
        if not any(k in (video.meta or {}) for k in _DECISION_KEYS):
            raise ReviewError("this clip was not decided from the review page")
        # The autopilot is another thread and may have claimed the clip since the read above. Only this
        # UPDATE decides: writing PENDING over a claimed row would leave a clip both uploading and in review.
        taken_back = db.execute(
            update(Video).where(Video.id == video_id, Video.status == video.status, Video.youtube_video_id.is_(None))
            .values(status=VideoStatus.PENDING, transformed_at=None, curated_at=None)
            .execution_options(synchronize_session=False)
        ).rowcount
        if not taken_back:
            raise ReviewError("already posted — too late to undo")
        db.refresh(video)
        video.meta = {k: v for k, v in (video.meta or {}).items() if k not in _DECISION_KEYS}
        return {"id": video_id, "status": video.status.value}


def media_path(video_id: str) -> Optional[str]:
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None or not (video.meta or {}).get("clip_rendered"):
            return None
        path = video.transformed_path
        return path if path and os.path.exists(path) else None
