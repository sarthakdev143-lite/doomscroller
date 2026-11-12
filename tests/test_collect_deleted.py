"""A video deleted between the analytics-due query and the analytics write
must not crash the batch nor miscount what was recorded."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from app.db import init_db, session_scope
from app.growth import collect
from app.models import ClipJob, Feed, GrowthEvent, GrowthState, Video, VideoSnapshot, VideoStatus


def _published(video_id="v1", yt_id="yt1", published=None):
    published = published or (datetime.now(timezone.utc) - timedelta(days=5))
    with session_scope() as db:
        db.add(Video(
            id=video_id, source="youtube", source_id=f"src_{video_id}",
            source_url="https://example.com/v", title="Clip",
            author="a", duration_sec=30,
            transformed_path="/tmp/x.mp4", local_path="/tmp/x.mp4",
            status=VideoStatus.PUBLISHED, youtube_video_id=yt_id,
            published_at=published,
            meta={"clip_rendered": True, "yt_privacy": "public"},
        ))
    return video_id


class _FakeInsights:
    def retention(self, ids, start, end):
        return {yt: {"averageViewPercentage": 50.0} for yt in ids}

    def feed_share(self, *args):
        return 0.5


def test_deleted_row_does_not_crash_analytics_batch(monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (Feed, VideoSnapshot, GrowthEvent, GrowthState, ClipJob, Video):
            db.execute(delete(table))
    vid = _published()
    published = datetime.now(timezone.utc) - timedelta(days=5)
    stale_due = [(vid, "yt1", published), ("gone-id", "ytGone", published)]
    monkeypatch.setattr(collect, "_analytics_due", lambda now, scope=None: list(stale_due))
    monkeypatch.setattr(collect, "_first_report_day", lambda moment: moment.date())
    monkeypatch.setattr(collect, "_report_day", lambda moment: moment.date())
    now = datetime.now(timezone.utc)
    # Must not raise; the surviving row is still recorded.
    recorded = collect._record_analytics(_FakeInsights(), now)
    assert recorded == 1
    with session_scope() as db:
        assert db.get(Video, vid).meta["yt_analytics"]["shorts_feed_share"] == 0.5
