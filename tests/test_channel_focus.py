"""Tests for channel-focus filtering and the queue re-filter pass."""
import pytest

from app.config import settings
from app.db import init_db, session_scope
from app.models import Video, VideoStatus
from app.scrapers.base import ScrapedItem
from sqlalchemy import select
from tasks.pipeline import apply_queue_filters, channel_focus_reason


@pytest.fixture(autouse=True)
def restore_focus_settings():
    """Focus settings are process-global; put them back after each test."""
    saved = (
        settings.target_languages,
        settings.target_topics,
        settings.excluded_topics,
        settings.allow_unknown_language,
    )
    yield
    (
        settings.target_languages,
        settings.target_topics,
        settings.excluded_topics,
        settings.allow_unknown_language,
    ) = saved


def item(**kw) -> ScrapedItem:
    base = dict(
        source="snapchat", source_id="x", source_url="u",
        title="A dance clip", description="People dancing",
        keywords=["dance"], language="en",
        views=500_000, likes=20_000, duration_sec=12, width=540, height=960,
    )
    base.update(kw)
    return ScrapedItem(**base)


# ------------------------------------------------------------------ language
def test_allowed_language_passes():
    settings.target_languages = '["en"]'
    assert channel_focus_reason(item(language="en")) is None


def test_other_language_is_rejected():
    settings.target_languages = '["en"]'
    assert "language" in channel_focus_reason(item(language="hi"))


def test_multiple_allowed_languages():
    settings.target_languages = '["en","hi"]'
    assert channel_focus_reason(item(language="hi")) is None
    assert channel_focus_reason(item(language="ko")) is not None


def test_empty_allowlist_accepts_everything():
    settings.target_languages = "[]"
    for language in ("en", "hi", "ko", ""):
        assert channel_focus_reason(item(language=language)) is None


def test_unknown_language_follows_the_flag():
    settings.target_languages = '["en"]'
    settings.allow_unknown_language = True
    assert channel_focus_reason(item(language="")) is None
    settings.allow_unknown_language = False
    assert channel_focus_reason(item(language="")) is not None


def test_language_match_is_case_insensitive():
    settings.target_languages = '["en"]'
    assert channel_focus_reason(item(language="EN")) is None


# --------------------------------------------------------------------- topics
def test_excluded_topic_rejects():
    settings.target_languages = "[]"
    settings.excluded_topics = '["politics"]'
    assert "excluded topic" in channel_focus_reason(item(keywords=["politics"]))


def test_excluded_topic_matches_title_and_description():
    settings.target_languages = "[]"
    settings.excluded_topics = '["politics"]'
    assert channel_focus_reason(item(title="A politics rant", keywords=[])) is not None
    assert channel_focus_reason(
        item(description="about politics", keywords=[])
    ) is not None


def test_required_topic_must_match():
    settings.target_languages = "[]"
    settings.target_topics = '["cooking"]'
    assert channel_focus_reason(item(keywords=["dance"])) is not None
    assert channel_focus_reason(item(keywords=["cooking"])) is None


def test_required_topic_matches_hashtags():
    settings.target_languages = "[]"
    settings.target_topics = '["cooking"]'
    assert channel_focus_reason(
        item(keywords=[], meta={"hashtags": ["cooking"]})
    ) is None


def test_exclusion_beats_inclusion():
    settings.target_languages = "[]"
    settings.target_topics = '["dance"]'
    settings.excluded_topics = '["politics"]'
    # Matches the required topic but also an excluded one.
    assert channel_focus_reason(item(keywords=["dance", "politics"])) is not None


# ------------------------------------------------------------ queue refilter
def _seed(db, **kw) -> str:
    base = dict(
        source="snapchat", source_url="u", title="t", description="d",
        keywords=["dance"], language="en", views=500_000, likes=20_000,
        duration_sec=12, width=540, height=960, status=VideoStatus.PENDING,
    )
    base.update(kw)
    video = Video(**base)
    db.add(video)
    db.flush()
    return video.id


def test_apply_queue_filters_dry_run_changes_nothing():
    init_db()
    settings.target_languages = '["en"]'
    with session_scope() as db:
        keep = _seed(db, source_id="keep-dry", language="en")
        drop = _seed(db, source_id="drop-dry", language="hi")

    result = apply_queue_filters(dry_run=True)
    assert result["dry_run"] is True
    assert result["rejected"] >= 1

    with session_scope() as db:
        assert db.get(Video, keep).status == VideoStatus.PENDING
        assert db.get(Video, drop).status == VideoStatus.PENDING, "dry run must not write"


def test_apply_queue_filters_rejects_off_focus_rows():
    init_db()
    settings.target_languages = '["en"]'
    with session_scope() as db:
        keep = _seed(db, source_id="keep-apply", language="en")
        drop = _seed(db, source_id="drop-apply", language="ko")

    apply_queue_filters()

    with session_scope() as db:
        assert db.get(Video, keep).status == VideoStatus.PENDING
        dropped = db.get(Video, drop)
        assert dropped.status == VideoStatus.REJECTED
        assert "language" in (dropped.meta or {}).get("rejected_reason", "")


def test_apply_queue_filters_also_catches_unsafe_content():
    init_db()
    settings.target_languages = "[]"
    with session_scope() as db:
        unsafe = _seed(db, source_id="unsafe-row", title="free casino spins")

    result = apply_queue_filters()
    assert result["rejected"] >= 1
    with session_scope() as db:
        assert db.get(Video, unsafe).status == VideoStatus.REJECTED


def test_apply_queue_filters_leaves_curated_rows_alone():
    init_db()
    settings.target_languages = '["en"]'
    with session_scope() as db:
        approved = _seed(
            db, source_id="already-approved", language="hi",
            status=VideoStatus.APPROVED,
        )

    apply_queue_filters()
    with session_scope() as db:
        # Only PENDING rows are re-filtered; a human decision is not overridden.
        assert db.get(Video, approved).status == VideoStatus.APPROVED
