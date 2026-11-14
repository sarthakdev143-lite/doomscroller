"""Reliability, edge-case, and regression tests for app/scrapers/."""
from __future__ import annotations

import itertools
import json
import os
import subprocess
from datetime import timezone
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.scrapers.base import ScrapedItem, Scraper
from app.scrapers.mock_scraper import MockScraper, _ensure_master_dummy
from app.scrapers.snapchat_scraper import SnapchatScraper, _flt, _num
from app.scrapers.yt_dlp_scraper import YtDlpScraper


# ===========================================================================
# Base Scraper & ScrapedItem invariants
# ===========================================================================
def test_scraped_item_negative_dimensions_are_not_vertical():
    neg_w = ScrapedItem(source="snapchat", source_id="1", source_url="https://x", width=-540, height=960)
    neg_h = ScrapedItem(source="snapchat", source_id="2", source_url="https://x", width=540, height=-960)
    zero = ScrapedItem(source="snapchat", source_id="3", source_url="https://x", width=0, height=960)
    valid = ScrapedItem(source="snapchat", source_id="4", source_url="https://x", width=540, height=960)

    assert neg_w.aspect_ratio == 0.0
    assert neg_w.is_vertical is False
    assert neg_h.aspect_ratio == 0.0
    assert neg_h.is_vertical is False
    assert zero.aspect_ratio == 0.0
    assert zero.is_vertical is False
    assert valid.is_vertical is True


def test_scraper_dedup_drops_blank_source_ids_and_supports_context_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    base = Scraper()
    items = [
        None,  # type: ignore[list-item]
        ScrapedItem(source="youtube", source_id="", source_url="https://x/1"),
        ScrapedItem(source="youtube", source_id="   ", source_url="https://x/2"),
        ScrapedItem(source="", source_id="id1", source_url="https://x/3"),
        ScrapedItem(source="youtube", source_id="ok1", source_url="https://x/4"),
        ScrapedItem(source="youtube", source_id="ok1", source_url="https://x/5"),
    ]
    deduped = base._dedup(items)
    assert [it.source_id for it in deduped] == ["ok1"]

    with YtDlpScraper() as ydl_scraper:
        assert isinstance(ydl_scraper, Scraper)


# ===========================================================================
# SnapchatScraper reliability
# ===========================================================================
def _sample_snap_entry() -> dict:
    return {
        "story": {
            "storyId": {"value": "STORY_REL_1"},
            "snapList": [{
                "snapId": {"value": "STORY_REL_1"},
                "snapMediaType": 1,
                "snapUrls": {"mediaUrl": "https://bolt-gcdn.sc-cdn.net/bo/good.mp4"},
                "timestampInSec": {"value": 1785348032},
                "isSponsored": False,
            }],
        },
        "metadata": {
            "videoMetadata": {
                "name": "Real Title",
                "description": "Real description",
                "contentUrl": "https://bolt-gcdn.sc-cdn.net/bo/good.mp4",
                "durationMs": {"value": "12000"},
                "width": 540,
                "height": 960,
                "creator": {
                    "personCreator": {
                        "username": "creator1",
                        "name": "Creator One",
                        "followerCount": {"value": "5000"},
                    }
                },
            },
            "engagementStats": {
                "viewCount": {"value": "50000"},
                "boostCount": 1200,
                "recommendCount": 80,
                "commentCount": 45,
                "shareCount": 30,
            },
        },
    }


def test_num_and_flt_handle_nan_inf_and_scalar_boxes():
    assert _num(float("nan")) == 0
    assert _num("nan") == 0
    assert _num(float("inf")) == 0
    assert _num("inf") == 0
    assert _num("-inf") == 0
    assert _num({"value": "24177"}) == 24177
    assert _num({"value": 99}) == 99

    assert _flt(True) == 0.0
    assert _flt(float("nan")) == 0.0
    assert _flt("nan") == 0.0
    assert _flt("inf") == 0.0
    assert _flt({"value": "0.82"}) == pytest.approx(0.82)


def test_parse_next_data_accepts_reordered_attributes_and_rejects_non_dict_json():
    payload = {"props": {"pageProps": {"ok": True}}}
    reordered = (
        "<html><body>"
        f"<script type='application/json' nonce='xyz' id='__NEXT_DATA__' crossorigin='anonymous'>"
        f"{json.dumps(payload)}</script></body></html>"
    )
    assert SnapchatScraper._parse_next_data(reordered) == payload
    assert SnapchatScraper._parse_next_data(
        '<script id="__NEXT_DATA__" type="application/json">[1, 2, 3]</script>'
    ) is None
    assert SnapchatScraper._parse_next_data(
        '<script id="__NEXT_DATA__" type="application/json">null</script>'
    ) is None


def test_parse_story_survives_malformed_timestamps_on_windows():
    scraper = SnapchatScraper()

    # Negative epoch (-1 raises OSError [Errno 22] on Windows MSVCRT)
    neg_entry = _sample_snap_entry()
    neg_entry["story"]["snapList"][0]["timestampInSec"] = {"value": -1}
    neg_item = scraper._parse_story(neg_entry)
    assert neg_item is not None
    assert neg_item.published_at is None

    # Huge overflow epoch
    huge_entry = _sample_snap_entry()
    huge_entry["story"]["snapList"][0]["timestampInSec"] = {"value": 99_999_999_999_999_999}
    huge_item = scraper._parse_story(huge_entry)
    assert huge_item is not None
    assert huge_item.published_at is None

    # Milliseconds accidentally served in timestampInSec
    ms_entry = _sample_snap_entry()
    ms_entry["story"]["snapList"][0]["timestampInSec"] = {"value": 1_785_348_032_000}
    ms_item = scraper._parse_story(ms_entry)
    assert ms_item is not None
    assert ms_item.published_at is not None
    assert ms_item.published_at.tzinfo == timezone.utc
    assert int(ms_item.published_at.timestamp()) == 1_785_348_032


def test_parse_story_normalizes_at_author_validates_urls_and_tolerates_non_list_fields():
    scraper = SnapchatScraper()
    entry = _sample_snap_entry()
    entry["metadata"]["videoMetadata"]["creator"] = {}
    entry["metadata"]["videoMetadata"]["contentUrl"] = "blob:https://www.snapchat.com/123"
    entry["metadata"]["videoMetadata"]["keywords"] = "dance, viral, funny"
    entry["metadata"]["contextCards"] = [{"title": "Card Name", "subtitle": "@card_creator"}]
    entry["metadata"]["hashtags"] = 12345  # non-iterable garbage
    entry["metadata"]["cuSignals"] = "not-a-list"
    entry["metadata"]["engagementStats"] = {
        "viewCount": -5,
        "boostCount": -10,
        "recommendCount": -2,
        "commentCount": -1,
        "shareCount": -3,
    }

    item = scraper._parse_story(entry)
    assert item is not None
    assert item.author == "card_creator"
    assert "@@" not in item.source_url
    assert item.media_url == "https://bolt-gcdn.sc-cdn.net/bo/good.mp4"
    assert item.keywords == ["dance", "viral", "funny"]
    assert item.views == 0
    assert item.likes == 0
    assert item.comments == 0
    assert item.shares == 0


def test_normalize_handle_edge_cases_and_url_variants():
    scraper = SnapchatScraper()
    assert scraper._normalize_handle("snapchat.com/add/snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("www.snapchat.com/@snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("https://m.snapchat.com/@snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("https://story.snapchat.com/@snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("https://www.snapchat.com/add/@snoopdogg") == "snoopdogg"
    assert scraper._normalize_handle("/@snoopdogg/") == "snoopdogg"
    assert scraper._normalize_handle("@snoopdogg/") == "snoopdogg"

    assert scraper._normalize_handle("https://www.snapchat.com/add/bad%20handle") is None
    assert scraper._normalize_handle("https://www.snapchat.com/@!") is None
    assert scraper._normalize_handle(None) is None  # type: ignore[arg-type]
    assert scraper._normalize_handle(123) is None  # type: ignore[arg-type]


def test_fetch_next_data_retries_transient_http_errors(monkeypatch):
    monkeypatch.setattr("app.scrapers.snapchat_scraper.time.sleep", lambda *_a: None)
    scraper = SnapchatScraper(timeout=2.0)
    calls = {"n": 0}
    payload = {"props": {"pageProps": {"recovered": True}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'

    class FakeResponse:
        def __init__(self, status_code: int, text: str = ""):
            self.status_code = status_code
            self.text = text
            self.headers = {}

    class FakeClient:
        def get(self, url: str):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("transient reset")
            if calls["n"] == 2:
                return FakeResponse(503, "unavailable")
            return FakeResponse(200, html)

        def close(self):
            pass

    scraper._client = FakeClient()
    result = scraper._fetch_next_data("https://www.snapchat.com/spotlight", allow_browser=False)
    assert result == payload
    assert calls["n"] == 3


def test_spotlight_profile_and_handles_respect_bounds_and_dedup_handles(monkeypatch):
    scraper = SnapchatScraper()
    http_calls: list[str] = []
    monkeypatch.setattr(scraper, "_fetch_next_data", lambda url, **_kw: http_calls.append(url) or None)

    assert scraper.scrape_spotlight(0) == []
    assert scraper.scrape_spotlight(-3) == []
    assert scraper.scrape_profile("snoopdogg", 0) == []
    assert scraper.scrape_profile("snoopdogg", -2) == []
    assert http_calls == []

    profile_calls: list[tuple[str, int]] = []

    def fake_scrape_profile(username: str, limit: int):
        profile_calls.append((username, limit))
        if username == "empty_user":
            return []
        return [
            ScrapedItem(
                source="snapchat",
                source_id=f"{username}_{i}",
                source_url=f"https://www.snapchat.com/@{username}/spotlight/{i}",
                media_url=f"https://bolt-gcdn.sc-cdn.net/bo/{i}.mp4",
            )
            for i in range(limit)
        ]

    monkeypatch.setattr(scraper, "scrape_profile", fake_scrape_profile)
    items = scraper.scrape_handles(
        ["@empty_user", "empty_user", "https://www.snapchat.com/@active_user", "@active_user"],
        max_per_run=4,
        download=False,
    )
    assert [u for u, _ in profile_calls] == ["empty_user", "active_user"]
    assert len(items) == 4


def test_snapchat_download_pools_client_honours_proxy_and_dedups_concurrent_items(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "proxy_url", "http://127.0.0.1:8888")

    clients_created: list[dict] = []
    stream_calls: list[str] = []
    payload = b"V" * 60_000

    class FakeStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def iter_bytes(self, _chunk_size: int = 65536):
            yield payload

    class FakeClient:
        def __init__(self, **kwargs):
            clients_created.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def stream(self, method: str, url: str):
            assert method == "GET"
            stream_calls.append(url)
            return FakeStream()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    dup1 = ScrapedItem(
        source="snapchat",
        source_id="../weird:id",
        source_url="https://www.snapchat.com/spotlight/1",
        media_url="https://bolt-gcdn.sc-cdn.net/bo/1.mp4",
    )
    dup2 = ScrapedItem(
        source="snapchat",
        source_id="../weird:id",
        source_url="https://www.snapchat.com/spotlight/1",
        media_url="https://bolt-gcdn.sc-cdn.net/bo/1.mp4",
    )
    bad_scheme = ScrapedItem(
        source="snapchat",
        source_id="bad_scheme",
        source_url="https://www.snapchat.com/spotlight/2",
        media_url="javascript:alert(1)",
    )

    scraper = SnapchatScraper()
    count = scraper.download([dup1, dup2, bad_scheme], max_workers=0)
    assert count == 2
    assert len(clients_created) == 1
    assert clients_created[0].get("proxy") == "http://127.0.0.1:8888"
    assert len(stream_calls) == 1
    assert dup1.local_path is not None and dup1.local_path == dup2.local_path
    assert Path(dup1.local_path).resolve().parent == tmp_path.resolve()
    assert Path(dup1.local_path).stat().st_size == len(payload)
    assert bad_scheme.local_path is None


# ===========================================================================
# YtDlpScraper reliability
# ===========================================================================
def test_ytdlp_cookiesfrombrowser_passes_tuple_and_preserves_ingest_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "cookies_from_browser", "edge,Default")
    monkeypatch.setattr(settings, "cookies_file", "")

    # Preserve ingest.py contract on _parse_browser_cookies
    parsed = YtDlpScraper._parse_browser_cookies("edge,Default")
    assert parsed == [("edge", "Default")]
    assert YtDlpScraper._parse_browser_cookies("edge") == ["edge"]

    scraper = YtDlpScraper()
    opts = scraper._ydl_opts(download=False)
    assert opts["cookiesfrombrowser"] == ("edge", "Default")

    cookie_txt = tmp_path / "cookies.txt"
    cookie_txt.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n", encoding="utf-8")
    monkeypatch.setattr(settings, "cookies_file", str(cookie_txt))
    scraper_with_file = YtDlpScraper()
    opts_file = scraper_with_file._ydl_opts(download=False)
    assert opts_file["cookiefile"] == str(cookie_txt)
    assert "cookiesfrombrowser" not in opts_file


def test_ytdlp_match_filter_handles_missing_height_string_numbers_and_live_streams(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    scraper = YtDlpScraper()
    mf = scraper._ydl_opts(download=False)["match_filter"]

    # Playlist/search entries often lack height prior to format selection
    assert mf({"duration": 25, "height": None}) is None
    # Numeric strings and floats from third-party extractors must not raise TypeError/ValueError
    assert mf({"duration": "25.4", "height": "720"}) is None
    assert mf({"duration": "nan", "height": "bad"}) is not None
    # Rejects low resolution, non-Shorts duration, and live streams
    assert mf({"duration": 25, "height": 240}) is not None
    assert mf({"duration": 120, "height": 1080}) is not None
    assert mf({"duration": 25, "height": 1080, "is_live": True}) is not None


def test_ytdlp_to_item_extracts_dimensions_published_at_clean_author_and_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    scraper = YtDlpScraper()
    info = {
        "id": "abc123",
        "webpage_url": "https://www.youtube.com/shorts/abc123",
        "uploader": "MrBeast",
        "uploader_id": "@MrBeast",
        "title": "Insane Challenge",
        "description": "Watch till the end",
        "width": "1080",
        "height": "1920",
        "duration": 34.6,
        "view_count": "150000",
        "like_count": "nan",
        "comment_count": -4,
        "repost_count": 12,
        "upload_date": "20260410",
        "tags": ["challenge", "shorts", "challenge"],
        "language": "en",
        "channel_follower_count": 300_000_000,
    }
    item = scraper._to_item(info, "youtube")
    assert item.width == 1080
    assert item.height == 1920
    assert item.is_vertical is True
    assert item.author == "MrBeast"
    assert item.author_name == "MrBeast"
    assert item.views == 150_000
    assert item.likes == 0
    assert item.comments == 0
    assert item.shares == 12
    assert item.duration_sec == 35
    assert item.published_at is not None
    assert (item.published_at.year, item.published_at.month, item.published_at.day) == (2026, 4, 10)
    assert item.keywords == ["challenge", "shorts"]
    assert item.language == "en"
    assert item.follower_count == 300_000_000


def test_ytdlp_download_returns_none_when_file_missing_and_sanitizes_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "nested_content"))
    scraper = YtDlpScraper()

    captured_opts: list[dict] = []

    class FakeYDL:
        def __init__(self, opts: dict):
            captured_opts.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url: str, download: bool = True):
            # Simulate match_filter skip where filepath metadata exists but file was never written
            return {"filepath": str(tmp_path / "nested_content" / "ghost.mp4")}

    class FakeModule:
        YoutubeDL = FakeYDL

    scraper._yt = FakeModule()
    ghost_item = ScrapedItem(
        source="youtube",
        source_id="../evil:id",
        source_url="https://www.youtube.com/shorts/evil",
    )
    assert scraper._download(ghost_item) is None
    outtmpl = captured_opts[-1]["outtmpl"]
    assert ".." not in Path(outtmpl).name
    assert ":" not in Path(outtmpl).name
    assert captured_opts[-1].get("noplaylist") is True


def test_ytdlp_platform_handle_url_and_bounded_enumeration(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    scraper = YtDlpScraper()

    # Preserve scripts/final_audit.py checks
    assert scraper._platform_for_handle("@charlidamelio") == "instagram"
    assert scraper._platform_for_handle("snapchat.com/add/test") == "snapchat"
    assert scraper._platform_for_handle("https://www.youtube.com/@test") == "youtube"
    assert scraper._platform_for_handle("https://www.reddit.com/r/videos") == "reddit"
    # Fix missing edge cases
    assert scraper._platform_for_handle("r/videos") == "reddit"
    assert scraper._platform_for_handle("  @charlidamelio  ") == "instagram"
    assert scraper._platform_for_handle("https://www.tiktok.com/@khaby.lame") == "tiktok"

    assert scraper._handle_url("r/videos") == "https://www.reddit.com/r/videos"
    assert scraper._handle_url(" /r/videos/ ") == "https://www.reddit.com/r/videos"
    assert scraper._handle_url("snapchat.com/add/test") == "https://snapchat.com/add/test"
    assert scraper._handle_url("mrbeast").startswith("ytsearch")

    # Bounded enumeration over a huge lazy generator + non-positive max_per_run guard
    consumed = {"n": 0}

    def infinite_entries():
        for idx in itertools.count(1):
            consumed["n"] += 1
            if idx > 500:
                raise AssertionError("unbounded playlist iteration")
            yield {"id": f"vid_{idx}", "webpage_url": f"https://www.youtube.com/shorts/vid_{idx}"}

    class FakeYDL:
        def __init__(self, _opts: dict):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url: str, download: bool = False):
            if "single" in url:
                return {"id": "solo_1", "webpage_url": url}
            return {"entries": infinite_entries()}

    class FakeModule:
        YoutubeDL = FakeYDL

    scraper._yt = FakeModule()
    enumerated = scraper._enumerate("https://www.youtube.com/@test/shorts", 3, "youtube")
    assert len(enumerated) == 3
    assert consumed["n"] <= 10

    solo = scraper._enumerate("https://www.youtube.com/shorts/single", 3, "youtube")
    assert [it.source_id for it in solo] == ["solo_1"]

    assert scraper.scrape_handles(["@charlidamelio"], 0, download=False) == []
    assert scraper.scrape_handles(["@charlidamelio"], -2, download=False) == []
    assert scraper.scrape_hashtags(["###", "   "], 5, download=False) == []


# ===========================================================================
# MockScraper reliability
# ===========================================================================
def test_mock_scraper_handles_url_handles_respects_download_false_and_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    master = tmp_path / "_mock_master.mp4"
    master.write_bytes(b"M" * 60_000)

    scraper = MockScraper()
    assert scraper.scrape_handles(["@khaby.lame"], 0) == []
    assert scraper.scrape_handles(["@khaby.lame"], -2) == []
    assert scraper.scrape_hashtags(["viral"], 0) == []
    assert scraper.scrape_hashtags(["viral"], -2) == []

    no_dl = scraper.scrape_handles(["@khaby.lame"], 2, download=False)
    assert len(no_dl) == 2
    assert all(it.local_path is None for it in no_dl)

    url_items = scraper.scrape_handles(
        ["https://www.snapchat.com/@snoopdogg", "r/videos"],
        max_per_run=2,
        download=True,
    )
    assert len(url_items) == 2
    for it in url_items:
        assert it.local_path is not None
        p = Path(it.local_path)
        assert p.exists()
        assert p.resolve().parent == tmp_path.resolve()


def test_mock_ensure_master_dummy_regenerates_zero_byte_file_and_passes_winproc_flags(tmp_path, monkeypatch):
    content_dir = tmp_path / "fresh_dir"
    master = content_dir / "_mock_master.mp4"
    content_dir.mkdir(parents=True)
    master.write_bytes(b"")  # stale 0-byte file from an earlier failed run

    seen: list[dict] = []

    class FakeProc:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        Path(cmd[-1]).write_bytes(b"F" * 1024)
        return FakeProc()

    monkeypatch.setattr("app.winproc.background_subprocess_kwargs", lambda low_priority=True: {"creationflags": 0x08004000})
    monkeypatch.setattr(subprocess, "run", fake_run)

    out = _ensure_master_dummy(str(content_dir), str(master))
    assert out == str(master)
    assert len(seen) == 1
    assert seen[0].get("stdin") == subprocess.DEVNULL
    assert seen[0].get("creationflags") == 0x08004000
    assert seen[0].get("timeout") is not None
    assert master.stat().st_size == 1024
