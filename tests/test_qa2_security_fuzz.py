"""Cross-cutting security & fuzz suite (QA-2).

Covers:
1. Path traversal across uploads, channel slugs/imports, scraper stems, and Content-Disposition headers.
2. Symlink & NTFS junction escape across cleanup_job, storage_report, and inbox scanning.
3. HMAC / cookie tampering, unconfigured/locked states, logout, and proxy HTTPS cookie flags on review_app.
4. Atom XXE, Billion Laughs entity expansion, oversized feed bombs, and malformed XML/videoId fuzzing in watch.py.
5. ASS subtitle override tag, vector drawing, CSV style, control-char, clock, and binary TTF/TTC fuzzing in captions.py.
6. Secret scrubbing across logging_config, job_history, /api/logs, notify, broll, ingest, doctor, youtube_oauth,
   markdown reports, /api/backup, and channels export.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import struct
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import logging_config, scheduler
from app.clipper import broll, captions, ingest, job as clip_job
from app.clipper.transcribe import Word
from app.config import settings
from app.db import init_db, session_scope
from app.growth import approval, channels, controls, notify, report, safety, watch
from app.models import (
    Channel,
    ClipJob,
    Creator,
    Feed,
    GrowthEvent,
    GrowthState,
    MetadataDraft,
    QuotaUsage,
    Video,
    VideoSnapshot,
    VideoStatus,
)
from app.publishers.youtube_publisher import UPLOAD_SCOPE
from app.review import _COOKIE, review_app
from app.scrapers.snapchat_scraper import _safe_snap_filename
from app.scrapers.yt_dlp_scraper import _safe_stem


CANARIES = {
    "review_token": "CANARY_REV_TOKEN_9f8e7d6c5b4a",
    "telegram_bot_token": "987654321:CANARY_TG_BOT_SECRET_AbCdEf1234",
    "pixabay_api_key": "CANARY_PIXABAY_KEY_1122334455",
    "pexels_api_key": "CANARY_PEXELS_KEY_6677889900",
    "tenor_api_key": "CANARY_TENOR_KEY_4455667788",
    "giphy_api_key": "CANARY_GIPHY_KEY_9988776655",
    "freesound_api_key": "CANARY_FREESOUND_KEY_1212121212",
    "deepgram_api_key": "CANARY_DEEPGRAM_KEY_aabbccddee",
    "sarvam_api_key": "CANARY_SARVAM_KEY_ff00112233",
}


@pytest.fixture(autouse=True)
def _isolated_security_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    init_db()
    with session_scope() as db:
        for table in (
            VideoSnapshot,
            MetadataDraft,
            Feed,
            Channel,
            ClipJob,
            GrowthEvent,
            GrowthState,
            QuotaUsage,
            Creator,
            Video,
        ):
            db.execute(delete(table))

    content_root = tmp_path / "content"
    inbox_root = content_root / "inbox"
    logs_root = tmp_path / "logs"
    content_root.mkdir(parents=True, exist_ok=True)
    inbox_root.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "content_dir", str(content_root))
    monkeypatch.setattr(settings, "growth_inbox_dir", str(inbox_root))
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    monkeypatch.setattr(settings, "youtube_client_secrets", "")
    monkeypatch.setattr(settings, "youtube_token_path", str(tmp_path / "secrets" / "youtube_token.json"))
    monkeypatch.setattr(settings, "review_token", "qa2-valid-review-secret-token")
    monkeypatch.setattr(settings, "proxy_url", "")
    monkeypatch.setattr(logging_config, "LOG_DIR", logs_root)
    monkeypatch.setattr(notify, "TUNNEL_URL_FILE", logs_root / "tunnel_url.txt")
    # Prevent background queue threads from spawning during panel upload/clip route tests.
    monkeypatch.setattr(clip_job, "start_queue_worker", lambda: None)
    controls._status.clear()
    yield


def _authed_panel_client(token: str = "qa2-valid-review-secret-token") -> TestClient:
    client = TestClient(review_app)
    resp = client.get(f"/?key={token}", follow_redirects=False)
    assert resp.status_code == 303
    return client


def _make_symlink_or_skip(link_path: Path, target_path: Path, *, target_is_directory: bool = False) -> None:
    """Create a symlink (or NTFS directory junction on Windows without symlink privilege)."""
    try:
        os.symlink(str(target_path), str(link_path), target_is_directory=target_is_directory)
        return
    except (OSError, NotImplementedError):
        if os.name == "nt" and target_is_directory:
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            if res.returncode == 0 and link_path.exists():
                return
        pytest.skip("OS symlink/junction privilege unavailable in this environment")


# ===========================================================================
# 1. PATH TRAVERSAL & FILENAME SANITIZATION FUZZING
# ===========================================================================


@pytest.mark.parametrize(
    "malicious_filename",
    [
        "../../../etc/passwd.mp4",
        "..\\..\\..\\secrets\\youtube_token.mp4",
        "../../.env.mp4",
        "/etc/shadow.mov",
        "C:\\Windows\\System32\\drivers\\etc\\hosts.mkv",
        "\\\\evil-smb\\share\\payload.webm",
        "C:drive_relative.m4v",
        "....//....//outside.mp4",
        ".hidden_dotfile.mp4",
        "---__...__---.mp4",
        "evil\x00nullbyte.mp4",
        "semi;colon|pipe&amp$var`cmd`.mp4",
        "CON.mp4",
        "NUL.mp4",
        "a" * 400 + ".mp4",
    ],
)
def test_prepare_inbox_upload_confines_every_traversal_payload_inside_inbox(malicious_filename: str):
    inbox = (settings.content_path / "inbox").resolve()
    part_path, final_path = clip_job.prepare_inbox_upload(malicious_filename)

    assert final_path.resolve().parent == inbox
    assert part_path.resolve().parent == inbox
    assert os.path.commonpath([str(inbox), str(final_path.resolve())]) == str(inbox)
    assert os.path.commonpath([str(inbox), str(part_path.resolve())]) == str(inbox)
    assert not final_path.name.startswith(".")
    assert ".." not in final_path.name
    assert "\x00" not in final_path.name
    assert final_path.suffix.lower() in clip_job.ALLOWED_UPLOAD_EXTS
    assert part_path.name == final_path.name + ".part"


@pytest.mark.parametrize(
    "bad_filename",
    [
        "",
        "../../.env",
        "../../secrets/youtube_token.json",
        "payload.exe",
        "shell.py",
        "script.ps1",
        "trick.mp4.exe",
        "trick.mp4.txt",
        "subtitles.ass",
        "archive.zip",
        "no_extension",
    ],
)
def test_prepare_inbox_upload_and_panel_endpoint_reject_disallowed_extensions(bad_filename: str):
    with pytest.raises(ValueError, match="unsupported video type"):
        clip_job.prepare_inbox_upload(bad_filename)

    client = _authed_panel_client()
    resp = client.post(
        "/api/clip/upload",
        params={"filename": bad_filename},
        content=b"fake-video-bytes",
    )
    assert resp.status_code == 400
    inbox = settings.content_path / "inbox"
    assert list(inbox.iterdir()) == []


def test_panel_upload_rejects_empty_and_oversized_streams_and_cleans_up_part_files(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _authed_panel_client()
    inbox = settings.content_path / "inbox"

    empty_resp = client.post("/api/clip/upload", params={"filename": "empty.mp4"}, content=b"")
    assert empty_resp.status_code == 400
    assert list(inbox.iterdir()) == []

    monkeypatch.setattr(clip_job, "MAX_UPLOAD_BYTES", 64)
    big_resp = client.post("/api/clip/upload", params={"filename": "huge.mp4"}, content=b"x" * 200)
    assert big_resp.status_code == 413
    assert list(inbox.iterdir()) == []


@pytest.mark.parametrize(
    "weird_id",
    [
        'clip"evil\r\nX-Injected: pwned',
        "../../secrets/token",
        "semi;filename=evil.exe",
        "unicode_🔥_name",
        "---",
    ],
)
def test_media_download_content_disposition_sanitizes_header_injection(weird_id: str):
    clip_file = settings.content_path / "clips" / "safe.mp4"
    clip_file.parent.mkdir(parents=True, exist_ok=True)
    clip_file.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    with session_scope() as db:
        db.add(
            Video(
                id=weird_id,
                source="youtube",
                source_id=f"src_{hash(weird_id)}",
                source_url="https://example.com/safe.mp4",
                status=VideoStatus.PENDING,
                transformed_path=str(clip_file),
                meta={"clip_rendered": True},
            )
        )

    from app.review import _serve_media

    response = _serve_media(weird_id, download="1")
    cd = response.headers.get("content-disposition", "")
    assert "\r" not in cd and "\n" not in cd
    assert "X-Injected:" not in cd  # the header-splitting payload is neutralized; its words may survive inertly inside the quoted filename
    assert cd.count('"') == 2
    assert re.fullmatch(r'attachment; filename="short-[A-Za-z0-9_-]+\.mp4"', cd)


@pytest.mark.parametrize(
    "bad_slug",
    [
        "",
        "../evil",
        "..\\evil",
        "tech/sub",
        "tech\\sub",
        ".dotstart",
        "-dashstart",
        "_understart",
        "UpperCase",
        "has space",
        "null\x00byte",
        "error",
        "a" * 33,
    ],
)
def test_channel_slug_validation_blocks_path_traversal_and_reserved_names(bad_slug: str):
    client = _authed_panel_client()
    resp = client.post("/api/channels", json={"slug": bad_slug, "name": "Test"})
    assert resp.status_code == 400

    with session_scope() as db:
        channels.sync_channels(db)
        with pytest.raises(channels.ChannelsConfigError):
            channels.clone_channel(db, "default", bad_slug)


def test_channels_import_strips_untrusted_secret_paths_oauth_bindings_and_enabled_flag():
    client = _authed_panel_client()
    malicious_payload = {
        "version": 1,
        "channels": [
            {
                "slug": "imported-tech",
                "name": "Imported\x00Tech\x1b[31m",
                "niche": "tech",
                "enabled": True,
                "token_path": "../../.env",
                "client_secrets": "/etc/passwd",
                "youtube_channel_id": "UC_ATTACKER_HIJACK_1234567890",
                "feeds": ["https://www.youtube.com/@mkbhd"],
            }
        ],
    }
    resp = client.post(
        "/api/channels/import",
        json={"payload": malicious_payload, "dry_run": False},
    )
    assert resp.status_code == 200, resp.text

    written = json.loads(Path(settings.channels_file).read_text(encoding="utf-8"))
    entry = next(c for c in written["channels"] if c["slug"] == "imported-tech")
    assert entry["client_secrets"] == "secrets/imported-tech_client_secret.json"
    assert entry["token_path"] == "secrets/imported-tech_token.json"
    assert entry["enabled"] is False
    assert "youtube_channel_id" not in entry
    assert "\x00" not in entry["name"] and "\x1b" not in entry["name"]


@pytest.mark.parametrize(
    "raw_id",
    [
        "../../../etc/passwd",
        "..\\..\\secrets\\token",
        "..",
        "...",
        ".leading_dot",
        "nul\x00byte",
        "colon:slash/back\\quote\"pipe|",
        "x" * 250,
        "",
    ],
)
def test_scraper_and_clipper_id_sanitizers_never_emit_path_separators_or_dotdot(raw_id: str):
    snap = _safe_snap_filename(raw_id)
    yt = _safe_stem(raw_id)
    stock = broll._safe_video_id(raw_id)
    ing = ingest._safe_source_id(raw_id, "fallback123")

    for candidate in (snap, yt, stock, ing):
        assert candidate
        assert "/" not in candidate and "\\" not in candidate
        assert ".." not in candidate
        assert not candidate.startswith(".")
        assert "\x00" not in candidate


# ===========================================================================
# 2. SYMLINK & JUNCTION ESCAPE PROTECTION
# ===========================================================================


def test_cleanup_job_never_follows_symlinked_file_or_symlinked_dir_outside_content(tmp_path: Path):
    outside_dir = tmp_path / "outside_precious"
    outside_dir.mkdir()
    precious_file = outside_dir / "keep_me.mp4"
    precious_file.write_bytes(b"precious-external-video-bytes" * 20)
    old_mtime = time.time() - 48 * 3600
    os.utime(precious_file, (old_mtime, old_mtime))

    # Place a symlinked directory inside content/clips/ pointing to outside_dir.
    clips_dir = settings.content_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    linked_subdir = clips_dir / "linked_escape"
    _make_symlink_or_skip(linked_subdir, outside_dir, target_is_directory=True)

    # Also reference the path via an expired REJECTED Video row.
    escaped_target = linked_subdir / "keep_me.mp4"
    expired_time = datetime.now(timezone.utc) - timedelta(days=30)
    with session_scope() as db:
        db.add(
            Video(
                id="vid_symlink_escape",
                source="snapchat",
                source_id="sym_1",
                source_url="https://example.com/symlink.mp4",
                status=VideoStatus.REJECTED,
                local_path=str(escaped_target),
                transformed_path=str(escaped_target),
                scraped_at=expired_time,
                curated_at=expired_time,
            )
        )

    assert scheduler._has_link_or_junction(str(escaped_target), stop_at=str(settings.content_path)) is True
    assert str(escaped_target) not in scheduler._content_files()

    res = scheduler.cleanup_job(dry_run=False)
    assert precious_file.is_file()
    assert precious_file.read_bytes().startswith(b"precious-external-video-bytes")
    assert res["mb_reclaimed"] == 0


# ===========================================================================
# 3. HMAC / COOKIE TAMPERING & AUTHENTICATION BOUNDARY FUZZING
# ===========================================================================


def test_review_panel_shut_with_503_when_review_token_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "review_token", "")
    client = TestClient(review_app)

    root = client.get("/")
    assert root.status_code == 503
    assert "no-store" in root.headers.get("cache-control", "")

    unlock_attempt = client.get("/?key=anything", follow_redirects=False)
    assert unlock_attempt.status_code == 503
    assert _COOKIE not in unlock_attempt.cookies

    for path in ("/api/queue", "/api/logs", "/api/backup", "/api/doctor", "/api/system/doctor", "/manifest.webmanifest"):
        resp = client.get(path)
        assert resp.status_code == 503


@pytest.mark.parametrize(
    "tampered_cookie",
    [
        "",
        "qa2-valid-review-secret-toke",  # truncated by 1 byte
        "qa2-valid-review-secret-tokenX",  # appended byte
        "QA2-VALID-REVIEW-SECRET-TOKEN",  # case-flipped
        "qa2-valid-review-secret-tokeN",  # last-byte bit flip
        "Xa2-valid-review-secret-token",  # first-byte bit flip
        "null",
        "true",
        "a" * 4096,
    ],
)
def test_review_panel_rejects_tampered_cookies_and_query_key_on_api_routes(tampered_cookie: str):
    client = TestClient(review_app)
    if tampered_cookie:
        client.cookies.set(_COOKIE, tampered_cookie)

    assert client.get("/").status_code == 401
    # Passing ?key=<valid> directly to /api/* without a valid cookie MUST NOT authenticate!
    api_resp = client.get("/api/queue?key=qa2-valid-review-secret-token")
    assert api_resp.status_code == 401

    wrong_unlock = client.get("/?key=wrong-token-value", follow_redirects=False)
    assert wrong_unlock.status_code == 401
    assert "set-cookie" not in {k.lower() for k in wrong_unlock.headers.keys()}


def test_review_cookie_security_flags_http_vs_https_proxy_and_logout_invalidation():
    http_client = TestClient(review_app)
    r_http = http_client.get("/?key=qa2-valid-review-secret-token", follow_redirects=False)
    assert r_http.status_code == 303
    set_cookie_http = r_http.headers["set-cookie"].lower()
    assert "httponly" in set_cookie_http
    assert "samesite=lax" in set_cookie_http
    assert "secure" not in set_cookie_http

    https_client = TestClient(review_app, base_url="https://testserver")
    r_https = https_client.get(
        "/?key=qa2-valid-review-secret-token",
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert r_https.status_code == 303
    set_cookie_https = r_https.headers["set-cookie"].lower()
    assert "httponly" in set_cookie_https
    assert "samesite=lax" in set_cookie_https
    assert "secure" in set_cookie_https

    # Authenticated before logout, 401 after logout.
    assert https_client.get("/api/queue").status_code == 200
    logout = https_client.post("/api/logout")
    assert logout.status_code == 200
    assert https_client.get("/api/queue").status_code == 401


# ===========================================================================
# 4. ATOM XXE, BILLION LAUGHS & OVERSIZED FEED BOMBS (app/growth/watch.py)
# ===========================================================================


@pytest.mark.parametrize(
    "malicious_xml",
    [
        # Classic external file XXE
        '<?xml version="1.0"?>'
        '<!DOCTYPE feed [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><yt:videoId xmlns:yt="http://www.youtube.com/xml/schemas/2015">&xxe;</yt:videoId>'
        "<published>2026-11-14T00:00:00+00:00</published></entry></feed>",
        # Case-varied parameter entity SSRF XXE
        '<?xml version="1.0"?>'
        '<!DOCTYPE feed [<!eNtItY % remote SYSTEM "http://169.254.169.254/latest/meta-data/">%remote;]>'
        '<feed xmlns="http://www.w3.org/2005/Atom"/>',
        # Billion Laughs exponential entity bomb
        '<?xml version="1.0"?>'
        "<!DOCTYPE lolz ["
        '<!ENTITY lol "lol">'
        '<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">'
        '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
        "]>"
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>&lol3;</title></feed>',
        # Truncated / binary / malformed payloads
        "\x00\xff\xfe<feed><entry>",
        "",
        "   ",
    ],
)
def test_watch_entries_rejects_xxe_billion_laughs_and_malformed_xml(malicious_xml: str):
    started = time.perf_counter()
    parsed = watch._entries(malicious_xml)
    elapsed = time.perf_counter() - started
    assert parsed == []
    assert elapsed < 0.5


def test_watch_entries_and_read_bounded_reject_oversized_feed_bombs():
    # 1. Direct _entries cap (> 2 MiB rejected before XML parser runs)
    oversized_xml = '<feed xmlns="http://www.w3.org/2005/Atom">' + (" " * watch._MAX_FEED_BYTES) + "</feed>"
    assert watch._entries(oversized_xml) == []

    # 2. Content-Length header > _MAX_FEED_BYTES aborts before streaming body
    class _HeaderBombResponse:
        headers = {"Content-Length": str(watch._MAX_FEED_BYTES + 100)}
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:  # pragma: no cover
            raise AssertionError("iter_bytes must not be called when Content-Length exceeds cap")

    assert watch._read_bounded(_HeaderBombResponse()) is None

    # 3. Lying/absent Content-Length chunked stream aborts mid-stream once bytes > _MAX_FEED_BYTES
    chunks_read = 0

    class _StreamingBombResponse:
        headers: dict[str, str] = {}
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> Iterator[bytes]:
            nonlocal chunks_read
            for _ in range(50):
                chunks_read += 1
                yield b"A" * (512 * 1024)  # 512 KiB per chunk; 5th chunk crosses 2 MiB

    assert watch._read_bounded(_StreamingBombResponse()) is None
    assert chunks_read == 5  # Stopped immediately on crossing 2 MiB instead of reading all 25 MiB!


def test_watch_entries_rejects_injected_video_ids_and_deduplicates_timestamps():
    atom = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <entry><yt:videoId>../../etc/passwd</yt:videoId><published>2026-11-14T01:00:00Z</published></entry>
      <entry><yt:videoId>bad;rm -rf /</yt:videoId><published>2026-11-14T01:00:00Z</published></entry>
      <entry><yt:videoId>{"a" * 65}</yt:videoId><published>2026-11-14T01:00:00Z</published></entry>
      <entry><yt:videoId>dQw4w9WgXcQ</yt:videoId><published>not-a-timestamp</published></entry>
      <entry><yt:videoId>dQw4w9WgXcQ</yt:videoId><published>2026-11-14T01:00:00+00:00</published></entry>
      <entry><yt:videoId>dQw4w9WgXcQ</yt:videoId><published>2026-11-14T02:00:00+00:00</published></entry>
    </feed>
    """.replace('{"a" * 65}', "a" * 65)
    entries = watch._entries(atom)
    assert len(entries) == 1
    assert entries[0][0] == "dQw4w9WgXcQ"
    assert entries[0][1] == datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)


def test_watch_safe_url_for_log_strips_query_secrets_and_userinfo():
    dirty = "https://user:CANARY_PASS@example.com/feeds/videos.xml?api_key=SECRET123&token=ABC#frag"
    cleaned = watch._safe_url_for_log(dirty)
    assert cleaned == "https://example.com/feeds/videos.xml"
    assert "CANARY_PASS" not in cleaned
    assert "SECRET123" not in cleaned


# ===========================================================================
# 5. ASS SUBTITLE OVERRIDE TAG, VECTOR DRAWING & TTF FUZZING (captions.py)
# ===========================================================================


def _strip_known_ass_tags(dialogue_text: str) -> str:
    """Remove legitimate leading/inline override blocks generated by captions.py itself."""
    return re.sub(r"\{[^}]*\}", "", dialogue_text)


def test_ass_subtitle_neutralizes_override_tags_vector_drawing_and_multiline_injection():
    evil_words = [
        Word(r"{\p1}m 0 0 l 1080 0 l 1080 1920{\p0}", 0.0, 0.4, 0.99),
        Word("line1\nDialogue: 0,0:00:00.00,9:59:59.99,Caption,,0,0,0,,PWNED", 0.4, 0.8, 0.99),
        Word("ctrl\x00\x07\x1b[31m\u2028\u2029\\N\\h{tag}", 0.8, 1.2, 0.99),
    ]
    evil_hook = "{\\pos(0,0)\\fs999}HOOK\r\nStyle: Injected,Arial,999"
    evil_creator = "Creator}\nDialogue: 0,0:00:00.00,0:00:05.00,Credit,,0,0,0,,{\\1c&H0000FF&}FAKE"
    evil_brand = "@brand{\\bord99}\\N\r\n[Events]"
    evil_font = "Arial,999,&H0000FF00\r\nDialogue: 0,0:00:00.00,0:00:10.00,Caption,,0,0,0,,FONT_INJECT"
    evil_beats = [
        (0.5, "$500{\\p1}\nINJECT", "stat", "revenue{\\fs500}\r\nFAKE", 1.2),
        (2.0, "BIG{\\an1}CARD\\NLINE", "card", "", 1.0),
    ]

    ass_doc = captions.build_ass(
        words=evil_words,
        hook=evil_hook,
        duration=5.0,
        font=evil_font,
        font_path=None,
        beats=evil_beats,
        creator=evil_creator,
        brand=evil_brand,
    )

    assert "\r" not in ass_doc
    assert "\x00" not in ass_doc and "\x1b" not in ass_doc
    assert "\u2028" not in ass_doc and "\u2029" not in ass_doc
    assert "FONT_INJECT" not in ass_doc

    lines = ass_doc.splitlines()
    style_lines = [ln for ln in lines if ln.startswith("Style: ")]
    assert len(style_lines) == 5
    for st in style_lines:
        # V4+ Styles Format has exactly 23 comma-separated columns (22 commas)
        assert st.count(",") == 22, f"corrupted Style CSV column count: {st}"

    in_events = False
    for ln in lines:
        if ln == "[Events]":
            in_events = True
            continue
        if not in_events or not ln or ln.startswith("Format: "):
            continue
        assert ln.startswith("Dialogue: "), f"unexpected non-Dialogue line injected into [Events]: {ln!r}"
        parts = ln.split(",", 9)
        assert len(parts) == 10
        assert parts[3] in {"Caption", "Hook", "Brand", "Credit", "Beat"}
        visible = _strip_known_ass_tags(parts[9]).replace("\\N", " ")
        assert "{" not in visible and "}" not in visible
        assert "\\" not in visible


@pytest.mark.parametrize(
    "weird_sec,expected_clock",
    [
        (float("nan"), "0:00:00.00"),
        (float("-inf"), "0:00:00.00"),
        (-99.5, "0:00:00.00"),
        (0.0, "0:00:00.00"),
        (float("inf"), "0:00:00.00"),
        (1e9, "9:59:59.99"),
    ],
)
def test_ass_clock_handles_non_finite_negative_and_overflow_timestamps(weird_sec: float, expected_clock: str):
    assert captions._clock(weird_sec) == expected_clock


def test_font_family_fuzzing_handles_truncated_corrupt_and_injected_ttf_files(tmp_path: Path):
    # 1. Truncated / empty binary files fall back cleanly to sanitized filename
    trunc = tmp_path / "Evil,Font;Name.ttf"
    trunc.write_bytes(b"ttcf\x00")
    fam_trunc = captions.font_family(str(trunc))
    assert "," not in fam_trunc and ";" not in fam_trunc and "\n" not in fam_trunc

    # 2. Crafted TTF with an ASS injection payload inside UTF-16BE name_id=1 record
    injected_name = "MyFont,999\r\nDialogue: 0,0:00:00.00,0:00:09.00,Hook,,0,0,0,,{\\p1}".encode("utf-16-be")
    # Build minimal sfnt header: version(4) + numTables=1(2) + pad(6) = 12 bytes
    # Table record (16 bytes): tag=b'name' + checksum(4) + offset=28(4) + length(4)
    # Name table at offset 28: format=0(2), count=1(2), stringOffset=18(2) + NameRecord(12): plat=3,enc=1,lang=0,id=1,len,off=0
    name_table = struct.pack(">HHH6H", 0, 1, 18, 3, 1, 0, 1, len(injected_name), 0) + injected_name
    sfnt = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0) + struct.pack(">4sIII", b"name", 0, 28, len(name_table)) + name_table
    crafted = tmp_path / "crafted.ttf"
    crafted.write_bytes(sfnt)

    fam = captions.font_family(str(crafted))
    assert fam.startswith("MyFont999")
    assert all(ch not in fam for ch in (",", ":", ";", "{", "}", "\\", "\r", "\n"))


# ===========================================================================
# 6. SECRET SCRUBBING ACROSS LOGS, API, PANEL, DOCTOR, OAUTH & BACKUP
# ===========================================================================


def test_logging_scrub_secrets_redacts_configured_secrets_overlap_and_inline_patterns(
    monkeypatch: pytest.MonkeyPatch,
):
    for attr, val in CANARIES.items():
        monkeypatch.setattr(settings, attr, val)

    # Overlapping prefix secret test: short secret is prefix of longer secret
    monkeypatch.setattr(settings, "pixabay_api_key", "SHARED_PREFIX_123")
    monkeypatch.setattr(settings, "pexels_api_key", "SHARED_PREFIX_123_LONGER_TAIL_999")
    overlapped = logging_config.scrub_secrets("k1=SHARED_PREFIX_123 k2=SHARED_PREFIX_123_LONGER_TAIL_999")
    assert "LONGER_TAIL_999" not in overlapped
    assert "SHARED_PREFIX_123" not in overlapped

    monkeypatch.setattr(settings, "pixabay_api_key", CANARIES["pixabay_api_key"])
    monkeypatch.setattr(settings, "pexels_api_key", CANARIES["pexels_api_key"])

    raw_line = (
        f"unlock=http://127.0.0.1:8600/?key={CANARIES['review_token']}&api_key=INLINE_API_999 "
        f"tg=https://api.telegram.org/bot{CANARIES['telegram_bot_token']}/sendMessage "
        f"Authorization: Bearer INLINE_BEARER_XYZ "
        f"Authorization: Token {CANARIES['deepgram_api_key']} "
        f"SARVAM_API_KEY={CANARIES['sarvam_api_key']} "
        f'{{"refresh_token": "1//0gINLINE_REFRESH_SECRET", "client_secret": "GOCSPX-INLINE_CLIENT_SEC"}}'
    )
    scrubbed = logging_config.scrub_secrets(raw_line)
    for canary in CANARIES.values():
        assert canary not in scrubbed
    for inline in ("INLINE_API_999", "INLINE_BEARER_XYZ", "1//0gINLINE_REFRESH_SECRET", "GOCSPX-INLINE_CLIENT_SEC"):
        assert inline not in scrubbed


def test_job_history_and_panel_api_logs_scrub_structured_and_raw_secrets(monkeypatch: pytest.MonkeyPatch):
    for attr, val in CANARIES.items():
        monkeypatch.setattr(settings, attr, val)

    # Write raw un-scrubbed secrets directly into doomscroller.log to test read-time scrubbing + /api/logs
    log_file = logging_config.get_log_file_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        f"2026-11-14 01:00:00 [INFO] app: boot token={CANARIES['review_token']}\n"
        f"2026-11-14 01:00:01 [ERROR] app: tg failed https://api.telegram.org/bot{CANARIES['telegram_bot_token']}/sendMessage\n",
        encoding="utf-8",
    )

    # Record structured job run with nested secrets + non-finite floats
    logging_config.record_job_run(
        "growth_collect",
        result={
            "access_token": "ya29.SECRET_ACCESS_TOKEN",
            "refresh_token": "1//SECRET_REFRESH_TOKEN",
            "client_secret": "GOCSPX-SECRET_CLIENT",
            "nested": [{"url": f"https://pixabay.com/api/?key={CANARIES['pixabay_api_key']}"}],
            "nan_metric": float("nan"),
        },
        duration_sec=float("inf"),
        ok=False,
        error=f"failed with REVIEW_TOKEN={CANARIES['review_token']}",
    )

    history = logging_config.read_job_history(limit=10)
    dumped_history = json.dumps(history)
    assert "ya29.SECRET_ACCESS_TOKEN" not in dumped_history
    assert "1//SECRET_REFRESH_TOKEN" not in dumped_history
    assert "GOCSPX-SECRET_CLIENT" not in dumped_history
    for canary in CANARIES.values():
        assert canary not in dumped_history

    client = _authed_panel_client(token=CANARIES["review_token"])
    all_logs = client.get("/api/logs?limit=50&level=all")
    assert all_logs.status_code == 200
    assert "no-store" in all_logs.headers.get("cache-control", "")
    body_text = all_logs.text
    for canary in CANARIES.values():
        assert canary not in body_text

    warn_logs = client.get("/api/logs?limit=50&level=warn").json()
    assert warn_logs["count"] == 1
    assert "[REDACTED]" in warn_logs["lines"][0]


def test_notify_redaction_header_injection_and_telegram_error_safety(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "review_token", CANARIES["review_token"])
    monkeypatch.setattr(settings, "telegram_bot_token", CANARIES["telegram_bot_token"])
    monkeypatch.setattr(settings, "telegram_chat_id", "12345")
    monkeypatch.setattr(settings, "ntfy_topic", "")

    # Tunnel URL file with embedded credentials, ?key=, CRLF, and fragment must strip cleanly
    notify.TUNNEL_URL_FILE.write_text(
        f"https://admin:PASS@demo-tunnel.trycloudflare.com/panel?key={CANARIES['review_token']}#frag\n",
        encoding="utf-8",
    )
    link = notify.review_link()
    assert link == "https://demo-tunnel.trycloudflare.com/panel"
    assert CANARIES["review_token"] not in link
    assert "PASS" not in link

    # Header sanitizer strips CRLF header injection and secrets
    hdr = notify._clean_header(f"Alert\r\nBcc: attacker@evil.com\r\nKey: {CANARIES['review_token']}")
    assert "\r" not in hdr and "\n" not in hdr
    assert CANARIES["review_token"] not in hdr

    # Failing httpx.post in Telegram delivery must never leak bot token in return dict or logs
    def _boom_post(url: str, **kwargs):
        raise httpx.ConnectError(f"DNS failure for {url}")

    monkeypatch.setattr(httpx, "post", _boom_post)
    out = notify.send_test_notification()
    dumped = json.dumps(out)
    assert CANARIES["telegram_bot_token"] not in dumped
    assert "CANARY_TG_BOT_SECRET" not in dumped


def test_broll_and_ingest_scrub_api_keys_and_proxy_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "pixabay_api_key", CANARIES["pixabay_api_key"])
    monkeypatch.setattr(settings, "pexels_api_key", CANARIES["pexels_api_key"])
    monkeypatch.setattr(settings, "proxy_url", "socks5://proxyuser:CANARY_PROXY_PASS_777@10.0.0.5:1080")

    broll_msg = broll.scrub_secrets(
        f"GET https://pixabay.com/api/videos/?key={CANARIES['pixabay_api_key']}&q=cats "
        f"hdr={CANARIES['pexels_api_key']}"
    )
    assert CANARIES["pixabay_api_key"] not in broll_msg
    assert CANARIES["pexels_api_key"] not in broll_msg

    ingest_msg = ingest._scrub_secrets(
        "ERROR: Unable to download via socks5://proxyuser:CANARY_PROXY_PASS_777@10.0.0.5:1080 "
        "or http://other:OTHER_SECRET_888@proxy.local:8080"
    )
    assert "CANARY_PROXY_PASS_777" not in ingest_msg
    assert "OTHER_SECRET_888" not in ingest_msg


def test_system_doctor_growth_doctor_oauth_status_and_backup_never_leak_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from scripts import doctor as sys_doctor
    from scripts import youtube_oauth

    # 1. Populate custom .env + OAuth token file + DB with all canary secrets
    env_path = tmp_path / ".env"
    env_lines = [
        f"REVIEW_TOKEN={CANARIES['review_token']}",
        f"TELEGRAM_BOT_TOKEN={CANARIES['telegram_bot_token']}",
        f"PIXABAY_API_KEY={CANARIES['pixabay_api_key']}",
        f"PEXELS_API_KEY={CANARIES['pexels_api_key']}",
        f"TENOR_API_KEY={CANARIES['tenor_api_key']}",
        f"GIPHY_API_KEY={CANARIES['giphy_api_key']}",
        f"FREESOUND_API_KEY={CANARIES['freesound_api_key']}",
        f"DEEPGRAM_API_KEY={CANARIES['deepgram_api_key']}",
        f"SARVAM_API_KEY={CANARIES['sarvam_api_key']}",
        "NTFY_TOPIC=CANARY_NTFY_SECRET_TOPIC_555",
    ]
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    for attr, val in CANARIES.items():
        monkeypatch.setattr(settings, attr, val)
    monkeypatch.setattr(settings, "ntfy_topic", "CANARY_NTFY_SECRET_TOPIC_555")

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    token_file = secrets_dir / "youtube_token.json"
    token_payload = {
        "token": "ya29.CANARY_OAUTH_ACCESS_TOKEN_111",
        "refresh_token": "1//0gCANARY_OAUTH_REFRESH_TOKEN_222",
        "client_id": "12345-canary.apps.googleusercontent.com",
        "client_secret": "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33",
        "scopes": [UPLOAD_SCOPE],
        "expiry": "2026-12-01T00:00:00Z",
    }
    token_file.write_text(json.dumps(token_payload), encoding="utf-8")
    monkeypatch.setattr(settings, "youtube_token_path", str(token_file))

    # 2. scripts/doctor.py dict + human stdout
    doc_dict = sys_doctor.run_doctor(fix=False, root=tmp_path, env_file=env_path, db_path=tmp_path / "test.db")
    sys_doctor._print_human(doc_dict)
    human_out = capsys.readouterr().out
    combined_doc = json.dumps(doc_dict) + "\n" + human_out
    assert doc_dict["env"]["review_token_set"] is True
    assert all(doc_dict["env"]["optional_keys"].values())
    for secret in (*CANARIES.values(), "CANARY_NTFY_SECRET_TOPIC_555"):
        assert secret not in combined_doc

    # 3. scripts/youtube_oauth.py inspect_token
    oauth_rep = youtube_oauth.inspect_token(str(token_file))
    youtube_oauth._print_status_report(oauth_rep, as_json=False)
    oauth_out = json.dumps(oauth_rep) + "\n" + capsys.readouterr().out
    assert oauth_rep["has_refresh_token"] is True
    assert oauth_rep["has_client_secret"] is True
    for oauth_secret in (
        "ya29.CANARY_OAUTH_ACCESS_TOKEN_111",
        "1//0gCANARY_OAUTH_REFRESH_TOKEN_222",
        "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33",
    ):
        assert oauth_secret not in oauth_out

    # 4. Growth doctor, /api/backup, channels export, and Markdown report
    local_mp4 = settings.content_path / "clips" / "c1.mp4"
    local_mp4.parent.mkdir(parents=True, exist_ok=True)
    local_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    with session_scope() as db:
        ch = Channel(
            slug="tech",
            name="Tech",
            client_secrets="secrets/tech_client_secret.json",
            token_path=str(token_file),
            enabled=True,
        )
        db.add(ch)
        db.flush()
        db.add(
            Video(
                id="clip_backup_1",
                source="local",
                source_id="loc_1",
                source_url=str(tmp_path / "private_local_folder" / "raw_video.mp4"),
                title=f"Title | [inject](https://evil.test/?key={CANARIES['review_token']})",
                status=VideoStatus.PENDING,
                channel_id=ch.id,
                transformed_path=str(local_mp4),
                meta={"clip_rendered": True},
            )
        )
        db.add(
            GrowthEvent(
                kind="publish_failed",
                channel_id=ch.id,
                summary=f"Failed with bot {CANARIES['telegram_bot_token']} and key={CANARIES['review_token']}",
            )
        )

    client = _authed_panel_client(token=CANARIES["review_token"])
    growth_doc_text = client.get("/api/doctor").text
    for oauth_secret in (
        "ya29.CANARY_OAUTH_ACCESS_TOKEN_111",
        "1//0gCANARY_OAUTH_REFRESH_TOKEN_222",
        "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33",
    ):
        assert oauth_secret not in growth_doc_text

    backup = client.get("/api/backup")
    assert backup.status_code == 200
    assert "no-store" in backup.headers.get("cache-control", "")
    backup_json = backup.json()
    backup_text = backup.text
    assert "token_path" not in backup_text
    assert "client_secrets" not in backup_text
    assert "private_local_folder" not in backup_text
    assert backup_json["queue"]["clips"][0]["source"] == ""

    ch_export_text = json.dumps(channels.export_channels_snapshot())
    assert "token_path" not in ch_export_text
    assert "client_secrets" not in ch_export_text

    md = report.export_markdown_report()
    assert CANARIES["review_token"] not in md
    assert CANARIES["telegram_bot_token"] not in md
    assert "CANARY_TG_BOT_SECRET" not in md
