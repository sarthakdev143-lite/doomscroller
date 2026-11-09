"""The managed-channel registry: legacy fallback, channels.json sync, routing helpers."""
import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels
from app.models import Channel, ClipJob, Feed, GrowthEvent, GrowthState, Video, VideoStatus


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (Feed, Channel, ClipJob, GrowthEvent, GrowthState, Video):
            db.execute(delete(table))
    monkeypatch.setattr(settings, "channels_file", "")
    monkeypatch.setattr(settings, "youtube_token_path", "./secrets/youtube_token.json")
    monkeypatch.setattr(settings, "youtube_client_secrets", "")
    monkeypatch.setattr(settings, "growth_uploads_per_day", 3)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "growth_require_approval", True)
    monkeypatch.setattr(settings, "brand_name", "@seduxing")


def _write(tmp_path, monkeypatch, data):
    path = tmp_path / "channels.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(path))


def test_without_a_file_one_legacy_channel_is_seeded_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "growth_feeds", '["https://www.youtube.com/@MrBeast"]')
    with session_scope() as db:
        active = channels.active_channels(db)
        assert len(active) == 1
        channel = active[0]
        assert channel.slug == channels.DEFAULT_SLUG
        assert channel.brand_name == "@seduxing"
        assert channel.uploads_per_day == 3
        assert channels.feeds_for(db, channel) == ["https://www.youtube.com/@MrBeast"]


def test_channels_json_defines_channels_and_their_feeds(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "name": "Tech & Science", "uploads_per_day": 4, "timezone": "Asia/Kolkata",
         "client_secrets": "secrets/tech_cs.json", "token_path": "secrets/tech_token.json",
         "feeds": ["https://www.youtube.com/@mkbhd", "https://www.youtube.com/@Mrwhosetheboss"]},
        {"slug": "challenges", "name": "Challenges", "feeds": ["https://www.youtube.com/@MrBeast"]},
    ]})
    with session_scope() as db:
        active = channels.active_channels(db)
        assert {c.slug for c in active} == {"tech", "challenges"}
        tech = channels.channel_by_slug(db, "tech")
        assert tech.uploads_per_day == 4 and tech.timezone == "Asia/Kolkata"
        assert channels.feeds_for(db, tech) == ["https://www.youtube.com/@mkbhd",
                                                "https://www.youtube.com/@Mrwhosetheboss"]
        assert channels.feeds_for(db, channels.channel_by_slug(db, "challenges")) == \
            ["https://www.youtube.com/@MrBeast"]


def test_sync_reconciles_feeds_added_and_removed(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "feeds": ["https://www.youtube.com/@mkbhd", "https://www.youtube.com/@old"]}]})
    with session_scope() as db:
        channels.sync_channels(db)
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "feeds": ["https://www.youtube.com/@mkbhd", "https://www.youtube.com/@new"]}]})
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
        assert set(channels.feeds_for(db, tech)) == {"https://www.youtube.com/@mkbhd",
                                                     "https://www.youtube.com/@new"}


def test_a_channel_dropped_from_config_is_retired_not_deleted(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "gone"}]})
    with session_scope() as db:
        channels.sync_channels(db)
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}]})
    with session_scope() as db:
        channels.sync_channels(db)
        assert {c.slug for c in channels.active_channels(db)} == {"tech"}
        # Still present, just disabled, so its past videos keep a channel to point at.
        assert channels.channel_by_slug(db, "gone") is not None
        assert channels.channel_by_slug(db, "gone").enabled is False


def test_state_key_namespaces_per_channel(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "comedy"}]})
    with session_scope() as db:
        channels.sync_channels(db)
        tech, comedy = channels.channel_by_slug(db, "tech"), channels.channel_by_slug(db, "comedy")
        assert channels.state_key(tech, "plan") == "tech:plan"
        assert channels.state_key(comedy, "plan") == "comedy:plan"


def test_publisher_is_mock_without_a_cloud_project(tmp_path, monkeypatch):
    from app.publishers import MockPublisher

    monkeypatch.setattr(settings, "use_mock", True)
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "client_secrets": "secrets/tech_cs.json", "token_path": "secrets/tech_token.json"}]})
    with session_scope() as db:
        channels.sync_channels(db)
        assert isinstance(channels.publisher_for(channels.channel_by_slug(db, "tech")), MockPublisher)


def test_a_queued_source_carries_its_channel_to_the_clip_job():
    from app.clipper.job import enqueue

    job_id = enqueue("https://www.youtube.com/watch?v=abc", channel_id="chan-123", feed="https://feed")
    with session_scope() as db:
        job = db.get(ClipJob, job_id)
        assert job.channel_id == "chan-123"
        assert (job.result or {}).get("feed") == "https://feed"


def test_supply_feeds_each_channel_from_its_own_feeds(tmp_path, monkeypatch):
    from app.growth import supply

    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_require_approval", False)
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "feeds": ["https://yt/tech-feed"]},
        {"slug": "challenges", "feeds": ["https://yt/ch-feed"]},
    ]})
    links = {"https://yt/tech-feed": ["https://vid/tech1"], "https://yt/ch-feed": ["https://vid/ch1"]}
    monkeypatch.setattr(supply, "_feed_links", lambda feed: links.get(feed, []))

    out = supply.supply_job()
    assert out["tech"]["queued"] == "https://vid/tech1"
    assert out["challenges"]["queued"] == "https://vid/ch1"

    with session_scope() as db:
        tech, chal = channels.channel_by_slug(db, "tech"), channels.channel_by_slug(db, "challenges")
        jobs = {j.source: j.channel_id for j in db.execute(select(ClipJob)).scalars()}
    assert jobs["https://vid/tech1"] == tech.id
    assert jobs["https://vid/ch1"] == chal.id


def _ready_clip(tmp_path, name, channel_id):
    path = tmp_path / f"{name}.mp4"
    path.write_bytes(b"x")
    with session_scope() as db:
        video = Video(source="youtube", source_id=name, source_url="https://example.com/v", title=f"Clip {name}",
                      author="a", duration_sec=30, transformed_path=str(path), local_path=str(path),
                      status=VideoStatus.TRANSFORMED, channel_id=channel_id,
                      meta={"clip_rendered": True, "clip_kind": "visual", "clip_parent": name, "clip_hook": "hi"})
        db.add(video)
        db.flush()
        return video.id


def _due_plan(now):
    due = now - timedelta(hours=1)
    return {"day": now.date().isoformat(), "posts": [
        {"slot": "00-04", "due": due.isoformat(), "ends": (due + timedelta(hours=4)).isoformat(),
         "state": "waiting", "attempts": 0}]}


def test_autopilot_posts_each_channels_own_clip_to_its_own_plan(tmp_path, monkeypatch):
    from app.growth import autopilot

    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "use_mock", True)  # MockPublisher: a fake upload, no network
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "comedy"}]})
    now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        channels.sync_channels(db)
        tech, comedy = channels.channel_by_slug(db, "tech"), channels.channel_by_slug(db, "comedy")
    tech_clip = _ready_clip(tmp_path, "tech-clip", tech.id)
    comedy_clip = _ready_clip(tmp_path, "comedy-clip", comedy.id)
    with session_scope() as db:
        db.add(GrowthState(key="tech:plan", value=_due_plan(now)))
        db.add(GrowthState(key="comedy:plan", value=_due_plan(now)))

    out = autopilot.autopilot_job(now=now, rng=random.Random(1))
    assert out["tech"].get("ok") and out["comedy"].get("ok")
    with session_scope() as db:
        # Each channel posted its OWN clip, not the other's.
        assert db.get(Video, tech_clip).status == VideoStatus.PUBLISHED
        assert db.get(Video, comedy_clip).status == VideoStatus.PUBLISHED
        assert out["tech"]["video_id"] == tech_clip
        assert out["comedy"]["video_id"] == comedy_clip
        # Separate plans, each marked posted.
        assert db.get(GrowthState, "tech:plan").value["posts"][0]["state"] == "posted"
        assert db.get(GrowthState, "comedy:plan").value["posts"][0]["state"] == "posted"


def _atom(*entries):
    rows = "".join(
        f'<entry><yt:videoId>{vid}</yt:videoId><published>{when}</published></entry>' for vid, when in entries)
    return ('<feed xmlns="http://www.w3.org/2005/Atom" '
            f'xmlns:yt="http://www.youtube.com/xml/schemas/2015">{rows}</feed>')


def test_watch_queues_a_fresh_upload_but_not_on_first_sight(tmp_path, monkeypatch):
    from app.growth import watch

    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_require_approval", False)
    monkeypatch.setattr(settings, "growth_watch_minutes", 5)
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "feeds": ["https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa"]}]})

    # First sight: only learns the watermark, cuts nothing (the back catalogue is supply's job).
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("VID1", "2026-11-09T08:00:00+00:00")))
    assert watch.watch_job(now=datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)) == {"feeds": 1, "queued": 0}
    assert _clipjob_count() == 0

    # A newer upload appears and is cut at once, with the channel's id.
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(
        ("VID2", "2026-11-09T09:30:00+00:00"), ("VID1", "2026-11-09T08:00:00+00:00")))
    out = watch.watch_job(now=datetime(2026, 11, 9, 9, 45, tzinfo=timezone.utc))
    assert out["queued"] == 1
    with session_scope() as db:
        tech = channels.channel_by_slug(db, "tech")
        job = db.execute(select(ClipJob)).scalar_one()
        assert job.channel_id == tech.id and "VID2" in job.source


def test_watch_ignores_an_upload_older_than_the_fresh_window(tmp_path, monkeypatch):
    from app.growth import watch

    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_watch_minutes", 5)
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "feeds": ["https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa"]}]})
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("OLD", "2026-11-09T00:00:00+00:00")))
    watch.watch_job(now=datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc))  # watermark
    # 10 hours later a "new" entry appears but it was published long before the fresh window.
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(
        ("STALE", "2026-11-09T02:00:00+00:00"), ("OLD", "2026-11-09T00:00:00+00:00")))
    assert watch.watch_job(now=datetime(2026, 11, 9, 11, 0, tzinfo=timezone.utc))["queued"] == 0
    assert _clipjob_count() == 0


def test_watch_resolves_a_handle_to_its_channel_rss(tmp_path, monkeypatch):
    from app.growth import watch

    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_watch_minutes", 5)
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "feeds": ["https://www.youtube.com/@sometech"]}]})
    calls = []

    def fetch(url):
        calls.append(url)
        if "feeds/videos.xml" in url:
            return _atom(("VID1", "2026-11-09T08:00:00+00:00"))
        return '<html>...{"channelId":"UCabcdefghijklmnopqrstuv"}...</html>'  # the handle page

    monkeypatch.setattr(watch, "_fetch", fetch)
    assert watch.watch_job(now=datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc))["feeds"] == 1
    assert any("channel_id=UCabcdefghijklmnopqrstuv" in url for url in calls)


def _clipjob_count():
    with session_scope() as db:
        return len(db.execute(select(ClipJob)).scalars().all())


def test_review_card_shows_the_target_channel(tmp_path, monkeypatch):
    from app.growth import approval

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "name": "Tech & Science"}]})
    path = tmp_path / "c.mp4"
    path.write_bytes(b"x")
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
    with session_scope() as db:
        db.add(Video(source="youtube", source_id="c1", source_url="u", title="t", status=VideoStatus.PENDING,
                     transformed_path=str(path), channel_id=tech.id, duration_sec=20, meta={"clip_rendered": True}))
    card = approval.queue()["clips"][0]
    assert card["channel"] == "Tech & Science"


def test_doctor_reports_each_named_channels_readiness(tmp_path, monkeypatch):
    from app.growth import report

    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "name": "Tech", "client_secrets": "secrets/nope.json",
         "token_path": "secrets/nope_token.json", "feeds": ["https://www.youtube.com/@mkbhd"]}]})
    with session_scope() as db:
        channels.sync_channels(db)
    out = report.doctor(live=False)
    assert "channels" in out and out["channels"][0]["slug"] == "tech"
    # Its client_secrets and token files do not exist, so the channel (and the whole doctor) is not ready.
    assert out["channels"][0]["ready"] is False
    assert out["ready"] is False
