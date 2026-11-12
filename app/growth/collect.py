"""Pull YouTube's numbers for recent uploads."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.db import session_scope
from app.models import GrowthEvent, GrowthState, Video, VideoSnapshot, VideoStatus
from app.publishers.youtube_publisher import explain_api_error

from . import channels, safety
from .factors import aware
from .learn import note_once, set_state, upload_turned_down
from .youtube import account_mismatch, get_insights, insights_for

log = logging.getLogger(__name__)

_TRACK_FOR = timedelta(days=30)
_ANALYTICS_AFTER = timedelta(days=2)  # YouTube's reporting delay
_ANALYTICS_EVERY = timedelta(hours=24)
ANALYTICS_LASTING = timedelta(days=3)  # past a bad day for YouTube, so someone needs to look
_ANALYTICS_API = "YouTube Analytics API"
_HEALTH = "collect"


def _report_day(moment: datetime) -> date:
    """YouTube's reporting days run from midnight Pacific."""
    try:
        zone = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:  # Windows has no zone data of its own, and a start date is widened anyway
        zone = timezone(timedelta(hours=-8))
    return moment.astimezone(zone).date()


def _first_report_day(published: datetime) -> date:
    """A day early, which costs nothing: the video had no views then."""
    return _report_day(published) - timedelta(days=1)


def _uploads(db, since: datetime, scope=None) -> list[Video]:
    conds = [Video.status == VideoStatus.PUBLISHED, Video.youtube_video_id.isnot(None), Video.published_at >= since]
    if scope is not None:
        conds.append(scope)
    rows = db.execute(select(Video).where(*conds)).scalars().all()
    return [v for v in rows if not v.youtube_video_id.startswith("MOCK_")]


def _tracked(db, now: datetime, scope=None) -> list[Video]:
    return _uploads(db, now - _TRACK_FOR, scope)


def _watched(db, now: datetime, scope=None) -> list[Video]:
    """A takedown counts for as long as a strike does, long after the numbers stop mattering."""
    return _uploads(db, now - safety.STRIKE_WINDOW, scope)


def _record_stats(asked: set[str], stats: dict[str, dict], now: datetime, scope=None) -> int:
    """Only judges what YouTube was asked about: a clip published since then would look deleted."""
    recorded = 0
    with session_scope() as db:
        for video in (v for v in _watched(db, now, scope) if v.youtube_video_id in asked):
            info = stats.get(video.youtube_video_id)
            meta = dict(video.meta or {})
            if info is None:
                if not meta.get("yt_missing"):
                    db.add(GrowthEvent(kind="missing", video_id=video.id,
                                       summary=f"{video.youtube_video_id} is gone from YouTube: deleted or blocked."))
                meta["yt_missing"] = True
                video.meta = meta
                safety.record_strike(video, "missing", now=now)
                continue
            meta.pop("yt_missing", None)
            meta["yt_privacy"] = info["privacy"]
            status = info.get("upload_status", "")
            meta["yt_upload_status"] = status
            video.meta = meta
            safety.clear_missing(video)
            if upload_turned_down(status):
                reason = info.get("rejection_reason") or info.get("failure_reason") or status
                safety.record_strike(video, reason, now=now)
                continue
            if aware(video.published_at) < now - _TRACK_FOR:
                continue
            video.views, video.likes, video.comments = info["views"], info["likes"], info["comments"]
            age = (now - aware(video.published_at)).total_seconds() / 3600
            db.add(VideoSnapshot(video_id=video.id, taken_at=now, age_hours=age, views=info["views"],
                                 likes=info["likes"], comments=info["comments"]))
            recorded += 1
    return recorded


def _analytics_due(now: datetime, scope=None) -> list[tuple[str, str, datetime]]:
    with session_scope() as db:
        due = []
        for video in _tracked(db, now, scope):
            if (video.meta or {}).get("yt_missing"):
                continue
            published = aware(video.published_at)
            updated = (video.meta or {}).get("yt_analytics", {}).get("updated")
            fresh = updated and now - datetime.fromisoformat(updated) < _ANALYTICS_EVERY
            if now - published >= _ANALYTICS_AFTER and not fresh:
                due.append((video.id, video.youtube_video_id, published))
        return due


def _record_analytics(insights, now: datetime, scope=None) -> int:
    due = _analytics_due(now, scope)
    if not due:
        return 0
    start, end = _first_report_day(min(p for _, _, p in due)), _report_day(now)
    numbers = insights.retention([yt for _, yt, _ in due], start, end)
    shares = {}
    for _, yt_id, published in due:
        if yt_id in numbers:
            # Only for the record, so it must not cost the retention numbers already fetched.
            try:
                shares[yt_id] = insights.feed_share(yt_id, _first_report_day(published), end)
            except Exception as exc:
                log.warning("growth: no feed share for %s: %s", yt_id, exc)
    with session_scope() as db:
        recorded = 0
        for video_id, yt_id, _ in due:
            if yt_id not in numbers:
                continue
            video = db.get(Video, video_id)
            if video is None:
                continue
            video.meta = {**(video.meta or {}), "yt_analytics": {
                **numbers[yt_id], "shorts_feed_share": shares.get(yt_id), "updated": now.isoformat(),
            }}
            recorded += 1
    return recorded


def _health_key(channel=None) -> str:
    return _HEALTH if channel is None else channels.state_key(channel, _HEALTH)


def health(db, channel=None) -> dict:
    state = db.get(GrowthState, _health_key(channel))
    return {"last_success": None, "analytics_failing_since": None, "analytics_error": None,
            **(state.value if state else {})}


def _update_health(channel=None, **fields) -> None:
    with session_scope() as db:
        set_state(db, _health_key(channel), {**health(db, channel), **fields})


def analytics_warning(status: dict, now: datetime) -> Optional[str]:
    """None until it has lasted. Before that, retention being absent looks the same as YouTube's reporting delay."""
    since = status["analytics_failing_since"]
    if not since or now - datetime.fromisoformat(since) < ANALYTICS_LASTING:
        return None
    days = (now - datetime.fromisoformat(since)).days
    return f"Analytics has failed for {days} days, so the loop is learning from views alone: {status['analytics_error']}"


def _ev(channel) -> Optional[str]:
    return None if channel is None else channels.event_channel(channel)


def _scope(channel):
    return None if channel is None else channels.video_scope(channel)


def _note_stats_failure(exc: Exception, channel=None) -> None:
    with session_scope() as db:
        note_once(db, "collect_failed", f"Could not read video stats, so nothing was measured: {explain_api_error(exc)}",
                  key=channels.state_key(channel, "") if channel is not None else "", channel_id=_ev(channel))


def _note_analytics_failure(exc: Exception, now: datetime, channel=None) -> Optional[str]:
    """One failed run is logged. The return value is what the job should fail with, once it has lasted."""
    cause = explain_api_error(exc, api=_ANALYTICS_API)
    with session_scope() as db:
        status = {**health(db, channel), "analytics_error": cause}
        status["analytics_failing_since"] = status["analytics_failing_since"] or now.isoformat()
        set_state(db, _health_key(channel), status)
        lasting = analytics_warning(status, now)
        base = "lasting" if lasting else "failing"
        note_once(db, "analytics_failed", lasting or f"Could not read YouTube Analytics: {cause}",
                  key=channels.state_key(channel, base) if channel is not None else base, channel_id=_ev(channel))
    return lasting


def _collect_one(channel, now: datetime) -> dict:
    """Read one managed channel's numbers with its own token, and record them against its videos."""
    # The legacy channel uses the module-level get_insights (what the tests patch); a named one its own token.
    insights = get_insights() if (channel is None or channels.is_default(channel)) else insights_for(channel)
    if insights is None:
        return {"skipped": "no real YouTube connection"}
    scope = _scope(channel)
    with session_scope() as db:
        ids = [v.youtube_video_id for v in _watched(db, now, scope)]
    if not ids:
        return {"skipped": "nothing published recently"}
    try:
        # Through another account's token every one of these videos would read as deleted, and count as a strike.
        wrong = account_mismatch(channel, insights)
        if wrong:
            raise RuntimeError(wrong)
        recorded = _record_stats(set(ids), insights.stats(ids), now, scope)
    except Exception as exc:
        log.warning("growth: could not read video stats: %s", exc)
        _note_stats_failure(exc, channel)
        return {"error": f"video stats: {exc}"}
    _update_health(channel, last_success=now.isoformat())
    result: dict = {"snapshots": recorded}
    # Analytics adds to the signal, but views alone still drive the loop.
    try:
        result["analytics"] = _record_analytics(insights, now, scope)
        _update_health(channel, analytics_failing_since=None, analytics_error=None)
    except Exception as exc:
        log.warning("growth: analytics unavailable: %s", exc)
        result["analytics_error"] = str(exc)[:300]
        lasting = _note_analytics_failure(exc, now, channel)
        if lasting:
            result["error"] = lasting
    return result


def collect_job(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    with session_scope() as db:
        active = channels.active_channels(db)
    # A single legacy channel keeps the old flat result; several report per channel by slug.
    if len(active) == 1 and channels.is_default(active[0]):
        return _collect_one(active[0], now)
    return channels.for_each(active, lambda channel: _collect_one(channel, now))
