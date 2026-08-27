"""Tests for candidate selection, scoring, and transform planning."""
from datetime import datetime, timedelta, timezone

import pytest

from app import analytics
from app.config import settings
from app.models import Video
from app.scrapers.base import ScrapedItem
from tasks.pipeline import (
    content_safety_reason,
    plan_transform,
    select_candidates,
    _is_viral_candidate,
    _shorts_shape_reason,
)


@pytest.fixture(autouse=True)
def neutral_channel_focus():
    """These tests exercise safety/shape/virality/ranking, not channel focus.

    select_candidates also applies the language and topic filters, so leave
    those wide open here; test_channel_focus.py covers them directly.
    """
    saved = (settings.target_languages, settings.target_topics,
             settings.excluded_topics, settings.allow_unknown_language)
    settings.target_languages = "[]"
    settings.target_topics = "[]"
    settings.excluded_topics = "[]"
    settings.allow_unknown_language = True
    yield
    (settings.target_languages, settings.target_topics,
     settings.excluded_topics, settings.allow_unknown_language) = saved


def item(**kw) -> ScrapedItem:
    base = dict(
        source="snapchat",
        source_id="abc",
        source_url="https://snap/x",
        author="creator",
        title="A fun clip",
        description="Something happens.",
        views=500_000,
        likes=20_000,
        comments=300,
        shares=1_500,
        duration_sec=12,
        width=540,
        height=960,
        language="en",
        published_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    base.update(kw)
    return ScrapedItem(**base)


def video(**kw) -> Video:
    base = dict(
        source="snapchat", source_id="1", source_url="u",
        views=100_000, likes=5_000, comments=100, shares=500, duration_sec=12,
        scraped_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return Video(**base)


# ------------------------------------------------------------------ safety
@pytest.mark.parametrize("field,value", [
    ("title", "Hot NSFW content here"),
    ("description", "Join my onlyfans now"),
])
def test_safety_blocks_by_text_field(field, value):
    assert content_safety_reason(item(**{field: value})) is not None


def test_safety_blocks_by_keyword_and_hashtag():
    assert content_safety_reason(item(keywords=["casino", "fun"])) is not None
    assert content_safety_reason(item(meta={"hashtags": ["gambling"]})) is not None


def test_safety_blocks_snapchat_content_advisory():
    # Snapchat bakes this phrase into the generated title of flagged posts.
    assert content_safety_reason(
        item(title="Sensitive Content Warning: viewer discretion")
    ) is not None


def test_safety_allows_ordinary_content():
    assert content_safety_reason(item()) is None
    assert content_safety_reason(item(keywords=["dance", "pets"])) is None


def test_safety_is_case_insensitive():
    assert content_safety_reason(item(title="PORN")) is not None


# ------------------------------------------------------------------- shape
def test_shape_rejects_landscape():
    assert _shorts_shape_reason(item(width=1920, height=1080)) is not None


def test_shape_accepts_vertical_and_square_ish():
    assert _shorts_shape_reason(item(width=540, height=960)) is None


def test_shape_rejects_out_of_range_durations():
    assert _shorts_shape_reason(item(duration_sec=1)) is not None
    assert _shorts_shape_reason(item(duration_sec=600)) is not None
    assert _shorts_shape_reason(item(duration_sec=12)) is None


def test_shape_allows_unknown_dimensions():
    # Dimensions are unknown for some sources; do not reject on missing data.
    assert _shorts_shape_reason(item(width=0, height=0)) is None


# ---------------------------------------------------------------- virality
def test_snapchat_gate_is_reach_based():
    assert _is_viral_candidate(item(views=settings.viral_min_views + 1), 0.0, 0.0)
    assert not _is_viral_candidate(item(views=10), 0.9, 0.9)


def test_snapchat_gate_passes_statless_items_downstream():
    # No stats at all is not evidence of low quality; let the file-level gate rule.
    assert _is_viral_candidate(item(views=0, likes=0), 0.0, 0.0)


def test_mock_source_always_passes():
    assert _is_viral_candidate(item(source="mock", views=0, likes=0), 0.0, 0.0)


# ----------------------------------------------------------------- scoring
def test_engagement_is_zero_without_views():
    assert analytics.engagement_score(video(views=0)) == 0.0


def test_engagement_rewards_shares_most():
    shares = analytics.engagement_score(video(likes=0, comments=0, shares=1000))
    likes = analytics.engagement_score(video(likes=1000, comments=0, shares=0))
    assert shares > likes


def test_reach_score_is_monotonic_and_bounded():
    scores = [analytics.reach_score(video(views=v)) for v in (0, 1_000, 1_000_000, 10**9)]
    assert scores == sorted(scores)
    assert scores[0] == 0.0
    assert scores[-1] <= 1.0


def test_velocity_decays_with_age():
    now = datetime.now(timezone.utc)
    fresh = video(source_published_at=now - timedelta(hours=1))
    stale = video(source_published_at=now - timedelta(hours=240))
    assert analytics.velocity_score(fresh, now=now) > analytics.velocity_score(stale, now=now)


def test_age_prefers_platform_publish_time_over_scrape_time():
    now = datetime.now(timezone.utc)
    # Scraped just now, but actually published three weeks ago.
    old = video(scraped_at=now, source_published_at=now - timedelta(days=21))
    assert analytics.age_hours(old, now=now) > 400


def test_age_handles_naive_datetimes():
    now = datetime.now(timezone.utc)
    naive_stamp = (now - timedelta(hours=5)).replace(tzinfo=None)
    naive = video(scraped_at=naive_stamp, source_published_at=None)
    assert analytics.age_hours(naive, now=now) == pytest.approx(5, abs=1)


def test_viral_score_blends_and_stays_bounded():
    now = datetime.now(timezone.utc)
    huge = video(views=50_000_000, likes=5_000_000, shares=500_000,
                 source_published_at=now - timedelta(hours=2))
    tiny = video(views=200, likes=1, shares=0,
                 source_published_at=now - timedelta(days=30))
    assert 0.0 <= analytics.viral_score(tiny, now=now) < analytics.viral_score(huge, now=now) <= 1.0


def test_recompute_sets_all_four_scores():
    v = video()
    analytics.recompute_scores([v])
    for field in ("engagement_score", "reach_score", "velocity_score", "viral_score"):
        assert getattr(v, field) > 0


# --------------------------------------------------------------- selection
def _proportional(source_id: str, views: int) -> ScrapedItem:
    """An item whose engagement scales with its views, isolating reach."""
    return item(
        source_id=source_id, views=views,
        likes=int(views * 0.05), comments=int(views * 0.002),
        shares=int(views * 0.01),
    )


def test_select_candidates_ranks_by_reach_when_rates_match():
    items = [
        _proportional("low", 30_000),
        _proportional("high", 20_000_000),
        _proportional("mid", 800_000),
    ]
    chosen, rejected = select_candidates(items, limit=2)
    assert [i.source_id for i in chosen] == ["high", "mid"]
    assert rejected["over limit"] == 1


def test_select_candidates_can_favour_rate_over_raw_reach():
    """A smaller video with a much better reaction rate is allowed to win.

    This is deliberate: engagement rate is the signal that a clip actually
    landed, so it is weighted alongside reach rather than behind it.
    """
    devoted = item(source_id="devoted", views=30_000, likes=4_000, comments=300, shares=1_500)
    indifferent = item(source_id="indifferent", views=800_000, likes=4_000, comments=20, shares=100)
    chosen, _ = select_candidates([indifferent, devoted], limit=1)
    assert chosen[0].source_id == "devoted"


def test_select_candidates_counts_rejection_reasons():
    items = [
        item(source_id="unsafe", title="free casino spins"),
        item(source_id="wide", width=1920, height=1080),
        item(source_id="small", views=5),
        item(source_id="good"),
    ]
    chosen, rejected = select_candidates(items, limit=10)
    assert [i.source_id for i in chosen] == ["good"]
    assert rejected["blocked term"] == 1
    assert rejected["not vertical"] == 1
    assert rejected["below virality bar"] == 1


def test_select_candidates_handles_empty_input():
    assert select_candidates([], limit=5) == ([], {})


# ------------------------------------------------------- transform planning
def test_plan_transform_copies_full_size_vertical():
    assert plan_transform(1080, 1920)["action"] == "copy"


def test_plan_transform_scales_small_vertical_without_blur():
    plan = plan_transform(540, 960)
    assert plan["action"] == "scale"
    # A blurred backdrop is invisible behind a frame-filling foreground.
    assert plan["add_blur_background"] is False


def test_plan_transform_letterboxes_landscape():
    plan = plan_transform(1920, 1080)
    assert plan["action"] == "letterbox"
    assert plan["add_blur_background"] is True


def test_plan_transform_handles_unknown_dimensions():
    assert plan_transform(0, 0)["action"] == "letterbox"
