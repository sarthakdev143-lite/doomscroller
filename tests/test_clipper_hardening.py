"""Hardening tests for clipper internals (Wave 9):
captions, ingest, broll, transcribe, sarvam, moments, edit, and music.
"""
from __future__ import annotations

import logging
import math
import struct
import traceback
from pathlib import Path

import numpy as np
import pytest

from app.clipper import ai, broll, captions, edit, ingest, moments, music, sarvam, transcribe
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.clipper.transcribe import Word
from app.config import settings


def _make_ttf_with_family_name(path: Path, family: str) -> Path:
    """Write a minimal OpenType/TrueType binary with a single UTF-16BE family name record."""
    encoded = family.encode("utf-16-be")
    name_header = struct.pack(">HHH", 0, 1, 6 + 12)
    name_record = struct.pack(">6H", 3, 1, 0x0409, 1, len(encoded), 0)
    name_table = name_header + name_record + encoded
    offset = 12 + 16
    sfnt_header = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    table_dir = b"name" + struct.pack(">III", 0, offset, len(name_table))
    path.write_bytes(sfnt_header + table_dir + name_table)
    return path


def _signals(duration: float = 120.0, length: int | None = None) -> Signals:
    n = int(math.ceil(max(0.0, duration))) if length is None else length
    return Signals(
        duration=duration,
        motion=np.full(n, 0.4),
        loudness=np.full(n, 0.4),
        brightness=np.full(n, 0.5),
        centroid=np.full(n, 0.5),
        centroid_weight=np.full(n, 1.0),
        cuts=[],
    )


# ---------- 1. captions.py ----------


def test_captions_ass_font_and_style_prevent_header_injection():
    malicious_font = "Arial,999,&H0000FF00\r\nDialogue: 0,0:00:00.00,0:00:05.00,Caption,,0,0,0,,PWNED"
    ass = captions.build_ass(
        [Word("hello", 0.0, 0.5)],
        "Hook line",
        5.0,
        malicious_font,
        creator="{\\an8}Some Creator",
    )
    lines = ass.splitlines()
    style_lines = [ln for ln in lines if ln.startswith("Style: ")]
    assert len(style_lines) == 5
    for style_line in style_lines:
        # Format has 23 fields -> exactly 22 commas per Style row.
        assert style_line.count(",") == 22
    assert not any("PWNED" in ln for ln in lines)
    # Existing escaped creator format remains intact.
    assert any("Credit: (an8)Some Creator" in ln for ln in lines)

    # Direct _steady_text calls must also sanitize text, style, and control bytes.
    steady = captions._steady_text("Credit,Injected\nStyle: Bad", "Credit: {\\fs200}Creator\x00Name", 5.0, 1480)
    assert len(steady) == 1
    assert "\\fs200" not in steady[0]
    assert "\x00" not in steady[0]
    assert "\n" not in steady[0]
    assert "(fs200)CreatorName" in steady[0]


def test_captions_empty_sanitized_words_and_inverted_timestamps_are_safe():
    words = [
        Word("\\\\", 0.0, 0.3),
        Word("   ", 0.3, 0.6),
        Word("first\x00word", -0.5, -0.1),
        Word("second", 1.2, 0.9),
        Word("out_of_order", 0.8, 1.0),
    ]
    ass = captions.build_ass(words, "", 5.0, "Arial")
    assert "\x00" not in ass
    assert "FIRSTWORD" in ass

    caption_lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,")]
    assert len(caption_lines) == 3
    for ln in caption_lines:
        parts = ln.split(",", 9)
        start_sec = captions._seconds(parts[1])
        end_sec = captions._seconds(parts[2])
        assert 0.0 <= start_sec <= end_sec
        # Never emit an empty highlighted token.
        assert "}{\\r\\fs" not in parts[9]


def test_captions_clock_and_zero_duration_guards():
    assert captions._clock(float("nan")) == "0:00:00.00"
    assert captions._clock(float("inf")) == "0:00:00.00"
    assert captions._clock(-12.3) == "0:00:00.00"
    assert captions._clock(40000.0) == "9:59:59.99"

    assert captions._hook_event("Stop scrolling", 0.0) == []
    assert captions._hook_event("Stop scrolling", float("nan")) == []
    assert captions._steady_text("Brand", "BrandName", 0.0, 175) == []
    assert captions._steady_text("Brand", "BrandName", -3.0, 175) == []

    # Zero-duration beat is skipped; unrenderable Arabic label on a valid stat beat is omitted.
    beat_lines = captions._beat_events(
        [
            (1.0, "77%", "stat", "PER YEAR", 0.0),
            (2.0, "50%", "stat", "مرحبا", 1.5),
        ],
        None,
    )
    assert beat_lines
    assert not any("77%" in ln or "PER YEAR" in ln for ln in beat_lines)
    assert not any("مرحبا" in ln for ln in beat_lines)


def test_captions_font_family_strips_control_chars_and_commas(tmp_path):
    ttf = _make_ttf_with_family_name(tmp_path / "Custom,Font.ttf", "My,Font\x00\r\n{\\fs99}Bold")
    family = captions.font_family(str(ttf))
    assert family
    for forbidden in (",", "\x00", "\r", "\n", "{", "}", "\\"):
        assert forbidden not in family


# ---------- 2. ingest.py ----------


def test_ingest_strips_playlist_params_from_single_video_urls_but_rejects_pure_playlists(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))

    # Single-video links copied from playlists/Mixes lose list/index/start_radio params.
    clean_watch = ingest._normalise_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=4&t=9s")
    assert "v=dQw4w9WgXcQ" in clean_watch
    assert "t=9s" in clean_watch
    assert "list=" not in clean_watch and "index=" not in clean_watch

    clean_short = ingest._normalise_url("https://youtu.be/dQw4w9WgXcQ?list=RDdQw4w9WgXcQ&start_radio=1&t=12")
    assert "/dQw4w9WgXcQ" in clean_short
    assert "t=12" in clean_short
    assert "list=" not in clean_short and "start_radio=" not in clean_short

    clean_music = ingest._normalise_url("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVMdQw4w9WgXcQ")
    assert "v=dQw4w9WgXcQ" in clean_music
    assert "list=" not in clean_music

    # Pure playlist links remain untouched so they get rejected.
    pure_playlist = "https://www.youtube.com/playlist?list=PL123"
    assert "list=PL123" in ingest._normalise_url(pure_playlist)
    watch_without_v = "https://www.youtube.com/watch?list=PL123"
    assert "list=PL123" in ingest._normalise_url(watch_without_v)

    video = tmp_path / "dl.mp4"
    video.write_bytes(b"video")

    class PlaylistAwareDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            if "list=" in url:
                return {"_type": "playlist", "entries": [{"id": "1"}, {"id": "2"}]}
            return {"id": "dQw4w9WgXcQ", "title": "Single"}

        def process_ie_result(self, info, download):
            return {
                "id": info["id"],
                "title": info["title"],
                "extractor_key": "Youtube",
                "requested_downloads": [{"filepath": str(video)}],
            }

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", PlaylistAwareDL)

    fetched = ingest.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=2")
    assert fetched.path == str(video)

    with pytest.raises(ingest.IngestError, match="playlist") as caught:
        ingest.fetch(pure_playlist)
    assert caught.value.permanent is True


def test_ingest_refuses_multi_video_redirect_and_directory_filepath(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    import yt_dlp

    # 1. URL redirect resolving to a playlist in process_ie_result must be refused permanently.
    class RedirectToPlaylistDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            return {"_type": "url", "url": "https://example.com/playlist"}

        def process_ie_result(self, info, download):
            return {"_type": "playlist", "entries": [{"id": "a"}]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", RedirectToPlaylistDL)
    with pytest.raises(ingest.IngestError, match="playlist") as caught:
        ingest.fetch("https://example.com/shortlink")
    assert caught.value.permanent is True

    # 2. _type == "multi_video" without "entries" is refused before download.
    calls = []

    class MultiVideoDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            calls.append(("extract", download))
            return {"_type": "multi_video", "id": "bundle"}

        def process_ie_result(self, info, download):
            calls.append(("download", download))
            return info

    monkeypatch.setattr(yt_dlp, "YoutubeDL", MultiVideoDL)
    with pytest.raises(ingest.IngestError, match="playlist") as caught:
        ingest.fetch("https://example.com/bundle")
    assert caught.value.permanent is True
    assert calls == [("extract", False)]

    # 3. A directory returned as filepath is never accepted as a downloaded video file.
    folder_target = tmp_path / "not_a_file"
    folder_target.mkdir()
    assert ingest._downloaded_path({"filepath": str(folder_target)}) is None


def test_ingest_sanitizes_source_ids_expands_cookie_tilde_and_scrubs_proxy_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(settings, "cookies_file", "~/cookies.txt")
    opts = ingest.network_options()
    assert opts.get("cookiefile") == str(cookie)

    # Proxy credentials must never leak in IngestError message or chained traceback.
    monkeypatch.setattr(settings, "proxy_url", "http://proxyuser:ultra_secret_pw@10.0.0.1:8080")
    import yt_dlp

    class ProxyFailDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            raise RuntimeError("Tunnel failed via http://proxyuser:ultra_secret_pw@10.0.0.1:8080")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", ProxyFailDL)
    with pytest.raises(ingest.IngestError) as caught:
        ingest.fetch("https://youtube.com/watch?v=abc")
    assert "ultra_secret_pw" not in str(caught.value)
    assert "ultra_secret_pw" not in "".join(traceback.format_exception(caught.value))

    # Unsafe extractor_key / id cannot introduce path separators or '..'.
    video = tmp_path / "safe.mp4"
    video.write_bytes(b"ok")

    class TraversalMetaDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            return {"id": "ok"}

        def process_ie_result(self, info, download):
            return {
                "id": "../../etc/passwd",
                "extractor_key": "../Evil\\Extractor",
                "title": "Clip",
                "requested_downloads": [{"filepath": str(video)}],
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", TraversalMetaDL)
    src = ingest.fetch("https://example.com/watch")
    assert ".." not in src.source and "/" not in src.source and "\\" not in src.source
    assert ".." not in src.source_id and "/" not in src.source_id and "\\" not in src.source_id


# ---------- 3. broll.py & edit.attach_stock ----------


@pytest.mark.parametrize("exc_factory", [
    lambda: broll.httpx.ConnectError("GET https://pixabay.com/api/videos/?key=pix_secret_999 failed"),
    lambda: RuntimeError("SSL boom https://pixabay.com/api/videos/?key=pix_secret_999"),
])
def test_broll_api_keys_never_leak_in_chained_tracebacks_logs_or_warnings(
    tmp_path, monkeypatch, caplog, exc_factory
):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "pexels_api_key", "")
    monkeypatch.setattr(settings, "pixabay_api_key", "pix_secret_999")

    def failing_get(*a, **k):
        raise exc_factory()

    monkeypatch.setattr(broll.httpx, "get", failing_get)
    with pytest.raises(broll.StockError) as caught:
        broll.search("ocean waves")
    assert caught.value.__cause__ is None
    assert "pix_secret_999" not in str(caught.value)
    assert "pix_secret_999" not in "".join(traceback.format_exception(caught.value))

    # Bad JSON error path must also suppress __cause__ containing response URL/details.
    class BadJsonResponse:
        status_code = 200

        def json(self):
            raise ValueError("invalid json from https://pixabay.com/api/videos/?key=pix_secret_999")

    monkeypatch.setattr(broll.httpx, "get", lambda *a, **k: BadJsonResponse())
    with pytest.raises(broll.StockError) as json_caught:
        broll.search("ocean waves")
    assert json_caught.value.__cause__ is None
    assert "pix_secret_999" not in "".join(traceback.format_exception(json_caught.value))

    # _download error path must also scrub keys and suppress __cause__.
    def failing_stream(*a, **k):
        raise broll.httpx.ConnectError("cdn redirect https://pixabay.com/api/videos/?key=pix_secret_999")

    monkeypatch.setattr(broll.httpx, "stream", failing_stream)
    with pytest.raises(broll.StockError) as dl_caught:
        broll._download("https://cdn.example.com/clip.mp4", tmp_path / "broll" / "1.mp4")
    assert dl_caught.value.__cause__ is None
    assert "pix_secret_999" not in str(dl_caught.value)
    assert "pix_secret_999" not in "".join(traceback.format_exception(dl_caught.value))

    # edit.attach_stock must never leak PIXABAY_API_KEY or PEXELS_API_KEY in warnings or logs.
    monkeypatch.setattr(settings, "pexels_api_key", "pex_secret_888")
    monkeypatch.setattr(settings, "clip_broll", True)
    plan = edit.EditPlan(
        words=[Word("ocean", 4.0, 4.5)],
        segments=[(0.0, 15.0)],
        cutaways=[edit.Cutaway(at=4.0, seconds=2.0, query="ocean")],
    )

    def unexpected_boom(*a, **k):
        raise RuntimeError("unexpected leak pix_secret_999 and pex_secret_888 and ?key=inline_secret")

    monkeypatch.setattr(broll, "fetch", unexpected_boom)
    with caplog.at_level(logging.WARNING):
        warnings = edit.attach_stock(plan)
    combined = "\n".join(warnings) + "\n" + caplog.text
    assert "pix_secret_999" not in combined
    assert "pex_secret_888" not in combined
    assert "inline_secret" not in combined


def test_broll_sanitizes_video_id_path_traversal_and_malformed_metadata(tmp_path, monkeypatch):
    content_root = tmp_path / "content"
    content_root.mkdir()
    monkeypatch.setattr(settings, "content_dir", str(content_root))

    outside_victim = tmp_path / "victim.mp4"
    outside_victim.write_bytes(b"precious")

    monkeypatch.setattr(broll, "_playable", lambda path, seconds: False)
    monkeypatch.setattr(
        broll,
        "_download",
        lambda link, path: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"bad")),
    )

    cand = broll.Candidate(video_id="../../victim", link="https://cdn.example.com/v.mp4", duration=10.0)
    result = broll._local_copy(cand, 2.0)
    assert result is None
    # Outside file must NEVER be unlinked or touched.
    assert outside_victim.exists() and outside_victim.read_bytes() == b"precious"

    # Bool dimensions must not pass isinstance(..., int), and NaN/Inf/negative numbers become 0.0.
    assert broll.best_file({
        "video_files": [{"link": "https://x/v.mp4", "width": True, "height": True, "file_type": "video/mp4"}]
    }) is None
    assert broll._number(float("nan")) == 0.0
    assert broll._number(float("inf")) == 0.0
    assert broll._number(-10.0) == 0.0
    assert broll._number(True) == 0.0
    assert broll._smallest_thumbnail({"id": 7}, {"thumbnail": "https://x/thumb.jpg"}) == "https://x/thumb.jpg"


# ---------- 4. transcribe.py & sarvam.py ----------


def test_transcribe_normalizes_missing_end_negative_nan_and_null_entries(tmp_path):
    valid_words = [
        {"word": f"word{i}", "start": 1.0 + i * 0.4, "end": 1.3 + i * 0.4, "confidence": 0.95}
        for i in range(60)
    ]
    payload = {
        "results": {
            "channels": [
                None,
                {
                    "detected_language": "en",
                    "alternatives": [
                        None,
                        {
                            "words": [
                                None,
                                {"word": "opener", "start": -0.5, "end": -0.1, "confidence": "nan"},
                                {"word": "midway", "start": 0.5, "end": None, "confidence": 0.9},
                                *valid_words,
                            ]
                        },
                    ],
                },
            ]
        }
    }
    tr = transcribe.parse_response(payload, duration=45.0)
    assert tr.words[0].text == "opener"
    assert tr.words[0].start == 0.0 and tr.words[0].end >= 0.0
    assert math.isfinite(tr.words[0].confidence)
    assert tr.words[1].text == "midway"
    assert tr.words[1].end >= tr.words[1].start == 0.5
    # One NaN confidence must not poison the whole transcript's speech gate.
    assert tr.is_english_speech is True

    # Cached transcripts with corrupted timestamps/NaN confidence must also normalize cleanly.
    fp = {"content": "abc123"}
    cache_file = tmp_path / "cached.json"
    cache_file.write_text(
        '{"source": {"content": "abc123"}, "language": "en", "words": '
        '[{"text": "hi", "start": -2.0, "end": -5.0, "confidence": 0.9}]}',
        encoding="utf-8",
    )
    loaded = transcribe._load_cache(str(cache_file), fp, duration=10.0)
    assert loaded is not None
    assert 0.0 <= loaded.words[0].start <= loaded.words[0].end

    # Non-finite duration is never considered speech.
    nan_tr = transcribe.Transcript(words=tr.words, language="en", duration=float("nan"))
    assert nan_tr.is_english_speech is False


def test_sarvam_basename_upload_clamped_timestamps_and_nan_duration_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam_key")

    # 1. Only the basename is uploaded, never local directory hierarchy,
    # on either OS separator style.
    assert sarvam._basename("C:\\Users\\me\\audio\\seg_0.wav") == "seg_0.wav"
    assert sarvam._basename("/tmp/audio/seg_0.wav") == "seg_0.wav"
    assert sarvam._basename("") == "audio.wav"
    audio_seg = tmp_path / "seg_0.wav"
    audio_seg.write_bytes(b"RIFF")
    captured_filename = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"transcript": "नमस्ते"}

    def fake_post(url, *, headers, data, files, timeout):
        captured_filename.append(files["file"][0])
        return FakeResp()

    monkeypatch.setattr(sarvam.httpx, "post", fake_post)
    sarvam._call(str(audio_seg), "hi-IN")
    assert captured_filename == ["seg_0.wav"]

    # 2. Out-of-window, inverted, or NaN chunk timestamps are clamped to [offset, offset + seg_len].
    words = sarvam._words_from(
        {
            "timestamps": {
                "words": ["नमस्ते दुनिया", "आगे बढ़ो"],
                "start_time_seconds": [-3.0, 20.0],
                "end_time_seconds": [40.0, float("nan")],
            },
            "transcript": "नमस्ते दुनिया आगे बढ़ो",
        },
        offset=25.0,
        seg_len=25.0,
    )
    assert words
    for w in words:
        assert math.isfinite(w.start) and math.isfinite(w.end)
        assert 25.0 <= w.start <= w.end <= 50.0

    # 3. NaN or infinite duration returns None immediately without looping or invoking ffmpeg.
    called_segment = []
    monkeypatch.setattr(sarvam, "_segment", lambda *a, **k: called_segment.append(a))
    assert sarvam.transcribe("audio.mp3", float("nan")) is None
    assert sarvam.transcribe("audio.mp3", float("inf")) is None
    assert called_segment == []


# ---------- 5. moments.py & edit.py ----------


def test_moments_zero_count_empty_interest_and_non_mutating_visual_pick(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: False)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: None)

    sig = _signals(120.0)
    words = [Word(f"word{i}.", i * 1.0, i * 1.0 + 0.6) for i in range(40)]
    info = MediaInfo(duration=120.0, width=1920, height=1080, fps=30.0, has_audio=True)

    assert moments._dedupe([Moment(0.0, 20.0, score=5.0)], count=0) == []
    assert moments.rank_windows(sig, count=0) == []
    assert moments.pick_speech(words, sig, count=0) == []
    assert moments.pick_visual("x.mp4", info, sig, count=0) == []

    # Invalid / zero / NaN durations return [] rather than negative or NaN moments.
    for bad_dur in (0.0, -5.0, float("nan"), float("inf")):
        assert moments.rank_windows(_signals(0.0, length=0) if bad_dur <= 0 else Signals(
            duration=bad_dur,
            motion=np.array([]),
            loudness=np.array([]),
            brightness=np.array([]),
            centroid=np.array([]),
            centroid_weight=np.array([]),
            cuts=[],
        ), count=2) == []

    # Empty or shorter-than-window signal arrays on a long video must not crash np.convolve or produce NaN.
    empty_long = _signals(120.0, length=0)
    ranked_empty = moments.rank_windows(empty_long, count=2)
    assert ranked_empty and all(math.isfinite(m.score) for m in ranked_empty)

    short_arr_long = _signals(120.0, length=5)
    ranked_short = moments.rank_windows(short_arr_long, count=2)
    assert ranked_short and all(math.isfinite(m.score) for m in ranked_short)

    speech_picked = moments._rule_based_speech(moments.build_sentences(words), words, empty_long, count=2)
    assert speech_picked and all(math.isfinite(m.score) and 0.0 <= m.start <= m.end <= 120.0 for m in speech_picked)

    # Non-dict items in AI reply lists must be ignored cleanly.
    sents = moments.build_sentences(words)
    assert moments._moments_from_reply({"clips": [None, "bad", 42]}, sents, words, 120.0) == []
    assert moments._complete_payoffs({"clips": [None, "bad", 42]}, sents, words, 120.0) == {"clips": [None, "bad", 42]}

    # Duplicate candidate numbers in pick_visual reply must not overwrite the winning candidate in place.
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(
        moments,
        "_ai_rank_visual",
        lambda *a, **k: {
            "clips": [
                {"candidate": 1, "title": "Best Shot", "hook": "Watch", "score": 95},
                {"candidate": 1, "title": "Low Duplicate", "hook": "Skip", "score": 10},
            ]
        },
    )
    picked = moments.pick_visual("x.mp4", info, sig, count=1, title="Film")
    assert len(picked) == 1
    assert picked[0].title == "Best Shot"
    assert picked[0].score == 95.0


def test_edit_zero_speed_empty_moment_fallback_and_monotonic_output_words(monkeypatch):
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: None)

    for bad_speed in (0.0, -1.25, float("nan"), float("inf")):
        plan = edit.EditPlan(words=[Word("hello", 1.0, 1.5)], segments=[(1.0, 3.0)], speed=bad_speed)
        assert plan.duration == pytest.approx(2.0)
        assert plan.to_output(1.5) == pytest.approx(0.5)

    # A speech moment whose words list is empty still yields a playable non-empty segment span.
    fallback_plan = edit.build_edit(Moment(10.0, 25.0), [])
    assert fallback_plan.segments == [(10.0, 25.0)]
    assert fallback_plan.duration > 0

    # Inverted word timestamps never produce end < start in output_words().
    inv_plan = edit.EditPlan(words=[Word("flip", 2.0, 1.2)], segments=[(1.0, 3.0)], speed=1.0)
    out_words = inv_plan.output_words()
    assert len(out_words) == 1
    assert 0.0 <= out_words[0].start <= out_words[0].end


# ---------- 6. music.py ----------


def test_music_tilde_whitespace_folder_zero_byte_dotfiles_and_credit_sanitization(tmp_path, monkeypatch):
    content_root = tmp_path / "content"
    (content_root / "music").mkdir(parents=True)
    monkeypatch.setattr(settings, "content_dir", str(content_root))

    # Whitespace-only clip_music_dir falls back to content_path / "music".
    monkeypatch.setattr(settings, "clip_music_dir", "   ")
    assert music.folder() == content_root / "music"

    # Tilde expansion works on clip_music_dir.
    home_dir = tmp_path / "home"
    music_dir = home_dir / "my_tracks"
    music_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setattr(settings, "clip_music_dir", "~/my_tracks")
    assert music.folder() == music_dir

    # library() skips 0-byte tracks and hidden dotfiles.
    (music_dir / "empty.mp3").write_bytes(b"")
    (music_dir / "._appledouble.mp3").write_bytes(b"not_audio")
    (music_dir / ".hidden.wav").write_bytes(b"RIFF")
    good_track = music_dir / "good.mp3"
    good_track.write_bytes(b"ID3DATA")
    assert music.library() == [good_track]

    # credit() handles str or Path, strips UTF-8 BOM, survives non-UTF8 bytes, strips <>/nulls/newlines.
    (music_dir / "good.txt").write_bytes(
        "\ufeffMusic: Café".encode("utf-8") + b" \xe9 <https://example.com>\r\nLine 2\x00"
    )
    line = music.credit(str(good_track))
    assert not line.startswith("\ufeff")
    assert "<" not in line and ">" not in line and "\x00" not in line
    assert "\n" not in line and "\r" not in line
    assert "Music: Café" in line and "Line 2" in line

    # Fallback to <track.name>.txt when <stem>.txt does not exist.
    alt_track = music_dir / "second.ogg"
    alt_track.write_bytes(b"OggS")
    (music_dir / "second.ogg.txt").write_text("Music: Second Track (CC BY)\n", encoding="utf-8")
    assert music.credit(alt_track) == "Music: Second Track (CC BY)"
