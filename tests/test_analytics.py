"""Tests for the analytics module."""
from datetime import datetime, timedelta, timezone

from app.analytics import engagement_score, velocity_score
from app.models import Video


def make_video(**kw) -> Video:
    base = dict(
        id="x", source="tiktok", source_id="1", source_url="http://x",
        views=10000, likes=500, comments=50, shares=100, duration_sec=15,
    )
    base.update(kw)
    return Video(**base)


def test_engagement_zero_views():
    v = make_video(views=0)
    assert engagement_score(v) == 0.0


def test_engagement_increases_with_shares():
    v1 = make_video(shares=0)
    v2 = make_video(shares=1000)
    assert engagement_score(v2) >= engagement_score(v1)


def test_velocity_decays_with_age():
    now = datetime.now(timezone.utc)
    fresh = make_video(scraped_at=now - timedelta(hours=1))
    old = make_video(scraped_at=now - timedelta(hours=200))
    assert velocity_score(fresh, now=now) > velocity_score(old, now=now)
