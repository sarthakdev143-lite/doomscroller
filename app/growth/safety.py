"""What a takedown or an account problem stops, and who gets to start it again."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import object_session

from app.db import session_scope
from app.models import GrowthEvent, GrowthState, Video, VideoStatus

from .factors import author_names
from .learn import set_state

# YouTube acting on the content or the account. A wrong pause costs one resume; posting on through these can cost the channel.
_HARD_REASONS = frozenset({"copyright", "claim", "termsofuse", "legal", "inappropriate", "trademark",
                           "uploaderaccountsuspended", "uploaderaccountclosed"})
STRIKE_WINDOW = timedelta(days=90)
_HARD_LIMIT, _SOFT_LIMIT = 1, 3
_HOLD = timedelta(hours=1)
_RESUME = "scripts/growth.py resume"
_UNBLOCK = "scripts/growth.py unblock"


def _value(db, key: str) -> Optional[dict]:
    state = db.get(GrowthState, key)
    return dict(state.value) if state else None


def _ckey(channel, name: str) -> str:
    """A pause/hold/strikes key for a channel. The legacy channel (or None) keeps the bare key."""
    if channel is None:
        return name
    from . import channels
    return channels.state_key(channel, name)


def _vkey(db, video: Video, name: str) -> str:
    """The key for the channel a struck video belongs to, so one channel's strike pauses only that channel."""
    from . import channels
    channel = channels.get_channel(db, video.channel_id) if video.channel_id else None
    return _ckey(channel, name)


def _blocks(db) -> dict[str, dict]:
    """One block per struck video, so taking one away leaves whatever another still blocks."""
    return dict((_value(db, "quarantine") or {}).get("videos", {}))


def blocked_parents(db) -> set[str]:
    return {b["parent"] for b in _blocks(db).values() if b["parent"]}


def blocked_sources(db) -> set[str]:
    return {b["source"] for b in _blocks(db).values() if b["source"]}


def blocked_authors(db) -> set[str]:
    return {name for b in _blocks(db).values() for name in b["authors"]}


def block_list(db) -> list[dict]:
    return sorted(_blocks(db).values(), key=lambda b: b["at"])


def describe_blocks(blocks: list[dict]) -> str:
    names = sorted({name for b in blocks for name in b["authors"]} or {b["source"] or "?" for b in blocks})
    return f"{', '.join(names)}. Their feeds are skipped. Lift with: {_UNBLOCK} <name>"


def _quarantine(db, video: Video, reason: str, now: datetime) -> None:
    parent = (video.meta or {}).get("clip_parent")
    block = {"video_id": video.id, "reason": reason, "at": now.isoformat(), "parent": parent,
             "source": video.source_url, "authors": list(author_names(video))}
    set_state(db, "quarantine", {"videos": {**_blocks(db), video.id: block}})
    if not parent:
        return
    unposted = db.execute(select(Video).where(
        Video.source == video.source, Video.status.not_in((VideoStatus.PUBLISHED, VideoStatus.REJECTED)),
    )).scalars().all()
    for row in unposted:
        if (row.meta or {}).get("clip_parent") == parent:
            row.meta = {**(row.meta or {}), "growth_quarantined": reason}


def _lift(db, video_ids: list[str]) -> None:
    blocks = _blocks(db)
    for video_id in video_ids:
        blocks.pop(video_id, None)
    set_state(db, "quarantine", {"videos": blocks})
    still_blocked = {b["parent"] for b in blocks.values()}
    held = db.execute(select(Video).where(Video.meta["growth_quarantined"].as_string().isnot(None))).scalars().all()
    for row in held:
        if row.meta.get("clip_parent") not in still_blocked:
            row.meta = {k: v for k, v in row.meta.items() if k != "growth_quarantined"}


def clear_missing(video: Video) -> None:
    """YouTube shows the video again, so it was never gone. A pause it caused is still the owner's to clear."""
    earlier = (video.meta or {}).get("growth_strike")
    if not earlier or earlier["reason"] != "missing":
        return
    db = object_session(video)
    video.meta = {k: v for k, v in video.meta.items() if k != "growth_strike"}
    _lift(db, [video.id])
    key = _vkey(db, video, "strikes")
    logged = (_value(db, key) or {}).get("log", [])
    set_state(db, key, {"log": [s for s in logged if s["video_id"] != video.id]})
    db.add(GrowthEvent(kind="strike_cleared", video_id=video.id, channel_id=video.channel_id,
                       summary=f"{video.youtube_video_id} is back on YouTube, so its strike and block are gone."))
    db.flush()


def unblock(name: str) -> int:
    """The owner's call. Any block naming this author, source or parent goes, and the strike behind it still counts."""
    wanted = name.casefold()
    with session_scope() as db:
        matched = [b["video_id"] for b in _blocks(db).values()
                   if wanted in {*b["authors"], (b["source"] or "").casefold(), (b["parent"] or "").casefold()}]
        if matched:
            _lift(db, matched)
            db.add(GrowthEvent(kind="unblocked", summary=f"The owner lifted {len(matched)} block(s) on {name}."))
    return len(matched)


def _pause_for(strikes: list[dict]) -> Optional[str]:
    hard = [s for s in strikes if s["hard"]]
    if len(hard) >= _HARD_LIMIT:
        return f"YouTube took action against a video: {hard[-1]['reason']}"
    if len(strikes) >= _SOFT_LIMIT:
        return f"{len(strikes)} videos went missing or were rejected in {STRIKE_WINDOW.days} days"
    return None


def record_strike(video: Video, reason: str, *, now: Optional[datetime] = None) -> None:
    """A video went missing or YouTube rejected it. Runs in the session that loaded the video, and is safe to repeat."""
    db = object_session(video)
    now = now or datetime.now(timezone.utc)
    hard = reason.casefold() in _HARD_REASONS
    earlier = (video.meta or {}).get("growth_strike")
    # Collect sees the same video every few hours, and that must not undo a resume.
    if earlier and (earlier["hard"] or not hard):
        return
    video.meta = {**(video.meta or {}), "growth_strike": {"reason": reason, "hard": hard, "at": now.isoformat()}}
    _quarantine(db, video, reason, now)
    db.add(GrowthEvent(kind="strike", video_id=video.id, channel_id=video.channel_id,
                       data={"reason": reason, "hard": hard},
                       summary=f"{video.youtube_video_id} has a problem: {reason}. Its source and its author's "
                               f"feeds are off the shelf. Lift with: {_UNBLOCK} <name>"))

    cutoff = now - STRIKE_WINDOW
    strikes_key, paused_key = _vkey(db, video, "strikes"), _vkey(db, video, "paused")
    logged = (_value(db, strikes_key) or {}).get("log", [])
    strikes = [s for s in logged if s["video_id"] != video.id and datetime.fromisoformat(s["at"]) > cutoff]
    strikes.append({"video_id": video.id, "reason": reason, "hard": hard, "at": now.isoformat()})
    set_state(db, strikes_key, {"log": strikes})
    reason_to_pause = _pause_for(strikes)
    if reason_to_pause and db.get(GrowthState, paused_key) is None:
        set_state(db, paused_key, {"reason": reason_to_pause, "at": now.isoformat()})
        db.add(GrowthEvent(kind="paused", video_id=video.id, channel_id=video.channel_id,
                           summary=f"Posting paused: {reason_to_pause}. Clear it with: {_RESUME}"))
    # One collect can bring several strikes, and db.get cannot see a row that is still pending.
    db.flush()


def pause_reason(db, channel=None) -> Optional[str]:
    """Why this channel's posting is paused, or None. A strike pauses only the channel it struck."""
    return (_value(db, _ckey(channel, "paused")) or {}).get("reason")


def _paused_keys(db, slug: Optional[str]) -> list[str]:
    from . import channels

    if slug:
        channel = channels.channel_by_slug(db, slug)
        return [channels.state_key(channel, "paused")] if channel else []
    # No slug: every channel (the legacy bare key plus each named one).
    return ["paused"] + [channels.state_key(c, "paused") for c in channels.all_channels(db)
                         if not channels.is_default(c)]


def resume(slug: Optional[str] = None) -> bool:
    """The owner's call, once they have looked at why it stopped. With a slug, resumes only that channel.

    Strikes still count until they age out.
    """
    cleared = 0
    with session_scope() as db:
        for key in _paused_keys(db, slug):
            state = db.get(GrowthState, key)
            if state is None:
                continue
            db.delete(state)
            db.add(GrowthEvent(kind="resumed", summary=f"The owner cleared the pause: {state.value.get('reason')}"))
            cleared += 1
    return cleared > 0


def active_hold(db, now: datetime, channel=None) -> Optional[dict]:
    hold = _value(db, _ckey(channel, "hold"))
    return hold if hold and now < datetime.fromisoformat(hold["until"]) else None


def set_hold(db, now: datetime, reason: Optional[str], channel=None) -> None:
    """The next clip would fail the same way. The row stays after the hour, so a long outage logs one event."""
    key = _ckey(channel, "hold")
    if db.get(GrowthState, key) is None:
        from . import channels
        cid = channels.event_channel(channel) if channel is not None else None
        db.add(GrowthEvent(kind="account_hold", channel_id=cid,
                           summary=f"Posting held by an account problem: {reason}"))
    set_state(db, key, {"until": (now + _HOLD).isoformat(), "reason": reason})


def clear_hold(db, channel=None) -> None:
    state = db.get(GrowthState, _ckey(channel, "hold"))
    if state is not None:
        db.delete(state)
