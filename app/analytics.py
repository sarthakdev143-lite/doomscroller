"""Engagement and velocity scoring."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .models import Video


def engagement_score(v: Video) -> float:
    """Weighted engagement: (likes + 2*comments + 3*shares) / max(views, 1)."""
    if v.views <= 0:
        return 0.0
    raw = (v.likes + 2 * v.comments + 3 * v.shares) / v.views
    # log-compress to keep things in roughly [0, 1]
    import math
    return round(min(1.0, raw * 5.0), 4) if raw > 0 else 0.0


def velocity_score(v: Video, now: datetime | None = None) -> float:
    """A simple velocity heuristic: engagement_score * recency decay."""
    if now is None:
        now = datetime.now(timezone.utc)
    scraped = v.scraped_at or now
    if scraped.tzinfo is None:
        scraped = scraped.replace(tzinfo=timezone.utc)
    hours_old = max(0.0, (now - scraped).total_seconds() / 3600.0)
    # decay halflife ~ 48h
    decay = 0.5 ** (hours_old / 48.0)
    return round(engagement_score(v) * decay, 4)


def recompute_scores(videos: Iterable[Video]) -> None:
    """Mutate the given videos to have fresh engagement_score and velocity_score."""
    now = datetime.now(timezone.utc)
    for v in videos:
        v.engagement_score = engagement_score(v)
        v.velocity_score = velocity_score(v, now=now)
