"""The managed-channel registry: legacy fallback, channels.json sync, routing helpers."""
import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels
from app.models import Channel, ClipJob, Feed, GrowthEvent, GrowthState, QuotaUsage, Video, VideoStatus


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (Feed, Channel, ClipJob, GrowthEvent, GrowthState, QuotaUsage, Video):
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


def test_a_broken_channels_json_keeps_the_fleet_and_doctor_flags_it(tmp_path, monkeypatch):
    from app.growth import report

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "comedy"}]})
    with session_scope() as db:
        channels.sync_channels(db)
        assert {c.slug for c in channels.active_channels(db)} == {"tech", "comedy"}
    # A trailing comma over SSH must NOT retire every channel to the legacy default.
    (tmp_path / "channels.json").write_text('{"channels": [{"slug": "tech"},]}', encoding="utf-8")
    with session_scope() as db:
        channels.sync_channels(db)
        assert {c.slug for c in channels.active_channels(db)} == {"tech", "comedy"}  # fleet unchanged
        assert "error" in channels.config_health(db)
    check = next(c for c in report.doctor(live=False)["checks"] if c["name"] == "channels.json")
    assert check["ok"] is False and "valid JSON" in check["detail"]


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


def test_reload_applies_channel_edits_without_a_restart(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}]})
    with session_scope() as db:
        channels.sync_channels(db)
    # The owner adds a channel and runs `growth.py reload` — no restart.
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "comedy"}]})
    result = channels.reload_channels()
    assert set(result["active"]) == {"tech", "comedy"} and "error" not in result
    with session_scope() as db:
        assert {c.slug for c in channels.active_channels(db)} == {"tech", "comedy"}


def test_adding_a_channel_is_picked_up_by_a_resync_the_app_runs_at_boot(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}]})
    with session_scope() as db:
        assert {c.slug for c in channels.active_channels(db)} == {"tech"}  # first lazy seed
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "comedy"}]})
    with session_scope() as db:
        # The lazy seed only fires on an empty table, so reading alone does NOT see the new channel...
        assert {c.slug for c in channels.active_channels(db)} == {"tech"}
        # ...the boot-time sync_channels (wired into the app lifespan) reconciles it.
        channels.sync_channels(db)
        assert {c.slug for c in channels.active_channels(db)} == {"tech", "comedy"}


def test_channels_are_added_and_switched_without_editing_the_file_by_hand(tmp_path, monkeypatch):
    path = tmp_path / "channels.json"
    monkeypatch.setattr(settings, "channels_file", str(path))
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    out = channels.add_channel("tech", name="Tech & Science", feeds=("https://www.youtube.com/@mkbhd",))
    assert out["active"] == []                                   # off until its token is in place
    saved = json.loads(path.read_text())["channels"][0]
    assert saved["client_secrets"] == "secrets/tech_client_secret.json" and saved["enabled"] is False
    assert channels.set_enabled("tech", True)["active"] == ["tech"]
    channels.change_feeds("tech", add=("https://www.youtube.com/@MarkRober",), remove=("https://www.youtube.com/@mkbhd",))
    with session_scope() as db:
        assert channels.feeds_for(db, channels.channel_by_slug(db, "tech")) == ["https://www.youtube.com/@MarkRober"]
    assert channels.set_enabled("tech", False)["active"] == []    # one channel stopped, no restart


def test_the_first_added_channel_does_not_retire_the_one_already_running(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    monkeypatch.setattr(settings, "growth_feeds", '["https://www.youtube.com/@MrBeast"]')
    with session_scope() as db:
        assert [c.slug for c in channels.active_channels(db)] == [channels.DEFAULT_SLUG]   # the legacy channel
    out = channels.add_channel("tech")
    assert out["active"] == [channels.DEFAULT_SLUG]               # still running; tech is off for now


def test_a_bad_change_is_refused_and_the_file_is_left_alone(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"_comment": ["keep me"], "channels": [{"slug": "tech"}]})
    before = (tmp_path / "channels.json").read_text()
    with pytest.raises(channels.ChannelsConfigError):
        channels.add_channel("Not A Slug")
    with pytest.raises(channels.ChannelsConfigError):
        channels.set_enabled("missing", True)
    assert (tmp_path / "channels.json").read_text() == before
    channels.add_channel("comedy")
    assert json.loads((tmp_path / "channels.json").read_text())["_comment"] == ["keep me"]   # notes survive an edit


def test_each_channel_counts_its_own_uploads():
    from app import quota

    with session_scope() as db:
        quota.consume_one(db, channel_id="channel-a")
        quota.consume_one(db, channel_id="channel-a")
        quota.consume_one(db)
    with session_scope() as db:
        assert quota.snapshot(db, "channel-a")["uploads"] == 2
        assert quota.snapshot(db, "channel-b")["uploads"] == 0   # another channel's project is untouched
        assert quota.snapshot(db)["uploads"] == 1                # and so is the legacy count


def test_one_channel_failing_does_not_stop_the_others(tmp_path, monkeypatch):
    from app.growth import supply

    monkeypatch.setattr(settings, "growth_enabled", True)
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "broken", "feeds": ["https://yt/a"]}, {"slug": "fine", "feeds": ["https://yt/b"]}]})
    real = supply._supply_one

    def flaky(channel):
        if channel.slug == "broken":
            raise RuntimeError("database is locked")
        return real(channel)

    monkeypatch.setattr(supply, "_supply_one", flaky)
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://vid/1"])
    out = supply.supply_job()
    assert out["fine"]["queued"] == "https://vid/1"          # the healthy channel still ran
    assert "database is locked" in out["broken"]["error"]
    assert out["error"].startswith("broken:")                # and the run is recorded as failed


def test_a_channel_put_back_in_the_config_runs_again(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "comedy"}]})
    with session_scope() as db:
        channels.sync_channels(db)
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}]})
    with session_scope() as db:
        channels.sync_channels(db)
        assert channels.channel_by_slug(db, "comedy").enabled is False
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "comedy"}]})
    with session_scope() as db:
        channels.sync_channels(db)
        assert channels.channel_by_slug(db, "comedy").enabled is True


@pytest.mark.parametrize("entry, problem", [
    ({"slug": "Tech Channel"}, "slug"),
    ({"slug": "error"}, "reserved"),
    ({"slug": "tech", "uploads_per_day": "3"}, "uploads_per_day"),
    ({"slug": "tech", "timezone": "Mars/Olympus"}, "timezone"),
    ({"slug": "tech", "enabled": "yes"}, "enabled"),
    ({"slug": "tech", "feeds": "https://yt/a"}, "feeds"),
])
def test_a_bad_value_is_refused_by_name_and_the_fleet_is_kept(tmp_path, monkeypatch, entry, problem):
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "good"}]})
    with session_scope() as db:
        channels.sync_channels(db)
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "good"}, entry]})
    with session_scope() as db:
        channels.sync_channels(db)
        assert {c.slug for c in channels.active_channels(db)} == {"good"}
        assert problem in channels.config_health(db)["error"]


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


def _pending_clip(tmp_path, name, channel_id, score, ai_titled=True):
    path = tmp_path / f"{name}.mp4"
    path.write_bytes(b"x")
    with session_scope() as db:
        video = Video(source="youtube", source_id=name, source_url="u", title=f"Clip {name}", author="a",
                      duration_sec=30, transformed_path=str(path), local_path=str(path), status=VideoStatus.PENDING,
                      channel_id=channel_id, meta={"clip_rendered": True, "clip_kind": "visual",
                                                   "clip_parent": name, "clip_score": score,
                                                   "clip_ai_titled": ai_titled})
        db.add(video)
        db.flush()
        return video.id


def test_auto_approve_sweep_passes_only_clips_above_the_threshold(tmp_path, monkeypatch):
    from app.growth import approval

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "auto_approve_score": 0.7}]})
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
    high = _pending_clip(tmp_path, "hi", tech.id, 0.9)
    low = _pending_clip(tmp_path, "lo", tech.id, 0.3)
    with session_scope() as db:
        assert approval.auto_approve_sweep(channels.channel_by_slug(db, "tech")) == 1
    with session_scope() as db:
        assert db.get(Video, high).status == VideoStatus.TRANSFORMED
        assert db.get(Video, high).meta.get("auto_approved")
        assert db.get(Video, high).curated_at is None  # not a human decision; upload gate still runs
        assert db.get(Video, low).status == VideoStatus.PENDING


def test_a_clip_no_ai_titled_is_never_approved_without_a_person(tmp_path, monkeypatch):
    from app.growth import approval

    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "auto_approve_score": 0.5, "cadence_backstop": True}]})
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
    raw = _pending_clip(tmp_path, "raw", tech.id, 0.99, ai_titled=False)
    with session_scope() as db:
        tech = channels.channel_by_slug(db, "tech")
        assert approval.auto_approve_sweep(tech) == 0
        assert approval.backstop_approve(tech) is None
    with session_scope() as db:
        assert db.get(Video, raw).status == VideoStatus.PENDING


def test_auto_approve_is_off_by_default(tmp_path, monkeypatch):
    from app.growth import approval

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}]})  # no threshold
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
    _pending_clip(tmp_path, "c", tech.id, 0.99)
    with session_scope() as db:
        assert approval.auto_approve_sweep(channels.channel_by_slug(db, "tech")) == 0  # a human reviews all


def test_cadence_backstop_approves_the_best_waiting_clip(tmp_path, monkeypatch):
    from app.growth import approval

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "cadence_backstop": True}]})
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
    _pending_clip(tmp_path, "weak", tech.id, 0.4)
    best = _pending_clip(tmp_path, "strong", tech.id, 0.8)
    with session_scope() as db:
        assert approval.backstop_approve(channels.channel_by_slug(db, "tech")) == best
    with session_scope() as db:
        assert db.get(Video, best).status == VideoStatus.TRANSFORMED


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


def test_a_strike_on_one_channel_pauses_only_that_channel(tmp_path, monkeypatch):
    from app.growth import safety

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}, {"slug": "comedy"}]})
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
        video = Video(source="youtube", source_id="x", source_url="u", title="t", status=VideoStatus.PUBLISHED,
                      youtube_video_id="yt1", channel_id=tech.id, meta={"clip_rendered": True})
        db.add(video)
        db.flush()
        safety.record_strike(video, "copyright", now=now)
    with session_scope() as db:
        tech, comedy = channels.channel_by_slug(db, "tech"), channels.channel_by_slug(db, "comedy")
        assert safety.pause_reason(db, tech) is not None       # the struck channel is paused
        assert safety.pause_reason(db, comedy) is None          # the others keep posting
    # resume is per-channel too
    assert safety.resume("comedy") is False                     # comedy was never paused
    assert safety.resume("tech") is True
    with session_scope() as db:
        assert safety.pause_reason(db, channels.channel_by_slug(db, "tech")) is None


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
    monkeypatch.setattr(watch, "_probe", lambda link: {"duration": 900, "live_status": "not_live"})
    out = watch.watch_job(now=datetime(2026, 11, 9, 9, 45, tzinfo=timezone.utc))
    assert out["queued"] == 1
    with session_scope() as db:
        tech = channels.channel_by_slug(db, "tech")
        job = db.execute(select(ClipJob)).scalar_one()
        assert job.channel_id == tech.id and "VID2" in job.source
    # The same upload on the next poll is not queued twice.
    assert watch.watch_job(now=datetime(2026, 11, 9, 9, 50, tzinfo=timezone.utc))["queued"] == 0
    assert _clipjob_count() == 1


def _watching(tmp_path, monkeypatch, feed="https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa"):
    from app.growth import watch

    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_watch_minutes", 5)
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "feeds": [feed]}]})
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("OLD", "2026-11-09T08:00:00+00:00")))
    watch.watch_job(now=datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc))  # first sight
    return watch


def test_watch_leaves_shorts_and_stream_recordings_alone(tmp_path, monkeypatch):
    watch = _watching(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(
        ("SHORT", "2026-11-09T09:30:00+00:00"), ("STREAM", "2026-11-09T09:20:00+00:00")))
    kinds = {"SHORT": {"duration": 45, "live_status": "not_live"},
             "STREAM": {"duration": 7200, "live_status": "was_live"}}
    monkeypatch.setattr(watch, "_probe", lambda link: kinds[link.rsplit("=", 1)[1]])
    assert watch.watch_job(now=datetime(2026, 11, 9, 9, 45, tzinfo=timezone.utc))["queued"] == 0
    assert _clipjob_count() == 0


def test_watch_waits_for_a_premiere_to_air_instead_of_locking_it_out(tmp_path, monkeypatch):
    watch = _watching(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("PREM", "2026-11-09T09:30:00+00:00")))
    state = {"live_status": "is_upcoming"}
    monkeypatch.setattr(watch, "_probe", lambda link: {"duration": 0, **state})
    # Not aired: the clipper would fail it permanently, so it must not be queued or marked seen.
    assert watch.watch_job(now=datetime(2026, 11, 9, 9, 35, tzinfo=timezone.utc))["queued"] == 0
    # Looked at again only after a while, and cut once it has aired.
    state.update(live_status="not_live")
    assert watch.watch_job(now=datetime(2026, 11, 9, 9, 40, tzinfo=timezone.utc))["queued"] == 0
    monkeypatch.setattr(watch, "_probe", lambda link: {"duration": 1200, "live_status": "not_live"})
    assert watch.watch_job(now=datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc))["queued"] == 1


def test_watch_skips_a_source_supply_already_queued(tmp_path, monkeypatch):
    from app.clipper.job import enqueue

    watch = _watching(tmp_path, monkeypatch)
    enqueue("https://www.youtube.com/watch?v=NEW")
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("NEW", "2026-11-09T09:30:00+00:00")))
    monkeypatch.setattr(watch, "_probe", lambda link: {"duration": 900, "live_status": "not_live"})
    assert watch.watch_job(now=datetime(2026, 11, 9, 9, 45, tzinfo=timezone.utc))["queued"] == 0
    assert _clipjob_count() == 1


def test_watch_resolves_a_handle_only_once(tmp_path, monkeypatch):
    from app.growth import watch

    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_watch_minutes", 5)
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "feeds": ["https://www.youtube.com/@sometech"]}]})
    pages = []

    def fetch(url):
        if "feeds/videos.xml" in url:
            return _atom(("VID1", "2026-11-09T08:00:00+00:00"))
        pages.append(url)
        return '<html>"externalId":"UCabcdefghijklmnopqrstuv"</html>'

    monkeypatch.setattr(watch, "_fetch", fetch)
    for minute in (0, 5, 10):
        watch.watch_job(now=datetime(2026, 11, 9, 9, minute, tzinfo=timezone.utc))
    assert len(pages) == 1  # the channel page is fetched once, then the feed address is remembered


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


def test_review_queue_has_a_per_channel_status_strip_when_multi(tmp_path, monkeypatch):
    from app.growth import approval

    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "name": "Tech"}, {"slug": "comedy", "name": "Comedy"}]})
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
    path = tmp_path / "t.mp4"
    path.write_bytes(b"x")
    with session_scope() as db:
        db.add(Video(source="youtube", source_id="t1", source_url="u", title="t", status=VideoStatus.PENDING,
                     transformed_path=str(path), channel_id=tech.id, duration_sec=20, meta={"clip_rendered": True}))
    q = approval.queue()
    strip = {c["slug"]: c for c in q["channels"]}
    assert set(strip) == {"tech", "comedy"}
    assert strip["tech"]["waiting"] == 1 and strip["comedy"]["waiting"] == 0
    assert q["clips"][0]["channel_slug"] == "tech"


def test_doctor_lists_a_configured_but_disabled_channel(tmp_path, monkeypatch):
    from app.growth import report

    secret = tmp_path / "cs.json"
    token = tmp_path / "tok.json"
    secret.write_text("{}")
    token.write_text("{}")
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "client_secrets": str(secret), "token_path": str(token), "enabled": False}]})
    with session_scope() as db:
        channels.sync_channels(db)
    rows = {r["slug"]: r for r in report.doctor(live=False).get("channels", [])}
    assert "tech" in rows and rows["tech"]["enabled"] is False
    # A fully-configured but off channel is the silent "forgot enabled:true" trap — doctor must name it.
    assert "enabled:true" in rows["tech"]["checks"][0]["detail"]


def test_report_has_a_section_per_channel_with_its_own_plan_and_stock(tmp_path, monkeypatch):
    from app.growth import report

    monkeypatch.setattr(settings, "growth_enabled", True)
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "name": "Tech"}, {"slug": "comedy", "name": "Comedy"}]})
    now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
        db.add(GrowthState(key="tech:plan", value=_due_plan(now)))
    _ready_clip(tmp_path, "tech-clip", tech.id)
    out = report.build_report()
    assert set(out["channels"]) == {"tech", "comedy"}
    assert out["channels"]["tech"]["plan"]["posts"] and out["channels"]["comedy"]["plan"] == {}
    assert out["channels"]["tech"]["stock"] == 1 and out["channels"]["comedy"]["stock"] == 0
    assert out["stock"] == 1                                    # totals on top
    # A starving channel is named even though the fleet as a whole has stock.
    assert any(w.startswith("Comedy: No finished clips") for w in out["warnings"])
    assert not any(w.startswith("Tech:") for w in out["warnings"])


def test_report_command_prints_each_channel(tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "name": "Tech"}, {"slug": "comedy", "name": "Comedy"}]})
    with session_scope() as db:
        channels.sync_channels(db)
    spec = importlib.util.spec_from_file_location("growth_cli", Path(__file__).parent.parent / "scripts" / "growth.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.show_report() == 0
    shown = capsys.readouterr().out
    assert "== Tech (tech) ==" in shown and "== Comedy (comedy) ==" in shown and "2 channels." in shown


def test_identity_check_catches_a_token_wired_to_the_wrong_channel():
    from app.growth import report

    class Insights:
        def channel_identity(self):
            return ("UCright", "Right Channel")

    class Chan:
        slug = "tech"
        youtube_channel_id = "UCright"

    assert report._identity_check(Chan(), Insights())[0]["ok"] is True  # matches
    Chan.youtube_channel_id = "UCwrong"  # an account-picker misclick at oauth time
    bad = report._identity_check(Chan(), Insights())[0]
    assert bad["ok"] is False and "DIFFERENT channel" in bad["detail"]


def test_doctor_flags_two_channels_sharing_a_token(tmp_path, monkeypatch):
    from app.growth import report

    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "client_secrets": "secrets/a.json", "token_path": "secrets/shared.json"},
        {"slug": "comedy", "client_secrets": "secrets/b.json", "token_path": "secrets/shared.json"}]})
    with session_scope() as db:
        channels.sync_channels(db)
    check = next(c for c in report.doctor(live=False)["checks"] if c["name"] == "channel credentials")
    assert check["ok"] is False and "shared by comedy, tech" in check["detail"]


def test_channel_status_flags_a_channel_that_needs_attention(tmp_path, monkeypatch):
    from app.growth import approval

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "name": "Tech"}]})
    with session_scope() as db:
        channels.sync_channels(db)
    # No clips in stock and nothing waiting -> the strip must say so, not look like a healthy idle channel.
    tech = next(c for c in approval.queue()["channels"] if c["slug"] == "tech")
    assert tech["attention"] == "No clips in stock"


def test_a_single_legacy_channel_keeps_the_flat_review_view(monkeypatch):
    from app.growth import approval

    monkeypatch.setattr(settings, "channels_file", "")  # legacy single default channel
    q = approval.queue()
    assert q["channels"] == []  # no per-channel strip for one legacy channel


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
