"""Engagement, reach and virality scoring.

Four scores, all normalised to ``[0, 1]`` so they can be blended and compared
across sources:

``engagement_score``
    How *hard* an audience reacted, relative to how many saw it. A rate, so a
    small video with a devoted audience can beat a big indifferent one.
``reach_score``
    How *many* people saw it, log-compressed. Absolute reach is the single
    strongest signal that content actually resonates.
``velocity_score``
    Engagement decayed by age -- surfaces things that are hot *right now*.
``viral_score``
    The blend the queue is ranked by. Reach dominates, engagement rate refines,
    freshness breaks ties toward current trends.

Ages are measured from the platform publish time when we know it, falling back
to when we scraped the video. That distinction matters: a clip posted three
hours ago with 1M views is a very different proposition from one posted three
weeks ago with the same count.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import Video

# Views at which reach_score saturates to 1.0 (100M).
_REACH_CEILING_LOG = 8.0
# Engagement rates above this are treated as maximal (20% of viewers reacted).
_ENGAGEMENT_CEILING = 0.20
# Half-lives, in hours.
_VELOCITY_HALFLIFE_H = 48.0
_FRESHNESS_HALFLIFE_H = 72.0

# viral_score weights. Must sum to 1.0.
_W_REACH = 0.50
_W_ENGAGEMENT = 0.30
_W_FRESHNESS = 0.20


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a possibly naive or offset-aware datetime to UTC-aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_count(value: Any) -> float:
    """Coerce nullable/corrupt counters to a non-negative finite float."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed) or parsed <= 0.0:
        return 0.0
    return parsed


def age_hours(v: Video, now: datetime | None = None) -> float:
    """Hours since the video was published (or scraped, if publish time is unknown)."""
    now_utc = _as_utc(now) or datetime.now(timezone.utc)
    reference = (
        _as_utc(getattr(v, "source_published_at", None))
        or _as_utc(getattr(v, "scraped_at", None))
        or now_utc
    )
    return max(0.0, (now_utc - reference).total_seconds() / 3600.0)


def engagement_score(v: Video) -> float:
    """Weighted reaction rate: ``(likes + 2*comments + 3*shares) / views``.

    Shares are weighted highest because they are the mechanic that actually
    spreads a video; comments next; passive likes least.
    """
    views = _clean_count(getattr(v, "views", 0))
    if views <= 0.0:
        return 0.0
    likes = _clean_count(getattr(v, "likes", 0))
    comments = _clean_count(getattr(v, "comments", 0))
    shares = _clean_count(getattr(v, "shares", 0))
    weighted = likes + 2.0 * comments + 3.0 * shares
    if weighted <= 0.0:
        return 0.0
    rate = weighted / views
    return round(min(1.0, max(0.0, rate / _ENGAGEMENT_CEILING)), 4)


def reach_score(v: Video) -> float:
    """Log-compressed absolute view count. 10k -> 0.5, 1M -> 0.75, 100M -> 1.0."""
    views = _clean_count(getattr(v, "views", 0))
    if views <= 1.0:
        return 0.0
    return round(min(1.0, max(0.0, math.log10(views) / _REACH_CEILING_LOG)), 4)


def velocity_score(v: Video, now: datetime | None = None) -> float:
    """Engagement rate decayed by age (48h half-life)."""
    decay = 0.5 ** (age_hours(v, now) / _VELOCITY_HALFLIFE_H)
    return round(min(1.0, max(0.0, engagement_score(v) * decay)), 4)


def freshness(v: Video, now: datetime | None = None) -> float:
    """Pure recency factor in ``[0, 1]`` (72h half-life)."""
    return min(1.0, max(0.0, 0.5 ** (age_hours(v, now) / _FRESHNESS_HALFLIFE_H)))


def viral_score(v: Video, now: datetime | None = None) -> float:
    """Blended ranking score: reach-dominant, engagement-refined, freshness-tied."""
    blended = (
        _W_REACH * reach_score(v)
        + _W_ENGAGEMENT * engagement_score(v)
        + _W_FRESHNESS * freshness(v, now)
    )
    return round(min(1.0, max(0.0, blended)), 4)


def recompute_scores(videos: Iterable[Video], now: datetime | None = None) -> None:
    """Mutate each video in place so all four scores are current."""
    now_utc = _as_utc(now) or datetime.now(timezone.utc)
    for v in videos:
        if v is None:
            continue
        v.engagement_score = engagement_score(v)
        v.reach_score = reach_score(v)
        v.velocity_score = velocity_score(v, now=now_utc)
        v.viral_score = viral_score(v, now=now_utc)
