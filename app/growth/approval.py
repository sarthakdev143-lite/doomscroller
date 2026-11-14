"""The owner's approval step. While it is on, a clip goes out only after a person says yes.

Clips are cut into PENDING. Approving one moves it straight to TRANSFORMED, since a rendered clip
needs no transform, and the autopilot posts it in a later window. A title typed on approval
becomes an edited draft, which the autopilot posts as written rather than restyling.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select, update

from app.config import settings
from app.db import session_scope
from app.metadata import compose, finished_title, youtube_description
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


def awaiting_scraped(db) -> list[Video]:
    """Pending candidates from classic scrapers (non-clip rows), separate from growth clip stock."""
    rows = db.execute(
        select(Video)
        .where(Video.status == VideoStatus.PENDING)
        .order_by(Video.viral_score.desc(), Video.scraped_at.desc())
        .limit(_QUEUE_LIMIT * 3)
    ).scalars().all()
    out: list[Video] = []
    for v in rows:
        if (v.meta or {}).get("clip_rendered"):
            continue
        path = v.transformed_path or v.local_path
        if path and os.path.isfile(path):
            out.append(v)
            if len(out) >= _QUEUE_LIMIT:
                break
    return out


def _channel_of(db, channel_id) -> tuple[str, str]:
    """(display name, slug) for a clip's channel; empty for a legacy clip with none."""
    from . import channels

    channel = channels.get_channel(db, channel_id)
    return ((channel.name or channel.slug), channel.slug) if channel else ("", "")


def _clean_tags(raw_tags: Sequence[str]) -> list[str]:
    """Normalize owner-edited YouTube tags within YouTube's count and character caps."""
    tags: list[str] = []
    total = 0
    seen: set[str] = set()
    for raw in raw_tags:
        tag = re.sub(r"[^\w\s]", "", str(raw or ""), flags=re.UNICODE).strip().lower()
        tag = re.sub(r"\s+", " ", tag)[:30].strip()
        if len(tag) < 2 or tag in seen:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if len(tags) >= 15 or total + cost > 480:
            continue
        seen.add(tag)
        tags.append(tag)
        total += cost
    return tags


def _compose_for_video(video: Video, *, title_override: str = ""):
    meta = video.meta or {}
    is_clip = bool(meta.get("clip_rendered"))
    has_link = (video.source_url or "").startswith(("http://", "https://"))
    platform = "" if is_clip and video.source in ("generic", "local") else (video.source or "")
    source_url = (video.source_url or "") if (has_link or not is_clip) else ""
    require_credit = has_link if is_clip else settings.source_crop_active
    generated = compose(
        title=title_override or meta.get("llm_title") or video.title or "",
        description=meta.get("llm_description") or video.description or "",
        keywords=list(video.keywords or []),
        author=video.author or "",
        author_name=video.author_name or "",
        source_url=source_url,
        platform=platform,
        language=video.language or "en",
        require_credit=require_credit,
    )
    if not (video.author or "").strip() and (video.author_name or "").strip():
        credit_name = video.author_name.strip()
        if "Originally posted on " in generated.description:
            generated.description = youtube_description(
                generated.description.replace("Originally posted on ", f"Credit: {credit_name} on ", 1)
            )
        elif "Credit:" not in generated.description:
            tail = f"Credit: {credit_name}"
            if source_url and source_url not in generated.description:
                tail += f"\n{source_url}"
            generated.description = youtube_description(f"{generated.description}\n\n{tail}".strip())
    return generated


def _card(video: Video, db) -> dict:
    meta = video.meta or {}
    is_clip = bool(meta.get("clip_rendered"))
    draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video.id)).scalar_one_or_none()
    composed = _compose_for_video(video) if (draft is None or not draft.description or not draft.tags) else None
    description = (draft.description if draft and draft.description else (composed.description if composed else "")) or ""
    if draft is not None and draft.edited and draft.tags is not None:
        tags = list(draft.tags)
    else:
        tags = list(draft.tags if draft and draft.tags else (composed.tags if composed else []))
    name, slug = _channel_of(db, video.channel_id)
    raw_score = meta.get("clip_score") if is_clip else video.viral_score
    raw_start = meta.get("clip_start")
    raw_end = meta.get("clip_end")
    width = int(video.width) if video.width and video.width > 0 else 0
    height = int(video.height) if video.height and video.height > 0 else 0
    resolution = f"{width}x{height}" if (width and height) else ""
    approved_by = (
        "you" if meta.get("owner_approved")
        else (str(meta["auto_approved"]) if meta.get("auto_approved")
              else ("auto-queue" if video.status == VideoStatus.TRANSFORMED else ""))
    )
    return {
        "id": video.id,
        "title": (draft.title if draft and draft.title else video.title) or "",
        "description": description,
        "tags": tags,
        "edited": bool(draft and draft.edited),
        "scraped": not is_clip,
        "platform": meta.get("platform") or video.source or "",
        "hook": meta.get("clip_hook") or "",
        "seconds": video.duration_sec or 0,
        "width": width,
        "height": height,
        "resolution": resolution,
        "score": round(float(raw_score), 2) if isinstance(raw_score, (int, float)) else None,
        "start": round(float(raw_start), 1) if isinstance(raw_start, (int, float)) else None,
        "end": round(float(raw_end), 1) if isinstance(raw_end, (int, float)) else None,
        "creator": video.author_name or video.author or "",
        "source": video.source_url or "",
        "kind": meta.get("clip_kind") or ("" if is_clip else (video.source or "")),
        "why": meta.get("clip_reason") or "",
        "flag": meta.get("growth_flagged") or "",
        "audio": _audio_line(meta.get("earshot")),
        "channel": name,
        "channel_slug": slug,
        "approved_by": approved_by,
        "pinned": bool(meta.get("pinned_at")),
        "has_media": any(bool(p and os.path.isfile(p)) for p in (video.transformed_path, video.local_path)),
        "made": (video.scraped_at.isoformat() if video.scraped_at else ""),
    }


def _audio_line(earshot: Optional[dict]) -> str:
    """A one-line audio verdict for the card, e.g. 'launch-grade 95%' or 'needs work: honky mids'."""
    if not isinstance(earshot, dict) or not earshot.get("verdict"):
        return ""
    pct = f" {round(earshot['score'] * 100)}%" if isinstance(earshot.get("score"), (int, float)) else ""
    issues = earshot.get("issues") or []
    worst = next((i for i in issues if i.get("weight") in ("critical", "major")), None)
    tail = f": {worst['action'].split(':')[0]}" if worst and worst.get("action") else ""
    return f"{earshot['verdict']}{pct}{tail}"


_POSTED_LIMIT = 20


def _result(video: Video) -> dict:
    meta = video.meta or {}
    analytics = meta.get("yt_analytics") or {}
    diagnosis = meta.get("growth_diagnosis") or {}
    percent = analytics.get("averageViewPercentage")
    return {
        "id": video.id,
        "title": video.title or "",
        "url": f"https://youtube.com/shorts/{video.youtube_video_id}",
        "posted": video.published_at.isoformat() if video.published_at else "",
        "views": video.views or 0,
        "likes": video.likes or 0,
        "watched": round(float(percent)) if percent is not None else None,
        "verdict": diagnosis.get("code", ""),
        "why": diagnosis.get("reason", ""),
        "has_media": any(bool(p and os.path.isfile(p)) for p in (video.transformed_path, video.local_path)),
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
    from .autopilot import ready_clips

    with session_scope() as db:
        active = channels.active_channels(db)
        cards = [_card(v, db) for v in awaiting_review(db)]
        scraped = [_card(v, db) for v in awaiting_scraped(db)]
        queued = [
            _card(v, db)
            for v in ready_clips(db)
            if v.transformed_path and os.path.exists(v.transformed_path)
        ][:_QUEUE_LIMIT]
        per = [_channel_status(db, c) for c in active]
        posted = recent_posts(db)
    # A single legacy channel keeps the flat view; several add a per-channel status strip and filter chips.
    multi = len(active) > 1 or (len(active) == 1 and not channels.is_default(active[0]))
    return {
        "clips": cards,
        "scraped": scraped,
        "queued": queued,
        "channels": per if multi else [],
        "approved_waiting": sum(c["approved"] for c in per),
        "posted_today": sum(c["posted_today"] for c in per),
        "next_post": min((c["next_post"] for c in per if c["next_post"]), default=""),
        "posted": posted,
    }


def channels_overview() -> list[dict]:
    """Everything the control panel's Channels tab shows: each channel's config, its live counts, whether
    it is connected to YouTube, and the one next step to get it running. The legacy channel is included."""
    from . import channels

    out = []
    with session_scope() as db:
        for channel in channels.all_channels(db):
            default = channels.is_default(channel)
            token = channel.token_path
            connected = default or bool(token and os.path.exists(token) and channel.youtube_channel_id)
            out.append({
                **_channel_status(db, channel),
                "niche": channel.niche or "",
                "brand_name": (settings.brand_name if default else channel.brand_name) or "",
                "uploads_per_day": channels.uploads_per_day(channel),
                "timezone": channels.timezone_of(channel),
                "require_approval": channels.require_approval(channel),
                "auto_approve_score": float(channel.auto_approve_score or 0.0),
                "cadence_backstop": bool(channel.cadence_backstop),
                "enabled": channel.enabled,
                "default": default,
                "connected": connected,
                "feeds": channels.feeds_for(db, channel),
                "next_step": channels.next_step(db, channel),
            })
    return out


def export_snapshot() -> dict:
    """Sanitised channels + review queue snapshot for portable owner backup (never includes secrets/paths)."""
    sanitized_channels = []
    for ch in channels_overview():
        row = {k: v for k, v in ch.items() if k not in ("next_step", "client_secrets", "token_path")}
        sanitized_channels.append(row)

    raw_queue = queue()
    sanitized_queue = dict(raw_queue)
    for bucket in ("clips", "scraped", "queued"):
        items = []
        for card in raw_queue.get(bucket) or []:
            clean_card = dict(card)
            src = str(clean_card.get("source") or "")
            if src and not src.startswith(("http://", "https://")):
                clean_card["source"] = ""
            items.append(clean_card)
        sanitized_queue[bucket] = items

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "channels": sanitized_channels,
        "queue": sanitized_queue,
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


def _save_draft_edits(
    db,
    video: Video,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> MetadataDraft:
    draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video.id)).scalar_one_or_none()
    if draft is None:
        draft = MetadataDraft(video_id=video.id)
        db.add(draft)
    was_edited = bool(draft.edited)
    written_title = " ".join(title.split()) if title is not None else ""
    fallback = _compose_for_video(video, title_override=written_title)
    if title is not None:
        if not written_title:
            raise ValueError("title cannot be empty")
        draft.title = finished_title(written_title)
    elif not draft.title:
        draft.title = fallback.title

    if description is not None:
        clean_desc = youtube_description(description)
        if not clean_desc:
            raise ValueError("description cannot be empty")
        draft.description = clean_desc
    elif not draft.description:
        draft.description = fallback.description

    if tags is not None:
        draft.tags = _clean_tags(tags)
    elif not was_edited and not draft.tags:
        draft.tags = list(fallback.tags)

    if not draft.detected_language:
        draft.detected_language = fallback.detected_language or "en"
    draft.edited = True
    return draft


def approve(
    video_id: str,
    title: Optional[str] = None,
    *,
    description: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> dict:
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        video = _pending_clip(db, video_id)
        if not video.transformed_path or not os.path.exists(video.transformed_path):
            raise ReviewError("the clip's file is missing")
        video.status = VideoStatus.TRANSFORMED
        video.transformed_at = now
        video.curated_at = now
        video.meta = {**(video.meta or {}), "owner_approved": now.isoformat()}
        has_title = bool(" ".join((title or "").split()))
        has_desc = bool(youtube_description(description)) if description is not None else False
        if has_title or has_desc or tags is not None:
            _save_draft_edits(
                db,
                video,
                title=title if has_title else None,
                description=description if has_desc else None,
                tags=tags,
            )
        return {"id": video_id, "status": video.status.value}


def reject(video_id: str) -> dict:
    with session_scope() as db:
        video = _pending_clip(db, video_id)
        video.status = VideoStatus.REJECTED
        video.curated_at = datetime.now(timezone.utc)
        video.meta = {**(video.meta or {}), "owner_rejected": True}
        return {"id": video_id, "status": video.status.value}


def reject_flagged(channel_slug: str = "") -> dict:
    """Reject all flagged pending rendered clips in one batch (optionally scoped to one channel)."""
    from . import channels

    slug = (channel_slug or "").strip()
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        conds = [Video.status == VideoStatus.PENDING]
        if slug and slug != "all":
            channels._ensure_seeded(db)
            channel = channels.channel_by_slug(db, slug)
            if channel is None:
                raise ValueError(f"unknown channel '{slug}'")
            conds.append(channels.video_scope(channel))
        rows = db.execute(select(Video).where(*conds).order_by(Video.scraped_at.desc())).scalars().all()
        rejected_ids: list[str] = []
        for video in rows:
            meta = video.meta or {}
            if not meta.get("clip_rendered") or not meta.get("growth_flagged"):
                continue
            video.status = VideoStatus.REJECTED
            video.curated_at = now
            video.meta = {**meta, "owner_rejected": True}
            rejected_ids.append(video.id)
        return {"rejected": len(rejected_ids), "ids": rejected_ids}


def _approve_clean_in_session(
    db,
    *,
    channel_slug: str = "",
    min_score: Optional[float] = None,
) -> dict:
    from . import channels

    threshold: Optional[float] = None
    if min_score is not None:
        if isinstance(min_score, bool):
            raise ValueError("min_score must be a number between 0 and 1")
        try:
            parsed = float(min_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("min_score must be a number between 0 and 1") from exc
        if not math.isfinite(parsed) or parsed < 0.0 or parsed > 1.0:
            raise ValueError("min_score must be between 0 and 1")
        threshold = parsed

    slug = (channel_slug or "").strip()
    conds = [Video.status == VideoStatus.PENDING]
    if slug and slug != "all":
        channels._ensure_seeded(db)
        channel = channels.channel_by_slug(db, slug)
        if channel is None:
            raise ValueError(f"unknown channel '{slug}'")
        conds.append(channels.video_scope(channel))

    now = datetime.now(timezone.utc)
    rows = db.execute(select(Video).where(*conds).order_by(Video.scraped_at.desc())).scalars().all()
    approved_ids: list[str] = []
    for video in rows:
        meta = video.meta or {}
        if not meta.get("clip_rendered") or meta.get("growth_flagged"):
            continue
        if not video.transformed_path or not os.path.exists(video.transformed_path):
            continue
        if threshold is not None and threshold > 0.0:
            raw_score = meta.get("clip_score")
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)) or float(raw_score) < threshold:
                continue
        elif threshold == 0.0:
            raw_score = meta.get("clip_score")
            if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) and float(raw_score) < 0.0:
                continue
        video.status = VideoStatus.TRANSFORMED
        video.transformed_at = now
        video.curated_at = now
        video.meta = {**meta, "owner_approved": now.isoformat()}
        approved_ids.append(video.id)
    db.flush()
    return {"approved": len(approved_ids), "ids": approved_ids}


def approve_clean(
    db=None,
    channel_slug: str = "",
    min_score: Optional[float] = None,
) -> dict:
    """Approve all unflagged awaiting_review clips matching channel_slug and optional min_score."""
    if isinstance(db, str):
        channel_slug = db
        db = None
    if db is None:
        with session_scope() as owned_db:
            return _approve_clean_in_session(owned_db, channel_slug=channel_slug, min_score=min_score)
    return _approve_clean_in_session(db, channel_slug=channel_slug, min_score=min_score)


_DECISION_KEYS = ("owner_approved", "owner_rejected", "auto_approved")
# What a clip sheds when it leaves the shelf: the decision that put it there, and any "publish next" pin.
_SHELF_EXIT_KEYS = _DECISION_KEYS + ("pinned_at",)
_SCRAPED_DECISION_KEYS = ("owner_scraped_approved", "owner_scraped_rejected")


def curate_scraped(
    video_id: str,
    *,
    approved: bool,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> dict:
    """Approve (PENDING -> APPROVED for transform) or reject a classic scraped video."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None or (video.meta or {}).get("clip_rendered"):
            raise ReviewError("no such scraped video")
        if video.status != VideoStatus.PENDING:
            raise ReviewError(f"already {video.status.value}")
        if approved:
            path = video.transformed_path or video.local_path
            if not path or not os.path.isfile(path):
                raise ReviewError("the video's file is missing")
            video.status = VideoStatus.APPROVED
            video.curated_at = now
            video.meta = {**(video.meta or {}), "owner_scraped_approved": now.isoformat()}
            has_title = bool(" ".join((title or "").split()))
            has_desc = bool(youtube_description(description)) if description is not None else False
            if has_title or has_desc or tags is not None:
                _save_draft_edits(
                    db,
                    video,
                    title=title if has_title else None,
                    description=description if has_desc else None,
                    tags=tags,
                )
        else:
            video.status = VideoStatus.REJECTED
            video.curated_at = now
            video.meta = {**(video.meta or {}), "owner_scraped_rejected": True}
        return {"id": video_id, "status": video.status.value}


def _editable_video(db, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if video is None:
        raise ReviewError("no such clip")
    if video.youtube_video_id or video.status in (VideoStatus.PUBLISHING, VideoStatus.PUBLISHED):
        raise ReviewError("already posted — too late to edit metadata")
    if video.status not in (VideoStatus.PENDING, VideoStatus.APPROVED, VideoStatus.TRANSFORMED):
        raise ReviewError(f"cannot edit metadata on a {video.status.value} clip")
    return video


def update_metadata(
    video_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> dict:
    """Save owner-edited YouTube title, description/credit, and/or tags on an unposted clip."""
    if title is None and description is None and tags is None:
        raise ValueError("nothing to update")
    if title is not None and not " ".join(title.split()):
        raise ValueError("title cannot be empty")
    if description is not None and not youtube_description(description):
        raise ValueError("description cannot be empty")
    with session_scope() as db:
        video = _editable_video(db, video_id)
        draft = _save_draft_edits(db, video, title=title, description=description, tags=tags)
        return {
            "id": video_id,
            "title": draft.title,
            "description": draft.description,
            "tags": list(draft.tags or []),
            "edited": True,
        }


def update_title(video_id: str, title: str) -> dict:
    """Save an owner-edited YouTube title on an unposted clip without changing its status."""
    return update_metadata(video_id, title=title)


def reject_from_shelf(video_id: str) -> dict:
    """Reject a queued TRANSFORMED clip before autopilot claims it for upload."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None or not (video.meta or {}).get("clip_rendered"):
            raise ReviewError("no such clip")
        if video.youtube_video_id or video.status in (VideoStatus.PUBLISHING, VideoStatus.PUBLISHED):
            raise ReviewError("already posted — too late to reject")
        if video.status != VideoStatus.TRANSFORMED:
            raise ReviewError(f"clip is {video.status.value}, not queued on the shelf")
        rejected = db.execute(
            update(Video)
            .where(Video.id == video_id, Video.status == VideoStatus.TRANSFORMED, Video.youtube_video_id.is_(None))
            .values(status=VideoStatus.REJECTED, transformed_at=None, curated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if not rejected:
            raise ReviewError("already posted — too late to reject")
        db.refresh(video)
        cleaned = {k: v for k, v in (video.meta or {}).items() if k not in _SHELF_EXIT_KEYS}
        video.meta = {**cleaned, "owner_rejected": True}
        return {"id": video_id, "status": video.status.value}


def pin_clip_next(video_id: str, *, pinned: bool = True) -> dict:
    """Pin an unposted shelf clip as the next one to publish (or unpin it). Wins over the autopilot's own pick."""
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None or not (video.meta or {}).get("clip_rendered"):
            raise ReviewError("no such clip")
        if video.youtube_video_id or video.status in (VideoStatus.PUBLISHING, VideoStatus.PUBLISHED):
            raise ReviewError("already posted — too late to pin")
        if video.status != VideoStatus.TRANSFORMED:
            raise ReviewError(f"clip is {video.status.value}, not queued on the shelf")
        # The autopilot is another thread; only this guarded UPDATE says the clip is still unclaimed.
        held = db.execute(
            update(Video)
            .where(Video.id == video_id, Video.status == VideoStatus.TRANSFORMED, Video.youtube_video_id.is_(None))
            .values(status=VideoStatus.TRANSFORMED)
            .execution_options(synchronize_session=False)
        ).rowcount
        if not held:
            raise ReviewError("already posted — too late to pin")
        db.refresh(video)
        meta = dict(video.meta or {})
        if pinned:
            meta["pinned_at"] = datetime.now(timezone.utc).isoformat()
        else:
            meta.pop("pinned_at", None)
        video.meta = meta
        return {"id": video_id, "pinned": bool(pinned)}


def undo(video_id: str) -> dict:
    """Reverse a review decision or pull an unposted shelf clip back into Review."""
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None:
            raise ReviewError("no such clip")
        meta = video.meta or {}
        if not meta.get("clip_rendered"):
            if not any(k in meta for k in _SCRAPED_DECISION_KEYS):
                raise ReviewError("no such clip")
            if video.youtube_video_id or video.status in (VideoStatus.PUBLISHING, VideoStatus.PUBLISHED):
                raise ReviewError("already posted — too late to undo")
            if video.status not in (VideoStatus.APPROVED, VideoStatus.REJECTED):
                raise ReviewError(f"nothing to undo on a {video.status.value} video")
            taken_back = db.execute(
                update(Video)
                .where(Video.id == video_id, Video.status == video.status, Video.youtube_video_id.is_(None))
                .values(status=VideoStatus.PENDING, curated_at=None)
                .execution_options(synchronize_session=False)
            ).rowcount
            if not taken_back:
                raise ReviewError("already transformed — too late to undo")
            db.refresh(video)
            video.meta = {k: v for k, v in (video.meta or {}).items() if k not in _SCRAPED_DECISION_KEYS}
            return {"id": video_id, "status": video.status.value}
        if video.youtube_video_id or video.status in (VideoStatus.PUBLISHING, VideoStatus.PUBLISHED):
            raise ReviewError("already posted — too late to undo")
        if video.status not in (VideoStatus.TRANSFORMED, VideoStatus.REJECTED):
            raise ReviewError(f"nothing to undo on a {video.status.value} clip")
        if video.status == VideoStatus.REJECTED and not any(k in meta for k in _DECISION_KEYS):
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
        video.meta = {k: v for k, v in (video.meta or {}).items() if k not in _SHELF_EXIT_KEYS}
        return {"id": video_id, "status": video.status.value}


def media_path(video_id: str) -> Optional[str]:
    with session_scope() as db:
        video = db.get(Video, video_id)
        if video is None:
            return None
        for path in (video.transformed_path, video.local_path):
            if path and os.path.isfile(path):
                return path
        return None
