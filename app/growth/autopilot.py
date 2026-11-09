"""Decide when to post and which clip goes out, then post it."""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import numpy as np
from sqlalchemy import or_, select

from app import quota
from app.config import settings
from app.db import session_scope
from app.models import GrowthEvent, GrowthState, MetadataDraft, Video, VideoStatus
from app.publishers import MockPublisher, get_publisher

from . import channels, safety, titles
from .factors import CLIP_FACTORS, SLOTS, aware, channel_zone, clip_arms, published_arms
from .learn import build_model, matured_results, set_state
from .model import Draw

log = logging.getLogger(__name__)

_ATTEMPTS_PER_POST = 3
_SAVE_ATTEMPTS = 3
_FOCUS_BOOST = 0.25
_MAX_EXPLORE_RATE = 0.9
_WINDOW_MARGIN = timedelta(minutes=30)  # room for the next tick, so a post lands in its own window
_LATE_GRACE = timedelta(hours=2)  # a post this far past its window is dropped: it would test the wrong slot
_lock = threading.Lock()
# A rehearsal under USE_MOCK says nothing about the channel, so only these count as having gone out.
_REALLY_POSTED = (Video.status == VideoStatus.PUBLISHED,
                  or_(Video.youtube_video_id.is_(None), ~Video.youtube_video_id.startswith("MOCK_", autoescape=True)))


# A channel of None means the legacy whole-DB behaviour, which is what the direct unit tests exercise.
def _skey(channel, name: str) -> str:
    return name if channel is None else channels.state_key(channel, name)


def _scope(channel):
    return None if channel is None else channels.video_scope(channel)


def _uploads_per_day(channel) -> int:
    return settings.growth_uploads_per_day if channel is None else channels.uploads_per_day(channel)


def _really_posted(channel) -> list:
    """The REALLY-posted conditions, limited to one channel when given."""
    conds = list(_REALLY_POSTED)
    scope = _scope(channel)
    if scope is not None:
        conds.append(scope)
    return conds


def _ev_channel(channel) -> Optional[str]:
    """The channel_id to stamp on a growth event (None for the legacy channel, as before)."""
    return None if channel is None else channels.event_channel(channel)


def _publisher(channel):
    """The legacy channel uses the global publisher (live from settings); a named one its own token."""
    if channel is None or channels.is_default(channel):
        return get_publisher()
    return channels.publisher_for(channel)


@dataclass
class Policy:
    draw: Draw
    tried: dict[str, dict[str, int]]
    rate: float
    focus: frozenset[str]
    rng: random.Random


def started_at(db, channel=None) -> Optional[datetime]:
    """When the first real post went out. A stamp naming no video came from a render or a rehearsal."""
    state = db.get(GrowthState, _skey(channel, "started"))
    if state is None or "video_id" not in state.value:
        return None
    return datetime.fromisoformat(state.value["at"])


def _start_clock(db, channel=None) -> None:
    if started_at(db, channel) is not None:
        return
    posted = db.execute(select(Video).where(*_really_posted(channel), Video.youtube_video_id.isnot(None),
                                            Video.published_at.isnot(None))
                        .order_by(Video.published_at)).scalars().all()
    # The loop stamps what it posts. A clip published by hand before it started tested nothing.
    first = next((v for v in posted if (v.meta or {}).get("growth_title_style")), None)
    if first is not None:
        set_state(db, _skey(channel, "started"), {"at": aware(first.published_at).isoformat(), "video_id": first.id})


def explore_rate(started: datetime, now: datetime) -> float:
    """Test hard for the first month, then spend most uploads on what the data favours."""
    day = (now - started).total_seconds() / 86400
    return settings.growth_explore_rate if day < settings.growth_explore_days else settings.growth_settled_explore_rate


def policy(now: Optional[datetime] = None, rng: Optional[random.Random] = None, channel=None) -> Policy:
    now = now or datetime.now(timezone.utc)
    rng = rng or random.Random()
    with session_scope() as db:
        results = matured_results(db, scope=_scope(channel))
        started = started_at(db, channel)
        rate = explore_rate(started, now) if started else settings.growth_explore_rate
        focus = db.get(GrowthState, _skey(channel, "focus"))
        focus_factors = frozenset((focus.value or {}).get("factors", [])) if focus else frozenset()
        # Count what is still maturing too, or the same arm gets picked until its first result lands.
        tried: dict[str, dict[str, int]] = {}
        published = db.execute(select(Video).where(*_really_posted(channel), Video.published_at.isnot(None))).scalars().all()
        for video in published:
            if (video.meta or {}).get("clip_rendered"):
                for factor, arm in published_arms(video).items():
                    tried.setdefault(factor, {}).setdefault(arm, 0)
                    tried[factor][arm] += 1
    draw = build_model(results).draw(np.random.default_rng(rng.getrandbits(32)))
    return Policy(draw, tried, rate, focus_factors, rng)


def pick_arm(current: Policy, factor: str, arms: Sequence[str]) -> str:
    """Least-tried arm on an exploring turn, otherwise whichever the draw favours."""
    rate = current.rate + (_FOCUS_BOOST if factor in current.focus else 0.0)
    if current.rng.random() < min(rate, _MAX_EXPLORE_RATE):
        tried = current.tried.get(factor, {})
        fewest = min(tried.get(a, 0) for a in arms)
        return current.rng.choice([a for a in arms if tried.get(a, 0) == fewest])
    return max(arms, key=lambda a: current.draw.arm(factor, a))


def _day_bounds(local_day: datetime) -> list[tuple[str, datetime, datetime]]:
    windows = []
    for index, slot in enumerate(SLOTS):
        start = local_day.replace(hour=index * 4, minute=0, second=0, microsecond=0)
        windows.append((slot, start, start + timedelta(hours=4)))
    return windows


def build_plan(current: Policy, now: datetime, channel=None) -> dict:
    """Today's posts: distinct windows until there are more posts than windows, at a random minute in each."""
    local = now.astimezone(channel_zone(channel))
    windows = [w for w in _day_bounds(local) if w[2] > local]
    by_slot = {slot: (start, end) for slot, start, end in windows}
    posts, pool = [], list(by_slot)
    uploads = _uploads_per_day(channel)
    # Started late in the day, the remaining windows get their usual share rather than the whole day's posts.
    per_window = math.ceil(uploads / len(SLOTS))
    for _ in range(min(uploads, len(by_slot) * per_window)):
        pool = pool or list(by_slot)
        slot = pick_arm(current, "slot", pool)
        pool.remove(slot)
        start, end = by_slot[slot]
        earliest = max(start, local)
        room = max(0.0, (end - _WINDOW_MARGIN - earliest).total_seconds())
        due = earliest + timedelta(seconds=current.rng.uniform(0, room))
        posts.append({"slot": slot, "due": due.astimezone(timezone.utc).isoformat(),
                      "ends": end.astimezone(timezone.utc).isoformat(), "state": "waiting", "attempts": 0})
    return {"day": local.date().isoformat(), "posts": sorted(posts, key=lambda p: p["due"])}


def ready_clips(db, scope=None) -> list[Video]:
    """Only finished clips. Scraped reposts never go out through this loop, nor anything from a source with a takedown.

    `scope` is an optional SQLAlchemy filter limiting the clips to one managed channel.
    """
    conds = [Video.status == VideoStatus.TRANSFORMED, Video.transformed_path.isnot(None)]
    if scope is not None:
        conds.append(scope)
    rows = db.execute(select(Video).where(*conds).order_by(Video.transformed_at)).scalars().all()
    blocked = safety.blocked_parents(db)  # a job still running can finish a clip after the strike
    return [v for v in rows if (v.meta or {}).get("clip_rendered") and not (v.meta or {}).get("growth_quarantined")
            and (v.meta or {}).get("clip_parent") not in blocked]


def _screen(db, clips: Sequence[Video], now: datetime) -> list[Video]:
    """The last gate before an upload. Auto-queued clips never sat in PENDING, and filters change while clips wait."""
    from tasks.pipeline import clip_gate_reason

    passed = []
    for video in clips:
        # Only a review sets curated_at on a clip, and a re-run never undoes a decision.
        reason = None if video.curated_at else clip_gate_reason(video)
        if reason is None:
            passed.append(video)
            continue
        video.status = VideoStatus.REJECTED
        video.curated_at = now
        video.meta = {**(video.meta or {}), "auto_rejected": reason}
        db.add(GrowthEvent(kind="auto_rejected", video_id=video.id, summary=f"{(video.title or '')[:60]}: {reason}."))
    return passed


def pick_clip(clips: Sequence[Video], current: Policy, last_parent: Optional[str]) -> Video:
    """Two clips from one source in a row would read as one video and blur the test."""
    from tasks.pipeline import least_failed

    clips = least_failed(clips)
    pool = [c for c in clips if (c.meta or {}).get("clip_parent") != last_parent] or list(clips)
    focused = [f for f in CLIP_FACTORS if f in current.focus]
    if current.rng.random() < min(current.rate + (_FOCUS_BOOST if focused else 0.0), _MAX_EXPLORE_RATE):
        factor = current.rng.choice(focused or CLIP_FACTORS)
        held = {clip_arms(c).get(factor) for c in pool} - {None}
        if held:
            tried = current.tried.get(factor, {})
            fewest = min(tried.get(a, 0) for a in held)
            narrowed = [c for c in pool if tried.get(clip_arms(c).get(factor), 0) == fewest
                        and clip_arms(c).get(factor) is not None]
            pool = narrowed or pool
    return max(pool, key=lambda c: current.draw.score(clip_arms(c), CLIP_FACTORS))


def _posted_for_pacing(channel=None) -> tuple:
    """A rehearsal paces itself like the real thing, but its posts say nothing about real uploads."""
    if settings.use_mock:
        conds = [Video.status == VideoStatus.PUBLISHED]
        scope = _scope(channel)
        if scope is not None:
            conds.append(scope)
        return tuple(conds)
    return tuple(_really_posted(channel))


def _gap_is_clear(db, now: datetime, channel=None) -> bool:
    from tasks.pipeline import publish_claim_is_stale

    # A manual upload still running will be a post any minute now. Each channel paces on its own uploads.
    conds = [Video.status == VideoStatus.PUBLISHING]
    scope = _scope(channel)
    if scope is not None:
        conds.append(scope)
    publishing = db.execute(select(Video).where(*conds)).scalars().all()
    if not all(publish_claim_is_stale(v, now) for v in publishing):
        return False
    last = db.execute(select(Video.published_at).where(*_posted_for_pacing(channel))
                      .order_by(Video.published_at.desc()).limit(1)).scalar_one_or_none()
    return last is None or now - aware(last) >= timedelta(minutes=settings.growth_min_gap_minutes)


def _post_id(post: dict) -> str:
    return f"{post['slot']}@{post['due']}"


def _publish_next(current: Policy, now: datetime, post_id: str, channel=None) -> dict:
    from tasks.pipeline import count_publish_failure

    with session_scope() as db:
        clips = _screen(db, ready_clips(db, _scope(channel)), now)
        if not clips:
            return {"retry": True, "skipped": "no finished clips in stock"}
        last_parent = db.execute(select(Video).where(*_posted_for_pacing(channel))
                                 .order_by(Video.published_at.desc()).limit(1)).scalar_one_or_none()
        last_parent = (last_parent.meta or {}).get("clip_parent") if last_parent else None
        video = pick_clip(clips, current, last_parent)
        video_id, title, spoken = video.id, video.title or "", (video.meta or {}).get("clip_opening_line", "")

    try:
        return _send_clip(current, video_id, title, spoken, post_id)
    except Exception as exc:
        log.exception("growth: the publish step failed for %s", video_id[:8])
        count_publish_failure(video_id)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _send_clip(current: Policy, video_id: str, title: str, spoken: str, post_id: str) -> dict:
    from tasks.pipeline import generate_metadata_for_video, publish_task

    style = pick_arm(current, "title_style", titles.available_styles())
    generate_metadata_for_video(video_id)
    with session_scope() as db:
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video_id)).scalar_one_or_none()
        edited = draft is not None and draft.edited
    # A title someone wrote by hand wins, and is not a test of anything.
    final, actual = (None, "plain") if edited else titles.styled_title(style, title, spoken)
    with session_scope() as db:
        video = db.get(Video, video_id)
        video.meta = {**(video.meta or {}), "growth_title_style": actual, "growth_title": final,
                      "growth_post": post_id}
    result = publish_task(video_id, meta={"title": final} if final else {})
    return {**result, "title_style": actual}


def fill_post(video_id: str, sent_at: datetime) -> None:
    """An upload found after a crash was this post, which would otherwise send a second clip."""
    with session_scope() as db:
        video = db.get(Video, video_id)
        channel = channels.get_channel(db, video.channel_id) if video and video.channel_id else None
        key = _skey(channel, "plan")
        state = db.get(GrowthState, key)
        posts = [dict(p) for p in (state.value if state else {}).get("posts", [])]
        for post in posts:
            if post["state"] == "waiting" and (
                    datetime.fromisoformat(post["due"]) <= sent_at <= datetime.fromisoformat(post["ends"]) + _LATE_GRACE):
                post.update(state="posted", video_id=video_id)
                set_state(db, key, {**state.value, "posts": posts})
                return


def _posted_event(post: dict, video_id: Optional[str], style: Optional[str], channel=None) -> GrowthEvent:
    return GrowthEvent(kind="posted", video_id=video_id, channel_id=_ev_channel(channel),
                       summary=f"Posted in window {post['slot']} with a {style} title.")


def _fill_lost_posts(db, posts: list[dict], channel=None) -> None:
    """The upload is what happened. A plan save lost after it must not send a second clip for the same post."""
    waiting = {_post_id(p): p for p in posts if p["state"] == "waiting"}
    if not waiting:
        return
    conds = [Video.status == VideoStatus.PUBLISHED, Video.meta["growth_post"].as_string().in_(list(waiting))]
    scope = _scope(channel)
    if scope is not None:
        conds.append(scope)
    rows = db.execute(select(Video).where(*conds)).scalars().all()
    filled = {p.get("video_id") for p in posts}
    for video in rows:
        post = waiting[video.meta["growth_post"]]
        if video.id not in filled and post["state"] == "waiting":
            post.update(state="posted", video_id=video.id)
            db.add(_posted_event(post, video.id, video.meta.get("growth_title_style"), channel))


def _note_missed(db, post: dict, channel=None) -> None:
    post["state"] = "missed"
    why = post.get("last_error") or "the loop did not get to it in time, so the app may have been off or paused"
    db.add(GrowthEvent(kind="missed", data={"slot": post["slot"], "due": post["due"]}, channel_id=_ev_channel(channel),
                       summary=f"Missed window {post['slot']}: {why}"))


def _past_grace(post: dict, now: datetime) -> bool:
    return now > datetime.fromisoformat(post["ends"]) + _LATE_GRACE


def _roll_over(db, old_posts: list[dict], current: Policy, now: datetime, channel=None) -> tuple[dict, list[dict]]:
    """A new day's plan, plus whatever of yesterday's is still inside its grace."""
    plan = build_plan(current, now, channel)
    db.add(GrowthEvent(kind="plan", channel_id=_ev_channel(channel),
                       summary="Today's windows: " + ", ".join(p["slot"] for p in plan["posts"]), data=plan))
    carried = []
    for post in (p for p in old_posts if p["state"] == "waiting"):
        if _past_grace(post, now):
            _note_missed(db, post, channel)
        else:
            carried.append(post)
    return plan, sorted(carried + [dict(p) for p in plan["posts"]], key=lambda p: p["due"])


def _settle_post(post: dict, outcome: dict) -> None:
    if outcome.get("ok"):
        post["attempts"] += 1
        post.update(state="posted", video_id=outcome.get("video_id"))
        return
    post["last_error"] = outcome.get("error") or outcome.get("skipped")
    # None of these say anything about this window's clip. If they last, the late grace ends the window.
    if outcome.get("account_level") or outcome.get("retry") or outcome.get("retryable"):
        return
    post["attempts"] += 1
    if post["attempts"] >= _ATTEMPTS_PER_POST:
        post["state"] = "failed"


def _save_outcome(plan: dict, posts: list[dict], post: dict, outcome: dict, now: datetime, channel=None) -> None:
    """Retried because the upload has already happened. If it never lands, the next tick works it out from the row."""
    for attempt in range(1, _SAVE_ATTEMPTS + 1):
        try:
            with session_scope() as db:
                set_state(db, _skey(channel, "plan"), {**plan, "posts": posts})
                if outcome.get("ok"):
                    safety.clear_hold(db)
                    db.add(_posted_event(post, outcome.get("video_id"), outcome.get("title_style"), channel))
                elif outcome.get("account_level"):
                    safety.set_hold(db, now, outcome.get("error"))
                elif post["state"] == "failed":
                    db.add(GrowthEvent(kind="publish_failed", channel_id=_ev_channel(channel),
                                       summary=f"Gave up on window {post['slot']}: {outcome.get('error')}"))
            return
        except Exception as exc:
            log.warning("growth: saving the plan failed (attempt %d): %s", attempt, exc)
            if attempt < _SAVE_ATTEMPTS:
                time.sleep(attempt)
    log.error("growth: the plan could not be saved after window %s: %s", post["slot"], outcome)


def _cannot_upload(channel=None) -> Optional[dict]:
    """Asked before any money goes on a title. Whatever the answer, it is not the clip's fault."""
    from tasks.pipeline import classify_publish_error

    try:
        _publisher(channel).check_ready()
    except Exception as exc:
        return {"ok": False, "error": str(exc), **classify_publish_error(exc)}
    return None


def _held_back(db, now: datetime, channel=None) -> Optional[dict]:
    """Why a due post cannot go on this tick."""
    hold = safety.active_hold(db, now)
    if hold:
        return {"skipped": f"on hold until {hold['until']}: {hold['reason']}"}
    if not _gap_is_clear(db, now, channel):
        return {"waiting": "minimum gap between uploads"}
    if quota.remaining(db) <= 0:
        return {"skipped": "daily upload cap reached"}
    return None


def _recover_stuck(now: datetime) -> None:
    from tasks.pipeline import recover_stuck_publishes

    try:
        recover_stuck_publishes(now)
    except Exception as exc:
        log.warning("publish recovery failed: %s", exc)


def _autopilot_one(channel, now: datetime, rng: Optional[random.Random]) -> dict:
    """Plan and post for one managed channel, to its own token at its own pace."""
    if not settings.use_mock and isinstance(_publisher(channel), MockPublisher):
        return {"error": "YOUTUBE_CLIENT_SECRETS is not set, so uploads would be fake"}
    with session_scope() as db:
        _start_clock(db, channel)
        paused = safety.pause_reason(db)
    if paused:
        return {"skipped": f"paused until the owner resumes: {paused}"}
    current = policy(now, rng, channel)
    today = now.astimezone(channel_zone(channel)).date().isoformat()
    key = _skey(channel, "plan")
    with session_scope() as db:
        state = db.get(GrowthState, key)
        plan = dict(state.value) if state else {}
        posts = [dict(p) for p in plan.get("posts", [])]
        _fill_lost_posts(db, posts, channel)
        if plan.get("day") != today:
            plan, posts = _roll_over(db, posts, current, now, channel)
        for post in posts:
            if post["state"] == "waiting" and _past_grace(post, now):
                _note_missed(db, post, channel)
        due = next((p for p in posts if p["state"] == "waiting" and datetime.fromisoformat(p["due"]) <= now), None)
        held = _held_back(db, now, channel) if due else None
        if held:  # kept on the post, so that a missed window can say why
            due["last_error"] = next(iter(held.values()))
        set_state(db, key, {**plan, "posts": posts})
        if due is None:
            return {"waiting": sum(p["state"] == "waiting" for p in posts), "day": today}
        if held:
            return held

    try:
        outcome = _cannot_upload(channel) or _publish_next(current, now, _post_id(due), channel)
    except Exception as exc:
        log.exception("growth: the publish step failed")
        outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _settle_post(due, outcome)
    _save_outcome(plan, posts, due, outcome, now, channel)
    if outcome.get("skipped"):
        return {"skipped": outcome["skipped"]}
    return {k: outcome[k] for k in ("ok", "video_id", "youtube_id", "error", "title_style") if k in outcome}


def autopilot_job(now: Optional[datetime] = None, rng: Optional[random.Random] = None) -> dict:
    if not settings.growth_enabled:
        return {"skipped": "GROWTH_ENABLED is off"}
    if not settings.enable_publish:
        return {"skipped": "publishing disabled"}
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        now = now or datetime.now(timezone.utc)
        _recover_stuck(now)
        with session_scope() as db:
            active = channels.active_channels(db)
        # A single legacy channel keeps the old flat result; several report per channel by slug.
        if len(active) == 1 and channels.is_default(active[0]):
            return _autopilot_one(active[0], now, rng)
        return {channel.slug: _autopilot_one(channel, now, rng) for channel in active}
    finally:
        _lock.release()
