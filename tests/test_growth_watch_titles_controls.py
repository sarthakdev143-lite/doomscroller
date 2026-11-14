"""Wave 10 TDD audit tests for watch, youtube, titles, channels, and controls."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels, controls, notify, safety, titles, watch, youtube
from app.models import (
    Channel,
    ClipJob,
    Feed,
    GrowthEvent,
    GrowthState,
    MetadataDraft,
    QuotaUsage,
    Video,
    VideoSnapshot,
    VideoStatus,
)

START = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (
            Feed,
            Channel,
            VideoSnapshot,
            MetadataDraft,
            GrowthEvent,
            GrowthState,
            ClipJob,
            QuotaUsage,
            Video,
        ):
            db.execute(delete(table))
    monkeypatch.setattr(settings, "channels_file", "")
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_require_approval", False)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "growth_watch_minutes", 5)
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "use_mock", True)


def _atom(*entries: tuple[str, str]) -> str:
    rows = "".join(
        f"<entry><yt:videoId>{vid}</yt:videoId><published>{when}</published></entry>"
        for vid, when in entries
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        f'xmlns:yt="http://www.youtube.com/xml/schemas/2015">{rows}</feed>'
    )


# ---------- 1. watch.py: _fetch compat & size bound, XML/timestamp guards, dedup ----------


def test_watch_preserves_1arg_and_2arg_fetch_bounds_response_size_and_handles_malformed_xml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Oversized streamed response must be aborted safely (returning None)
    def huge_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (3 * 1024 * 1024))

    with httpx.Client(transport=httpx.MockTransport(huge_handler)) as mock_client:
        assert watch._fetch("https://example.com/huge.xml", client=mock_client) is None

    # Invalid URL (ValueError, not HTTPError) must return None instead of raising
    assert watch._fetch("http://[::1") is None

    # Entity-expansion / malformed XML / unsafe videoId / missing timestamps -> safe entries
    entity_xml = '<!DOCTYPE lolz [<!ENTITY lol "lol">]><feed xmlns="http://www.w3.org/2005/Atom">&lol;</feed>'
    assert watch._entries(entity_xml) == []
    mixed_xml = _atom(
        ("GOOD_VID", "2026-11-09T09:30:00"),  # naive timestamp
        ("BAD?inject=1", "2026-11-09T09:30:00+00:00"),  # unsafe videoId chars
        ("   ", "2026-11-09T09:30:00+00:00"),
    )
    parsed = watch._entries(mixed_xml)
    assert [vid for vid, _ in parsed] == ["GOOD_VID"]
    assert parsed[0][1].tzinfo is not None

    ch_file = tmp_path / "channels.json"
    ch_file.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "slug": "tech",
                        "feeds": ["https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "channels_file", str(ch_file))

    # 1-arg _fetch override + naive `now`
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("SEED1", "2026-11-09T08:00:00")))
    assert watch.watch_job(now=datetime(2026, 11, 9, 9, 0, 0)) == {"feeds": 1, "queued": 0}

    # Store a naive recheck timestamp in GrowthState to ensure _watch_feed normalizes it without TypeError
    feed_key = watch._state_key("https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa")
    with session_scope() as db:
        st = db.get(GrowthState, feed_key)
        st.value = {**st.value, "rechecks": {"FRESH2": "2026-11-09T09:00:00"}}

    # 2-arg _fetch override (client keyword supported) + naive `now`
    seen_clients = []

    def fetch_2arg(url: str, *, client=None) -> str:
        seen_clients.append(client)
        return _atom(("FRESH2", "2026-11-09T09:30:00"), ("SEED1", "2026-11-09T08:00:00"))

    monkeypatch.setattr(watch, "_fetch", fetch_2arg)
    monkeypatch.setattr(watch, "_probe", lambda link: {"duration": 600, "live_status": "not_live"})
    out = watch.watch_job(now=datetime(2026, 11, 9, 9, 45, 0))
    assert out == {"feeds": 1, "queued": 1}
    assert seen_clients and seen_clients[0] is not None


def test_watch_prevents_duplicate_video_ingestion_across_xml_duplicates_url_variants_and_existing_videos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ch_file = tmp_path / "channels.json"
    ch_file.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "slug": "tech",
                        "feeds": ["https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "channels_file", str(ch_file))

    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("SEED1", "2026-11-09T08:00:00+00:00")))
    watch.watch_job(now=START)

    # Pre-existing ClipJob using youtu.be shortlink for VID_SHORT, pre-existing Video for VID_CLIPPED,
    # and blocked parent for VID_BLOCKED_PARENT
    with session_scope() as db:
        db.add(ClipJob(source="https://youtu.be/VID_SHORT?si=abc", status="done"))
        db.add(
            Video(
                source="youtube",
                source_id="VID_CLIPPED_0",
                source_url="https://www.youtube.com/watch?v=VID_CLIPPED",
                title="Already cut",
                status=VideoStatus.TRANSFORMED,
                meta={"clip_rendered": True, "clip_parent": "youtube:VID_CLIPPED"},
            )
        )
        struck = Video(
            source="youtube",
            source_id="struck_0",
            source_url="https://youtu.be/other_url",
            title="Struck",
            status=VideoStatus.PUBLISHED,
            youtube_video_id="yt_struck",
            meta={"clip_rendered": True, "clip_parent": "youtube:VID_STRUCK"},
        )
        db.add(struck)
        db.flush()
        safety.record_strike(struck, "copyright", now=START)

    # Atom feed has duplicate entries for VID_UNIQUE plus the 3 already-ingested/blocked IDs
    monkeypatch.setattr(
        watch,
        "_fetch",
        lambda url: _atom(
            ("VID_UNIQUE", "2026-11-09T09:20:00+00:00"),
            ("VID_UNIQUE", "2026-11-09T09:20:00+00:00"),
            ("VID_SHORT", "2026-11-09T09:21:00+00:00"),
            ("VID_CLIPPED", "2026-11-09T09:22:00+00:00"),
            ("VID_STRUCK", "2026-11-09T09:23:00+00:00"),
        ),
    )
    monkeypatch.setattr(watch, "_probe", lambda link: {"duration": 900, "live_status": "not_live"})
    res = watch.watch_job(now=START + timedelta(minutes=30))
    assert res["queued"] == 1
    with session_scope() as db:
        sources = [j.source for j in db.execute(select(ClipJob)).scalars()]
    assert sources.count("https://www.youtube.com/watch?v=VID_UNIQUE") == 1
    assert len(sources) == 2  # only the pre-existing youtu.be job + VID_UNIQUE


# ---------- 2. youtube.py: 50-ID batching, dedup/MOCK skip, ISO-8601 duration, secret scrubbing ----------


def test_youtube_insights_batches_50_dedupes_skips_mock_and_blank_and_parses_iso_durations():
    assert youtube.YouTubeInsights is youtube.Insights

    assert youtube.parse_duration("PT45S") == 45
    assert youtube.parse_duration("PT1M2S") == 62
    assert youtube.parse_duration("PT1H2M3S") == 3723
    assert youtube.parse_duration("PT1M2.9S") == 62
    assert youtube.parse_duration("P1DT2H") == 93600
    assert youtube.parse_duration("P2W") == 1209600
    assert youtube.parse_duration("P0D") == 0
    assert youtube.parse_duration("PT0S") == 0
    assert youtube.parse_duration("PT") == 0
    assert youtube.parse_duration("P") == 0
    assert youtube.parse_duration("not-iso") == 0
    assert youtube.parse_duration("") == 0
    assert youtube.parse_duration(None) == 0

    batches: list[list[str]] = []
    client_calls = 0

    class FakeVideosReq:
        def __init__(self, ids_csv: str):
            self._ids = ids_csv.split(",")

        def execute(self):
            batches.append(self._ids)
            return {
                "items": [
                    {
                        "id": vid,
                        "statistics": {"viewCount": "10", "likeCount": None},
                        "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                        "contentDetails": {"duration": "PT35S"},
                    }
                    for vid in self._ids
                ]
            }

    class FakeClient:
        def videos(self):
            return self

        def list(self, **kwargs):
            return FakeVideosReq(kwargs["id"])

    class FakePub:
        def _client(self):
            nonlocal client_calls
            client_calls += 1
            return FakeClient()

    ins = youtube.YouTubeInsights(FakePub())
    # Empty / blank / MOCK_ only -> must NOT even initialize OAuth _client()
    assert ins.stats(["", "   ", "MOCK_123"]) == {}
    assert client_calls == 0

    # 65 real IDs + duplicates + blanks + MOCK_ -> 2 batches (50 + 15)
    raw_ids = [f"vid_{i}" for i in range(65)] + ["vid_0", "vid_1", "", "   ", "MOCK_demo"]
    out = ins.stats(raw_ids)
    assert len(batches) == 2 and len(batches[0]) == 50 and len(batches[1]) == 15
    assert len(out) == 65
    assert out["vid_0"]["duration"] == 35
    assert out["vid_0"]["likes"] == 0


def test_youtube_insights_scrubs_api_keys_and_tokens_from_exceptions_and_handles_naive_since():
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    api_secret = "AIzaSySUPER_SECRET_KEY_999"
    oauth_secret = "ya29.SUPER_SECRET_ACCESS_TOKEN"

    class BoomClient:
        def videos(self):
            return self

        def channels(self):
            return self

        def playlistItems(self):
            return self

        def list(self, **kwargs):
            if "playlistId" in kwargs:
                return self
            if kwargs.get("part") == "contentDetails":
                return FakeExec({"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]})
            err = HttpError(
                Response({"status": 403, "reason": "Forbidden"}),
                b'{"error": {"message": "forbidden"}}',
                uri=f"https://youtube.googleapis.com/youtube/v3/videos?key={api_secret}&access_token={oauth_secret}",
            )
            raise err

        def execute(self):
            return {
                "items": [
                    {
                        "snippet": {
                            "resourceId": {"videoId": "u1"},
                            "title": "Clip 1",
                            "publishedAt": "2026-11-08T10:00:00Z",
                        }
                    }
                ],
                "nextPageToken": "NEXT",
            }

    class FakeExec:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload

    class BoomPub:
        def _client(self):
            return BoomClient()

    ins = youtube.Insights(BoomPub())
    with pytest.raises(HttpError) as caught:
        ins.stats(["vid_1"])
    rendered = str(caught.value)
    assert api_secret not in rendered
    assert oauth_secret not in rendered

    # Naive `since` compared against aware `publishedAt` stops pagination cleanly without TypeError
    uploads = ins.uploads_since(since=datetime(2026, 11, 9, 0, 0, 0))
    assert len(uploads) == 1 and uploads[0]["id"] == "u1"


# ---------- 3. titles.py: sanitize quotes, fences, hashtags, control chars, length, refusals ----------


def test_titles_sanitizes_quotes_fences_hashtags_control_chars_length_and_refusals(monkeypatch):
    # Wrapped in markdown code fences + quotes + hashtags + control chars
    monkeypatch.setattr(
        titles.ai,
        "ask_json",
        lambda prompt: {"title": '```\n"Why did the engine\x00 fail? #Shorts #viral #fyp"\n```'},
    )
    out_title, out_style = titles.styled_title("question", "Original Title", "The engine blew up")
    assert out_style == "question"
    assert out_title == "Why did the engine fail? #Shorts"
    assert len(out_title) <= 100

    # Leading "Title:" label + inline hashtag stripped cleanly
    monkeypatch.setattr(
        titles.ai,
        "ask_json",
        lambda prompt: {"title": "Title: Hidden detail nobody noticed #trending"},
    )
    out_title2, out_style2 = titles.styled_title("curiosity", "Original Title", "")
    assert out_style2 == "curiosity"
    assert out_title2 == "Hidden detail nobody noticed #Shorts"

    # Refusals, hashtag-only, non-dict JSON, and raised exceptions fall back to ("...", "plain")
    for bad_reply in (
        {"title": "I cannot fulfill this request as an AI assistant."},
        {"title": "Sorry, I can't write a title from this transcript."},
        {"title": "#Shorts #viral"},
        [{"title": "Wrong JSON shape"}],
    ):
        monkeypatch.setattr(titles.ai, "ask_json", lambda prompt, r=bad_reply: r)
        fallback_t, fallback_s = titles.styled_title("question", "Safe Original", "line")
        assert fallback_s == "plain" and fallback_t == "Safe Original #Shorts"

    def raise_ai(prompt):
        raise RuntimeError("AI CLI crashed")

    monkeypatch.setattr(titles.ai, "ask_json", raise_ai)
    assert titles.styled_title("curiosity", "Safe Original", "line") == ("Safe Original #Shorts", "plain")

    # Quote style ignores blank/quote-only opening lines instead of emitting '"" #Shorts'
    assert titles.styled_title("quote", "Safe Original", "   ") == ("Safe Original #Shorts", "plain")
    assert titles.styled_title("quote", "Safe Original", '""') == ("Safe Original #Shorts", "plain")


# ---------- 4. channels.py: URL normalization, case-insensitive dedup, numeric bounds ----------


def test_channels_normalizes_youtube_urls_dedupes_case_insensitively_and_validates_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ch_path = tmp_path / "channels.json"
    monkeypatch.setattr(settings, "channels_file", str(ch_path))
    monkeypatch.setattr(settings, "growth_feeds", "[]")

    uc_id = "UCBJycsmduvYEL83R_U4JriQ"
    channels.add_channel(
        "tech",
        name="Tech",
        feeds=(
            "@mkbhd",
            "https://m.youtube.com/@MKBHD/videos?si=tracking123",
            uc_id,
            f"https://www.youtube.com/channel/{uc_id}/featured?view=0",
        ),
    )
    with session_scope() as db:
        tech = channels.channel_by_slug(db, "tech")
        saved_feeds = channels.feeds_for(db, tech)
    assert saved_feeds == [
        "https://www.youtube.com/@mkbhd",
        f"https://www.youtube.com/channel/{uc_id}",
    ]

    # Case-insensitive dedup on add, then shorthand/case-insensitive removal
    channels.change_feeds("tech", add=("https://youtube.com/@MKBHD/", "@MarkRober"))
    with session_scope() as db:
        tech = channels.channel_by_slug(db, "tech")
        assert channels.feeds_for(db, tech) == [
            "https://www.youtube.com/@mkbhd",
            f"https://www.youtube.com/channel/{uc_id}",
            "https://www.youtube.com/@MarkRober",
        ]
    channels.change_feeds("tech", remove=("@MKBHD",))
    with session_scope() as db:
        tech = channels.channel_by_slug(db, "tech")
        updated_feeds = channels.feeds_for(db, tech)
    assert updated_feeds == [
        f"https://www.youtube.com/channel/{uc_id}",
        "https://www.youtube.com/@MarkRober",
    ]

    # Numeric & timezone bounds validation on update_channel / add_channel
    for bad_kwargs in (
        {"uploads_per_day": -1},
        {"uploads_per_day": 51},
        {"uploads_per_day": True},
        {"auto_approve_score": -0.05},
        {"auto_approve_score": 1.01},
        {"auto_approve_score": float("nan")},
        {"auto_approve_score": float("inf")},
        {"timezone": "   "},
    ):
        with pytest.raises(channels.ChannelsConfigError):
            channels.update_channel("tech", **bad_kwargs)


# ---------- 5. controls.py: test_notify action registration & safe summary ----------


def test_controls_registers_test_notify_action_and_summarises_delivery_without_secret_leaks(
    monkeypatch: pytest.MonkeyPatch,
):
    assert "test_notify" in controls.ACTIONS

    monkeypatch.setattr(settings, "ntfy_topic", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    unconf = controls.trigger("test_notify")
    assert unconf["state"] == "done"
    assert "NTFY_TOPIC" in unconf["result"] or "Telegram" in unconf["result"]

    bot_secret = "777888:SECRET_CTRL_BOT_TOKEN"
    monkeypatch.setattr(settings, "ntfy_topic", "alerts-topic")
    monkeypatch.setattr(settings, "telegram_bot_token", bot_secret)
    monkeypatch.setattr(settings, "telegram_chat_id", "12345")

    class OkResp:
        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        if "telegram.org" in url:
            raise httpx.ConnectError(f"failed {url}")
        return OkResp()

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    done = controls.trigger("test_notify")
    assert done["state"] == "done"
    assert "ntfy: sent" in done["result"]
    assert "telegram: failed" in done["result"]
    assert bot_secret not in json.dumps(controls.status())
    assert "SECRET_CTRL_BOT_TOKEN" not in json.dumps(controls.status())


def test_wave10_owned_modules_preserve_numpy_free_idle_import():
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, app.main, app.review, app.growth.watch, app.growth.youtube, "
            "app.growth.titles, app.growth.channels, app.growth.controls; "
            "assert 'numpy' not in sys.modules, f'numpy loaded: {list(sys.modules)}'",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

