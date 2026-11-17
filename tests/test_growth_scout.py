"""The lab across creators: losers are benched, supply favours what wins, and the scout finds newcomers."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels, learn, scout, supply, watch
from app.models import Channel, ClipJob, Feed, GrowthEvent, GrowthState, Video

GOOD, BAD, NEW = (f"https://www.youtube.com/@{handle}" for handle in ("Good", "Bad", "New"))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (Feed, Channel, GrowthEvent, GrowthState, ClipJob, Video):
            db.execute(delete(table))
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_watch_minutes", 5)
    monkeypatch.setattr(settings, "growth_feeds", "[]")


def _row(arm, n, high):
    return {"arm": arm, "n": n, "multiplier": 1.0, "low": 0.1, "high": high, "p_best": 0.1,
            "score": {"multiplier": 1.0, "low": 0.1, "high": high, "p_best": 0.1}}


def _bench_bad():
    with session_scope() as db:
        learn.set_state(db, "summary", {"series": [_row("@Bad", 5, 0.8)]})


class _Draw:
    def __init__(self, values):
        self.values = values

    def arm(self, factor, arm):
        return self.values.get(arm, 0.0)


def _policy(rate, tried, values):
    return SimpleNamespace(rate=rate, tried={"series": tried}, draw=_Draw(values), rng=random.Random(1))


def test_a_creator_is_benched_only_once_tried_enough_and_clearly_worse():
    assert channels.creator("https://www.youtube.com/@Good/videos") == "@Good"
    with session_scope() as db:
        learn.set_state(db, "summary", {"series": [_row("@Bad", 5, 0.8), _row("@Early", 2, 0.5), _row("@Fine", 6, 1.4)]})
    with session_scope() as db:
        assert learn.benched(db, None) == {"@bad"}


def test_supply_drops_benched_creators_and_favours_what_the_lab_backs(monkeypatch):
    monkeypatch.setattr(supply, "policy", lambda channel=None: _policy(0.0, {}, {}))
    _bench_bad()
    assert supply._feeds_by_turn({}, [BAD, GOOD]) == [GOOD]
    assert supply._feeds_by_turn({}, [BAD]) == [BAD]  # an empty shelf misses posts; a weak creator still fills it
    backed = _policy(0.0, {"@Good": 2, "@New": 1}, {"@Good": 0.1, "@New": 0.9})
    monkeypatch.setattr(supply, "policy", lambda channel=None: backed)
    assert supply._feeds_by_turn({}, [GOOD, NEW]) == [NEW, GOOD]
    backed.rate = 1.0  # an exploring turn keeps the fair rotation
    assert supply._feeds_by_turn({}, [GOOD, NEW]) == [GOOD, NEW]


def test_watch_leaves_a_benched_creator_alone(monkeypatch):
    monkeypatch.setattr(settings, "growth_feeds", json.dumps([GOOD, BAD]))
    _bench_bad()
    polled = []
    monkeypatch.setattr(watch, "_watch_feed", lambda channel, feed, now, client=None: polled.append(feed) or [])
    assert watch.watch_job()["feeds"] == 1 and polled == [GOOD]


def _feed_xml(*rows):
    entries = "".join(f"<entry><yt:videoId>{vid}</yt:videoId><published>{when}</published>"
                      f'<link rel="alternate" href="https://www.youtube.com/{"shorts/" if vid.startswith("short") else "watch?v="}{vid}"/>'
                      f"<media:group><media:community>"
                      f'<media:statistics views="{views}"/></media:community></media:group></entry>' for vid, when, views in rows)
    return ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns:media="http://search.yahoo.com/mrss/">{entries}</feed>')


def test_the_feed_parser_reads_each_uploads_views_and_drops_shorts():
    xml = _feed_xml(("old1", "2026-11-01T00:00:00+00:00", 500), ("new1", "2026-11-02T00:00:00+00:00", 9000),
                    ("short1", "2026-11-03T00:00:00+00:00", 99000))
    assert [(vid, views) for vid, _when, views in watch._parse(xml)] == [("new1", 9000), ("old1", 500)]
    assert [vid for vid, _when in watch._entries(xml)] == ["new1", "old1"]


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def test_supply_cuts_what_viewers_are_rushing_to_not_just_the_newest(monkeypatch):
    uploads = {
        GOOD: [("calm", NOW - timedelta(hours=10), 100_000),       # newest, 10k an hour: the creator's normal
               ("viral", NOW - timedelta(hours=40), 4_000_000),    # 100k an hour: a breakout
               ("usual", NOW - timedelta(hours=60), 300_000)],
        BAD: [("huge", NOW - timedelta(hours=10), 9_000_000)],     # hottest of all, but the lab benched its creator
        "https://example.com/feed": [("unnamed", NOW, 10)],
    }
    long_video = {"duration": 900, "live_status": "not_live", "authors": set()}
    monkeypatch.setattr(supply.watch, "recent_uploads", lambda feed, client=None: uploads.get(feed, []))
    monkeypatch.setattr(supply.watch, "_probe", lambda link: long_video)
    monkeypatch.setattr(supply, "policy", lambda channel=None: _policy(0.0, {}, {}))
    _bench_bad()
    feeds = [GOOD, BAD, "https://example.com/feed"]
    assert supply._hottest(None, feeds, {}, set(), NOW) == ("https://www.youtube.com/watch?v=viral", GOOD)

    cut = {"https://www.youtube.com/watch?v=viral": [SimpleNamespace(status="done", result={}, finished_at=NOW, created_at=NOW)]}
    assert supply._hottest(None, feeds, cut, set(), NOW)[0].endswith("=calm")
    monkeypatch.setattr(supply.watch, "_probe", lambda link: {**long_video, "duration": 30 if link.endswith("viral") else 900})
    assert supply._hottest(None, feeds, {}, set(), NOW)[0].endswith("=calm")  # a Short is no source

    monkeypatch.setattr(supply, "policy", lambda channel=None: _policy(1.0, {}, {}))
    assert supply._hottest(None, feeds, {}, set(), NOW) is None  # an exploring turn keeps the fair rotation


def test_search_keeps_long_uploads_one_row_per_channel_most_viewed_first(monkeypatch):
    import yt_dlp

    entries = [{"uploader_id": "@A", "channel": "A", "duration": 900, "view_count": 50, "title": "a1"},
               {"uploader_id": "@B", "channel": "B", "duration": 900, "view_count": 90, "title": "b1"},
               {"uploader_id": "@a", "channel": "A", "duration": 600, "view_count": 70, "title": "a2"},
               {"uploader_id": "@C", "channel": "C", "duration": 30, "view_count": 999, "title": "a short"},
               {"channel": "D", "duration": 900, "view_count": 999, "title": "no handle"}, None]

    class FakeYdl:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            assert "sp=" in url  # this week's uploads, most viewed first
            return {"entries": entries}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYdl)
    found = scout._search("hindi vlog")
    assert [(c["handle"], c["views"], c["titles"]) for c in found] == [("@B", 90, ["b1"]), ("@A", 70, ["a1", "a2"])]


def _named(tmp_path, monkeypatch):
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"channels": [{"slug": "default"}, {"slug": "vlogs", "niche": "hindi vlog", "feeds": [GOOD]}]}),
                    encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(path))
    with session_scope() as db:
        channels.sync_channels(db)
    return path


def _found(*handles):
    return [{"handle": h, "name": h.lstrip("@"), "views": 1000 - i, "titles": [f"{h} vlog"]} for i, h in enumerate(handles)]


def test_the_scout_adds_the_vetted_newcomer_and_never_brings_back_a_removed_one(tmp_path, monkeypatch):
    path = _named(tmp_path, monkeypatch)
    monkeypatch.setattr(scout.ai, "available", lambda: True)
    monkeypatch.setattr(scout, "_search", lambda query: _found("@Good", "@TVNetwork", "@New"))
    shown = []

    def vet(prompt):
        shown.append(prompt)
        return {"pick": 1, "why": "an independent hindi vlogger"}

    monkeypatch.setattr(scout.ai, "ask_json", vet)
    out = scout.scout_job()
    assert out["default"] == {"skipped": "no niche to search for"}
    assert out["vlogs"]["added"] == NEW
    assert "Refuse TV networks" in shown[0] and "[0] TVNetwork" in shown[0] and "(@Good)" not in shown[0]
    assert NEW in json.loads(path.read_text())["channels"][1]["feeds"]
    with session_scope() as db:
        event = db.execute(select(GrowthEvent).where(GrowthEvent.kind == "creator_added")).scalar_one()
    assert event.summary == "Trying a new creator, New (@New): an independent hindi vlogger"

    channels.change_feeds("vlogs", remove=(NEW, GOOD))  # one the scout added, one the owner did
    assert scout.scout_job()["vlogs"] == {"skipped": "a creator was already added in the last day"}
    with session_scope() as db:
        db.get(GrowthEvent, event.id).created_at = datetime.now(timezone.utc) - timedelta(days=1, minutes=1)
    assert scout.scout_job()["vlogs"]["added"] is None  # only @TVNetwork is left to offer, at index 0
    assert "(@New)" not in shown[1] and "(@Good)" not in shown[1]


def test_the_scout_never_adds_unvetted_or_past_the_cap(tmp_path, monkeypatch):
    _named(tmp_path, monkeypatch)
    monkeypatch.setattr(scout, "_search", lambda query: pytest.fail("no search without a way to vet"))
    monkeypatch.setattr(scout.ai, "available", lambda: False)
    assert "no AI" in scout.scout_job()["vlogs"]["skipped"]
    monkeypatch.setattr(scout.ai, "available", lambda: True)
    monkeypatch.setattr(scout, "_MAX_LIVE_FEEDS", 1)
    assert "already in the running" in scout.scout_job()["vlogs"]["skipped"]
    monkeypatch.setattr(scout, "_search", lambda query: _found("@TVNetwork"))
    monkeypatch.setattr(scout.ai, "ask_json", lambda prompt: {"pick": None, "why": "a TV network"})
    monkeypatch.setattr(scout, "_MAX_LIVE_FEEDS", 12)
    assert scout.scout_job()["vlogs"] == {"found": 1, "added": None, "why": "a TV network"}


def test_the_env_channel_takes_scouted_creators_alongside_growth_feeds(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_feeds", json.dumps([GOOD]))
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"channels": [{"slug": "default", "niche": "challenge"}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(path))
    with session_scope() as db:
        channels.sync_channels(db)
    monkeypatch.setattr(scout.ai, "available", lambda: True)
    monkeypatch.setattr(scout, "_search", lambda query: _found("@Good", "@New", "@Vlogger"))
    monkeypatch.setattr(scout.ai, "ask_json", lambda prompt: {"pick": 0, "why": "fits"})
    channels.add_channel("vlogs", feeds=("https://www.youtube.com/@Vlogger",))
    assert scout.scout_job()["default"]["added"] == NEW  # @Good is its own, @Vlogger is another channel's
    with session_scope() as db:
        assert channels.feeds_for(db, channels.channel_by_slug(db, "default")) == [GOOD, NEW]
    with pytest.raises(channels.ChannelsConfigError):  # a creator still feeds one channel only
        channels.change_feeds("vlogs", add=(NEW,))
