"""Turn raw numbers into rewards, a model, and a reason for each video's result."""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select

from app.config import settings
from app.db import session_scope
from app.models import ClipJob, GrowthEvent, GrowthState, Video, VideoSnapshot, VideoStatus

from .factors import FACTORS, author_names, aware, published_arms
from .model import Model, Observation

log = logging.getLogger(__name__)

_RETENTION_WEIGHT = 2.0
_SUBSCRIBER_WEIGHT = 0.3
_MIN_BASELINE = 8
_BASELINE_WINDOW = 30
_MIN_LENGTH_BAND = 3
_FOCUS_WINDOW = 10
_FOCUS_SHARE = 0.4
# A loop can show above 100%, and one replayed clip should not dominate the fit.
_MAX_RETENTION = 1.5
# The model is checked every run, so one crossing of a loose threshold happens even when nothing matters.
_CONFIDENT_P_BEST = 0.95
_CONFIDENT_N = 8
# Runs hours apart re-read nearly the same videos, so only new videos confirm anything. Simulated with no real effects
# at 3 posts a day: 7 of 60 channels log a false finding by day 60, while a real doubling shows in about 24 days.
_CONFIRM_VIDEOS = 30
_FADED_P_BEST = 0.7
_LATE_FIRST_SNAPSHOT = 1.5
# Past the horizon. Older ones would all fire at once when a baseline appears.
_JUDGE_WITHIN = timedelta(days=14)
_MAX_MINED_PER_SOURCE = 2
_UPLOAD_LIVE = ("processed", "uploaded")


@dataclass
class Result:
    video_id: str
    title: str
    views: int
    arms: dict[str, str]
    day: float
    retention: Optional[float]
    subscribers: Optional[float]
    source_url: str
    diagnosed: bool
    authors: tuple[str, ...] = ()
    published_at: Optional[datetime] = None


def views_at(snapshots: Sequence[tuple[float, int]], hours: float) -> Optional[float]:
    """Views at an exact age, rising from zero at upload. None until that age, or if first seen far too late to say."""
    points = sorted(snapshots)
    if not points or points[-1][0] < hours or points[0][0] > hours * _LATE_FIRST_SNAPSHOT:
        return None
    before = [p for p in [(0.0, 0), *points] if p[0] <= hours]
    after = [p for p in points if p[0] >= hours]
    (t0, v0), (t1, v1) = before[-1], after[0]
    return float(v0) if t1 == t0 else v0 + (v1 - v0) * (hours - t0) / (t1 - t0)


def upload_turned_down(status: Optional[str]) -> bool:
    """YouTube refused the file, whatever its privacy says. A row collected before this was kept has no status."""
    return bool(status) and status not in _UPLOAD_LIVE


def _subscriber_rate(analytics: dict) -> Optional[float]:
    """Per 1000 views. A raw count keeps climbing with age, while the views a video is judged on stop at the horizon."""
    views, gained = analytics.get("views"), analytics.get("subscribersGained")
    return 1000 * gained / views if views and gained is not None else None


def matured_results(db, *, include_diagnosed: bool = True) -> list[Result]:
    """Public clips old enough to judge. A private test upload says nothing about an audience."""
    rows = db.execute(select(Video).where(
        Video.status == VideoStatus.PUBLISHED, Video.youtube_video_id.isnot(None),
        Video.published_at.isnot(None),
    )).scalars().all()
    rows = [v for v in rows if (v.meta or {}).get("clip_rendered") and (v.meta or {}).get("yt_privacy") == "public"
            and not (v.meta or {}).get("yt_missing")
            and not upload_turned_down((v.meta or {}).get("yt_upload_status"))]
    if not rows:
        return []
    first = min(aware(v.published_at) for v in rows)
    results = []
    for video in rows:
        # A too_early saved by an older version is still waiting for its baseline.
        diagnosed = (video.meta or {}).get("growth_diagnosis", {}).get("code") not in (None, "too_early")
        if diagnosed and not include_diagnosed:
            continue
        points = db.execute(select(VideoSnapshot.age_hours, VideoSnapshot.views)
                            .where(VideoSnapshot.video_id == video.id)).all()
        views = views_at([(a, v) for a, v in points], settings.growth_horizon_hours)
        if views is None:
            continue
        analytics = (video.meta or {}).get("yt_analytics") or {}
        percent = analytics.get("averageViewPercentage")
        results.append(Result(
            video_id=video.id, title=video.title or "", views=int(views), arms=published_arms(video),
            day=(aware(video.published_at) - first).total_seconds() / 86400,
            retention=min(percent / 100, _MAX_RETENTION) if percent is not None else None,
            subscribers=_subscriber_rate(analytics),
            source_url=video.source_url or "", diagnosed=diagnosed, authors=author_names(video),
            published_at=aware(video.published_at),
        ))
    return results


def reward(result: Result, typical_retention: float, typical_subscribers: float = 0.0) -> float:
    """Views on a log scale, nudged by whether people stayed and subscribed. Missing numbers count as typical."""
    retention = typical_retention if result.retention is None else result.retention
    subscribers = typical_subscribers if result.subscribers is None else result.subscribers
    return (math.log1p(result.views) + _RETENTION_WEIGHT * (retention - typical_retention)
            + _SUBSCRIBER_WEIGHT * math.log1p(max(subscribers, 0.0)))


def _typical_hold(length: Optional[str], others: Sequence[Result]) -> Optional[float]:
    """Median hold of clips this long. A short clip naturally shows more of itself, whatever it is worth."""
    held = [o for o in others if o.retention is not None]
    band = [o.retention for o in held if o.arms.get("length") == length]
    pool = band if len(band) >= _MIN_LENGTH_BAND else [o.retention for o in held]
    return statistics.median(pool) if pool else None


def build_model(results: Sequence[Result]) -> Model:
    """The blended score, which is what choices are made on."""
    holds = {length: _typical_hold(length, results) or 0.0 for length in {r.arms.get("length") for r in results}}
    known = [r.subscribers for r in results if r.subscribers is not None]
    subscribers = statistics.median(known) if known else 0.0
    return Model([Observation(r.arms, reward(r, holds[r.arms.get("length")], subscribers), r.day) for r in results])


def summarise(results: Sequence[Result]) -> dict:
    """Each arm's effect on views alone, with the blended score that drives choices under its `score` key."""
    views = Model([Observation(r.arms, math.log1p(r.views), r.day) for r in results]).summary()
    blended = build_model(results).summary()
    for factor, rows in views.items():
        scores = {row["arm"]: row for row in blended[factor]}
        for row in rows:
            row["score"] = {key: scores[row["arm"]][key] for key in ("multiplier", "low", "high", "p_best")}
    return views


@dataclass
class Diagnosis:
    code: str
    reason: str
    focus: tuple[str, ...] = ()


def diagnose(result: Result, others: Sequence[Result]) -> Diagnosis:
    """Why this video did what it did, against the videos it is compared with: see _baseline."""
    if len(others) < _MIN_BASELINE:
        return Diagnosis("too_early", f"Only {len(others)} other videos to compare with; {_MIN_BASELINE} are needed.")
    views = statistics.median(o.views for o in others)
    typical_hold = _typical_hold(result.arms.get("length"), others)
    hold = result.retention
    weak_hold = hold is not None and typical_hold is not None and hold < typical_hold * 0.8
    low_views = result.views < views * 0.5
    numbers = f"{result.views} views at {settings.growth_horizon_hours}h against a typical {views:.0f}"
    if hold is not None and typical_hold is not None:
        numbers += f", {hold:.0%} watched against a typical {typical_hold:.0%}"

    if result.views >= max(views * 2, 10):
        return Diagnosis("winner", f"Well above normal: {numbers}.")
    if weak_hold:
        return Diagnosis("weak_hold", f"Viewers left early: {numbers}. Testing length, pace and format harder.",
                         ("length", "pace", "kind", "sfx", "broll"))
    if low_views and hold is not None and typical_hold is not None:
        return Diagnosis("no_reach", f"People who saw it stayed, but few saw it: {numbers}. "
                         "Testing timing, title and source harder.", ("slot", "title_style", "series"))
    if low_views:
        return Diagnosis("low_views", f"Few views and no watch data yet: {numbers}. "
                         "Testing timing, title and source harder.", ("slot", "title_style", "series"))
    return Diagnosis("typical", f"Within normal range: {numbers}.")


def _baseline(result: Result, results: Sequence[Result]) -> list[Result]:
    """The channel's normal when this went out. Its first videos have no past, so they borrow what came next."""
    others = sorted((o for o in results if o.video_id != result.video_id), key=lambda o: o.day)
    earlier = [o for o in others if o.day <= result.day][-_BASELINE_WINDOW:]
    if len(earlier) >= _MIN_BASELINE:
        return earlier
    return earlier + [o for o in others if o.day > result.day][:_MIN_BASELINE - len(earlier)]


def _coming_clips(db) -> int:
    """What jobs in progress will add to the shelf. One may have stocked a few already, so this can run a little high."""
    counts = db.execute(select(ClipJob.count).where(ClipJob.status.in_(("queued", "running")))).scalars()
    return sum(count or settings.max_clips_per_video for count in counts)


def _mine_winner(db, result: Result) -> Optional[str]:
    """Cut more from a source that worked, while the shelf has room. The clipper steers around moments it has used."""
    from .autopilot import ready_clips
    from .safety import blocked_authors, blocked_sources

    if not result.source_url.startswith(("http://", "https://")) or result.source_url in blocked_sources(db):
        return None
    if blocked_authors(db).intersection(result.authors):
        return None
    active = db.scalar(select(func.count(ClipJob.id)).where(
        ClipJob.source == result.source_url, ClipJob.status.in_(("queued", "running"))))
    if active or len(ready_clips(db)) + _coming_clips(db) >= settings.growth_min_inventory:
        return None
    mined = db.execute(select(ClipJob).where(ClipJob.source == result.source_url, ClipJob.mined.is_(True))).scalars()
    # A failure that may clear up cut nothing from the source, so it does not use up its turn.
    spent = [job for job in mined if job.status != "failed" or (job.result or {}).get("permanent")]
    if len(spent) >= _MAX_MINED_PER_SOURCE:
        return None
    job = ClipJob(source=result.source_url, count=settings.growth_clips_per_source,
                  auto_queue=not settings.growth_require_approval, mined=True)
    db.add(job)
    db.flush()
    return job.id


def _mining_due(db, result: Result, diagnosis: dict, now: datetime) -> bool:
    """Never tried, or the last try failed in a way that may clear up and has waited as long as any other source."""
    from .supply import can_take

    if not diagnosis.get("mined_job"):
        return True
    last = db.get(ClipJob, diagnosis["mined_job"])
    if last is None or last.status != "failed":
        return False
    failures = db.execute(select(ClipJob).where(
        ClipJob.source == result.source_url, ClipJob.mined.is_(True), ClipJob.status == "failed")).scalars().all()
    return can_take(failures, now)


def mine_waiting_winner(db, now: datetime) -> Optional[str]:
    """A winner judged while the shelf was full is still owed its follow-up. Newest first, inside the judging window."""
    oldest = now - timedelta(hours=settings.growth_horizon_hours) - _JUDGE_WITHIN
    recent = [r for r in matured_results(db) if r.published_at >= oldest]
    for result in sorted(recent, key=lambda r: r.published_at, reverse=True):
        video = db.get(Video, result.video_id)
        diagnosis = (video.meta or {}).get("growth_diagnosis", {})
        if diagnosis.get("code") != "winner" or not _mining_due(db, result, diagnosis, now):
            continue
        job_id = _mine_winner(db, result)
        if job_id:
            video.meta = {**video.meta, "growth_diagnosis": {**diagnosis, "mined_job": job_id}}
            db.add(GrowthEvent(kind="winner_mined", video_id=video.id, data={"mined_job": job_id},
                               summary=f"{result.title[:60]}: stock fell, so cutting more from this winner's source."))
            return job_id
    return None


def _note_findings(db, summary: dict, matured: int) -> int:
    """Log an arm that stays clearly ahead for views across new videos, and take it back if that fades."""
    state = db.get(GrowthState, "findings")
    saved = state.value if state else {}
    live, held = dict(saved.get("live", {})), saved.get("since", {})
    since: dict[str, int] = {}
    added = 0
    for factor, rows in summary.items():
        if factor not in FACTORS:  # day_type is not something the loop or the owner can choose
            continue
        for row in rows:
            key = f"{factor}={row['arm']}"
            if row["p_best"] >= _CONFIDENT_P_BEST and row["n"] >= _CONFIDENT_N:
                since[key] = held.get(key, matured)
            if key in live and row["p_best"] < _FADED_P_BEST:
                del live[key]
                db.add(GrowthEvent(kind="finding_retracted", data=row, summary=(
                    f"{factor} = {row['arm']} no longer looks best: its chance fell to {row['p_best']:.0%}.")))
            elif key not in live and key in since and matured - since[key] >= _CONFIRM_VIDEOS:
                live[key] = row["multiplier"]
                db.add(GrowthEvent(kind="finding", data=row, summary=(
                    f"{factor} = {row['arm']} looks best: about {row['multiplier']}x the factor's average views "
                    f"(range {row['low']}-{row['high']}x, {row['n']} videos).")))
                added += 1
    set_state(db, "findings", {"live": live, "since": since})
    return added


def set_state(db, key: str, value: dict) -> None:
    state = db.get(GrowthState, key)
    if state:
        state.value = value
    else:
        db.add(GrowthState(key=key, value=value))


def note_once(db, kind: str, summary: str, *, key: str = "", within: timedelta = timedelta(days=1)) -> None:
    """Record a problem that repeats every run only once in a while, or it pushes everything else out of the report."""
    since = datetime.now(timezone.utc) - within
    recent = db.execute(select(GrowthEvent.data).where(GrowthEvent.kind == kind, GrowthEvent.created_at >= since)).scalars()
    if not any((data or {}).get("key") == key for data in recent):
        db.add(GrowthEvent(kind=kind, summary=summary, data={"key": key}))


def learn_job(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    oldest_age = timedelta(hours=settings.growth_horizon_hours) + _JUDGE_WITHIN
    counts: dict[str, int] = {}
    with session_scope() as db:
        results = matured_results(db)
        for result in (r for r in results if not r.diagnosed and now - r.published_at <= oldest_age):
            found = diagnose(result, _baseline(result, results))
            counts[found.code] = counts.get(found.code, 0) + 1
            if found.code == "too_early":  # judged on a later run, once there is a baseline
                continue
            action = {}
            if found.code == "winner":
                job_id = _mine_winner(db, result)
                if job_id:
                    action = {"mined_job": job_id}
            video = db.get(Video, result.video_id)
            video.meta = {**(video.meta or {}), "growth_diagnosis": {
                "code": found.code, "reason": found.reason, "focus": list(found.focus), "at": now.isoformat(), **action,
            }}
            db.add(GrowthEvent(kind=f"diagnosis:{found.code}", video_id=result.video_id,
                               summary=f"{result.title[:60]}: {found.reason}", data=action))
        db.flush()
        mine_waiting_winner(db, now)

        # A run of the same problem steers extra testing to the factors that could explain it.
        recent = db.execute(select(Video).where(Video.status == VideoStatus.PUBLISHED)
                            .order_by(Video.published_at.desc()).limit(_FOCUS_WINDOW * 3)).scalars().all()
        judged = [(v.meta or {})["growth_diagnosis"] for v in recent
                  if (v.meta or {}).get("growth_diagnosis", {}).get("code") not in (None, "too_early")][:_FOCUS_WINDOW]
        tally: dict[str, int] = {}
        for item in judged:
            for factor in item.get("focus", []):
                tally[factor] = tally.get(factor, 0) + 1
        focus = sorted(f for f, n in tally.items() if judged and n / len(judged) >= _FOCUS_SHARE)
        set_state(db, "focus", {"factors": focus})

        summary = summarise(results) if results else {}
        set_state(db, "summary", summary)
        findings = _note_findings(db, summary, len(results))
    return {"matured": len(results), "diagnosed": counts, "focus": focus, "findings": findings}
