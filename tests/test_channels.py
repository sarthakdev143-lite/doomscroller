"""The managed-channel registry: legacy fallback, channels.json sync, routing helpers."""
import json

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels
from app.models import Channel, Feed


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    with session_scope() as db:
        db.execute(delete(Feed))
        db.execute(delete(Channel))
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
    from app.models import ClipJob

    job_id = enqueue("https://www.youtube.com/watch?v=abc", channel_id="chan-123", feed="https://feed")
    with session_scope() as db:
        job = db.get(ClipJob, job_id)
        assert job.channel_id == "chan-123"
        assert (job.result or {}).get("feed") == "https://feed"
