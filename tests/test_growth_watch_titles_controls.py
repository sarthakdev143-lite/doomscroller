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


def test_youtubes_random_feed_404s_and_5xx_are_asked_again_but_real_refusals_are_not():
    def replies(*codes):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url)
            return httpx.Response(codes[min(len(calls) - 1, len(codes) - 1)], content=b"<feed/>")

        return calls, httpx.Client(transport=httpx.MockTransport(handler))

    calls, client = replies(404, 500, 200)
    assert watch._fetch("https://www.youtube.com/feeds/videos.xml", client=client) == "<feed/>" and len(calls) == 3
    calls, client = replies(404)
    assert watch._fetch("https://www.youtube.com/feeds/videos.xml", client=client) is None and len(calls) == watch._ATTEMPTS
    calls, client = replies(403)
    assert watch._fetch("https://www.youtube.com/feeds/videos.xml", client=client) is None and len(calls) == 1


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
    assert out_title == "Why did the engine fail?"
    assert len(out_title) <= 100

    # Leading "Title:" label + inline hashtag stripped cleanly
    monkeypatch.setattr(
        titles.ai,
        "ask_json",
        lambda prompt: {"title": "Title: Hidden detail nobody noticed #trending"},
    )
    out_title2, out_style2 = titles.styled_title("curiosity", "Original Title", "")
    assert out_style2 == "curiosity"
    assert out_title2 == "Hidden detail nobody noticed"

    # Refusals, hashtag-only, non-dict JSON, and raised exceptions fall back to ("...", "plain")
    for bad_reply in (
        {"title": "I cannot fulfill this request as an AI assistant."},
        {"title": "Sorry, I can't write a title from this transcript."},
        {"title": "#Shorts #viral"},
        [{"title": "Wrong JSON shape"}],
    ):
        monkeypatch.setattr(titles.ai, "ask_json", lambda prompt, r=bad_reply: r)
        fallback_t, fallback_s = titles.styled_title("question", "Safe Original", "line")
        assert fallback_s == "plain" and fallback_t == "Safe Original"

    def raise_ai(prompt):
        raise RuntimeError("AI CLI crashed")

    monkeypatch.setattr(titles.ai, "ask_json", raise_ai)
    assert titles.styled_title("curiosity", "Safe Original", "line") == ("Safe Original", "plain")

    # Quote style ignores blank/quote-only opening lines instead of emitting '""'
    assert titles.styled_title("quote", "Safe Original", "   ") == ("Safe Original", "plain")
    assert titles.styled_title("quote", "Safe Original", '""') == ("Safe Original", "plain")


def test_title_rewrite_matches_the_text_language(monkeypatch):
    seen = {}

    def fake_ask(prompt):
        seen["prompt"] = prompt
        return {"title": "Piyush Shocked by Vietnam Surprise"}

    monkeypatch.setattr(titles.ai, "ask_json", fake_ask)
    out_title, out_style = titles.styled_title("question", "My title", "Piyush ko Vietnam trip ka surprise mila")
    assert out_style == "question" and out_title == "Piyush Shocked by Vietnam Surprise"
    assert "Hinglish" in seen["prompt"]


def test_english_titles_and_comments_block_hinglish_leaks(monkeypatch):
    seen = []

    def fake_ask(prompt):
        seen.append(prompt)
        return {"title": "Kitna masala daalna chahiye khane mein?"}

    monkeypatch.setattr(titles.ai, "available", lambda: True)
    monkeypatch.setattr(titles.ai, "ask_json", fake_ask)

    out_title, out_style = titles.styled_title(
        "question", "If You Ain't Sneezing, It Ain't Seasoned", "Add more spice", language="en"
    )
    assert out_style == "plain"
    assert out_title == "If You Ain't Sneezing, It Ain't Seasoned"
    for forbidden in ("Hinglish", "bhoot", "kachche"):
        assert forbidden not in seen[0]

    monkeypatch.setattr(
        titles.ai,
        "ask_json",
        lambda prompt: {"title": "Bola 'Very Bad Employee', phir Mr Singh ke saath kya kiya?"},
    )
    out2, style2 = titles.styled_title(
        "curiosity", "He Said I Was Very Bad Employee", "Wait for it", language="en"
    )
    assert (out2, style2) == ("He Said I Was Very Bad Employee", "plain")

    assert titles.styled_title("quote", "English Title", "Bhai aaj hadh ho gayi", language="en") == (
        "English Title",
        "plain",
    )

    monkeypatch.setattr(titles.ai, "ask_json", lambda prompt: {"comment": "Bhai aaj hadh kar di yaar!"})
    comment = titles.engagement_comment("Cool Video", language="en")
    assert comment == "Be honest — what would you have done here?"


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


def test_ambient_engagement_comment_and_controls_cleanup_summary(monkeypatch):
    monkeypatch.setattr(titles.ai, "available", lambda: False)
    off = titles.engagement_comment("it's late, go to bed.", is_long=True, is_ambient=True)
    assert "#1" not in off and ("time" in off.lower() or "tonight" in off.lower())

    seen_prompts: list[str] = []
    monkeypatch.setattr(titles.ai, "available", lambda: True)
    monkeypatch.setattr(
        titles.ai,
        "ask_json",
        lambda p: seen_prompts.append(p) or {"comment": "1:30 AM here, raining outside and studying."},
    )
    on = titles.engagement_comment("it's late, go to bed.", is_long=True, is_ambient=True)
    assert "1:30 AM" in on
    assert "ambient" in seen_prompts[0].lower() or "lofi" in seen_prompts[0].lower()

    summary = controls._summarise({"transforms_deleted": 3, "cache_deleted": 5, "mb_reclaimed": 12.5})
    assert "renders removed" in summary
    assert "cache files removed" in summary


def test_watch_skips_permanently_unavailable_video_and_youtube_scrubs_tts_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import yt_dlp

    def fake_ydl_cls(opts):
        class _YDL:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, **kw):
                if "DEAD_EMBED" in url:
                    raise RuntimeError("ERROR: [youtube] DEAD_EMBED: Video unavailable. Playback on other websites has been disabled by the video owner")
                if "UPCOMING" in url:
                    raise RuntimeError("ERROR: [youtube] UPCOMING: Premieres in 30 minutes")
                raise RuntimeError("Connection timed out")

        return _YDL()

    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake_ydl_cls)

    p_dead = watch._probe("https://www.youtube.com/watch?v=DEAD_EMBED")
    assert p_dead == {"unavailable": True}
    assert watch._verdict(p_dead) == "skip"
    assert watch._verdict(watch._probe("https://www.youtube.com/watch?v=UPCOMING")) == "later"
    assert watch._verdict(watch._probe("https://www.youtube.com/watch?v=NET_ERR")) == "later"

    ch_file = tmp_path / "channels.json"
    feed_url = "https://www.youtube.com/channel/UCbbbbbbbbbbbbbbbbbbbbbb"
    ch_file.write_text(json.dumps({"channels": [{"slug": "vlogs", "feeds": [feed_url]}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(ch_file))
    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("SEED1", "2026-11-09T08:00:00+00:00")))
    watch.watch_job(now=START)

    monkeypatch.setattr(watch, "_fetch", lambda url: _atom(("DEAD_EMBED", "2026-11-09T09:20:00+00:00")))
    res = watch.watch_job(now=START + timedelta(minutes=25))
    assert res == {"feeds": 1, "queued": 0}
    with session_scope() as db:
        st = db.get(GrowthState, watch._state_key(feed_url))
        assert "DEAD_EMBED" in (st.value or {}).get("seen", [])
        assert "DEAD_EMBED" not in (st.value or {}).get("rechecks", {})

    monkeypatch.setattr(settings, "sarvam_api_key", "sk_sarvam_w10_111")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "el_key_w10_222")
    monkeypatch.setattr(settings, "deepgram_api_key", "dg_key_w10_333")
    scrubbed = youtube.scrub_secrets("err sk_sarvam_w10_111 and el_key_w10_222 and dg_key_w10_333")
    for s in ("sk_sarvam_w10_111", "el_key_w10_222", "dg_key_w10_333"):
        assert s not in scrubbed


def test_wave15_controls_per_channel_errors_safe_engagement_comment_and_scrubbed_collect_safety(
    tmp_path, monkeypatch
):
    from app.growth import collect

    # A. controls._summarise surfaces per-channel errors with and without counts
    summary_err = controls._summarise(
        {"default": {"skipped": "nothing published"}, "indianvlogs": {"error": "token expired"}}
    )
    assert "indianvlogs: token expired" in summary_err

    summary_both = controls._summarise(
        {"default": {"snapshots": 4}, "indianvlogs": {"error": "quota exceeded"}}
    )
    assert "4 stats updated" in summary_both
    assert "indianvlogs: quota exceeded" in summary_both

    # B. titles.engagement_comment rejects non-strings and risky words
    monkeypatch.setattr(titles.ai, "available", lambda: True)
    monkeypatch.setattr(titles.ai, "ask_json", lambda _p: {"comment": True})
    assert (
        titles.engagement_comment("Cool Video", language="en")
        == "Be honest — what would you have done here?"
    )
    monkeypatch.setattr(titles.ai, "ask_json", lambda _p: {"comment": ["bad list"]})
    assert (
        titles.engagement_comment("Cool Video", language="en")
        == "Be honest — what would you have done here?"
    )
    monkeypatch.setattr(titles.ai, "ask_json", lambda _p: {"comment": "Was that ghost murder scene real?"})
    assert (
        titles.engagement_comment("Cool Video", language="en")
        == "Be honest — what would you have done here?"
    )
    monkeypatch.setattr(titles.ai, "ask_json", lambda _p: {"comment": "Kya sach me bhoot tha wahan?"})
    assert (
        titles.engagement_comment("Desi Vlog", language="hi")
        == "Sach batao, is par aapka kya reaction hota?"
    )

    # C. safety.set_hold and collect error/event helpers scrub secrets
    monkeypatch.setattr(settings, "proxy_url", "http://u:CANARY_COLLECT_W15@10.0.0.1:8080")
    now = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        safety.set_hold(db, now, "proxy http://u:CANARY_COLLECT_W15@10.0.0.1:8080 denied")
    with session_scope() as db:
        hold_state = db.get(GrowthState, "hold")
        assert "CANARY_COLLECT_W15" not in str((hold_state.value or {}).get("reason", ""))
        ev_hold = db.execute(select(GrowthEvent).where(GrowthEvent.kind == "account_hold")).scalar_one()
        assert "CANARY_COLLECT_W15" not in ev_hold.summary

    collect._note_stats_failure(RuntimeError("http://u:CANARY_COLLECT_W15@10.0.0.1:8080 failed"))
    collect._note_analytics_failure(RuntimeError("http://u:CANARY_COLLECT_W15@10.0.0.1:8080 failed"), now)
    with session_scope() as db:
        st_collect = db.get(GrowthState, "collect")
        assert "CANARY_COLLECT_W15" not in str((st_collect.value or {}).get("analytics_error", ""))
        evs = db.execute(
            select(GrowthEvent).where(GrowthEvent.kind.in_(("collect_failed", "analytics_failed")))
        ).scalars().all()
        assert len(evs) == 2
        for ev in evs:
            assert "CANARY_COLLECT_W15" not in ev.summary


