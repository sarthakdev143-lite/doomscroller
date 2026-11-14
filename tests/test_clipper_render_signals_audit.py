"""Wave 10 TDD audit suite for clipper media, render, signals, timeline, sfx,
shots, faces, subtitles, audio_qa, ai, and orphaned job recovery.
"""
from __future__ import annotations

import ast
import json
import math
import subprocess
import sys
import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from app.clipper import (
    ai,
    audio_qa,
    faces,
    job,
    media,
    render,
    sfx,
    shots,
    signals,
    subtitles,
    timeline,
)
from app.clipper.media import MediaError, MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.config import settings
from app.db import init_db, session_scope
from app.models import ClipJob


def _make_signals(duration: float = 60.0, n: int | None = None) -> Signals:
    secs = int(math.ceil(max(0.0, duration))) if n is None else n
    return Signals(
        duration=duration,
        motion=np.full(secs, 0.3),
        loudness=np.full(secs, -20.0),
        brightness=np.full(secs, 0.5),
        centroid=np.full(secs, 0.5),
        centroid_weight=np.full(secs, 1.0),
        cuts=[],
    )


# ---------- 1. media.py ----------


def test_media_frame_rate_duration_and_ffprobe_stdin_hardening(monkeypatch):
    assert media._frame_rate("0/0") == 0.0
    assert media._frame_rate("30/0") == 0.0
    assert media._frame_rate("-30/1") == 0.0
    assert media._frame_rate("30/-1") == 0.0
    assert media._frame_rate("nan/1") == 0.0
    assert media._frame_rate("inf/1") == 0.0
    assert media._frame_rate("30000/1001") == pytest.approx(30000 / 1001)

    assert media._seconds("nan") == 0.0
    assert media._seconds("inf") == 0.0
    assert media._seconds("-5.0") == 0.0
    assert media._seconds(True) == 0.0

    seen_kwargs = {}

    def fake_probe_run(cmd, **kwargs):
        seen_kwargs.update(kwargs)
        body = json.dumps({
            "format": {"duration": "12.5"},
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "0/0", "r_frame_rate": "30/1"}],
        })
        return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_probe_run)
    info = media.probe("sample.mp4")
    assert seen_kwargs.get("stdin") == subprocess.DEVNULL
    assert info.fps == pytest.approx(30.0)

    # Malformed JSON, NaN duration, or zero video dimensions must raise MediaError cleanly.
    for bad_stdout in (
        "{not-json",
        json.dumps({"format": {"duration": "nan"}, "streams": [{"codec_type": "video", "width": 1280, "height": 720}]}),
        json.dumps({"format": {"duration": "15.0"}, "streams": [{"codec_type": "video", "width": 0, "height": 720}]}),
    ):
        monkeypatch.setattr(
            media.subprocess,
            "run",
            lambda cmd, out=bad_stdout, **kw: subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=""),
        )
        with pytest.raises(MediaError):
            media.probe("bad.mp4")


# ---------- 2. render.py ----------


def test_render_filter_path_escaping_even_dims_bad_speed_and_temp_cleanup(tmp_path, monkeypatch):
    escaped = render.escape_filter_path("C:\\Users\\O'Brien\\work\\clip:1.ass\x00\n")
    assert "\\" not in escaped.replace("\\:", "").replace("'\\''", "")
    assert "C\\:/Users/O'\\''Brien/work/clip\\:1.ass" == escaped
    assert "\x00" not in escaped and "\n" not in escaped

    info = MediaInfo(duration=60.0, width=1280, height=720, fps=30.0, has_audio=True)
    sig = _make_signals(60.0)

    # Default graph preserves exact relative ass=clip.ass:fontsdir=fonts
    default_plan = render.plan_clip(Moment(5.0, 20.0), [], sig, info, has_graphics=True)
    assert "ass=clip.ass:fontsdir=fonts" in render.build_filtergraph(default_plan, info, 0.0)

    # Blur-fill foreground scale must enforce even dimensions for yuv420p.
    wide_sig = _make_signals(60.0)
    wide_sig.centroid[5:25] = np.linspace(0.05, 0.95, 20)
    blur_plan = render.plan_clip(Moment(5.0, 25.0, kind="visual"), [], wide_sig, info, has_graphics=False)
    assert blur_plan.use_blur_fill is True
    blur_graph = render.build_filtergraph(blur_plan, info, 0.0)
    assert "force_original_aspect_ratio=decrease:force_divisible_by=2" in blur_graph

    # Odd portrait dimensions produce an even crop_width; _picture_height handles 0x0 and NaN crop_bottom.
    odd_portrait = MediaInfo(duration=30.0, width=721, height=1281, fps=30.0, has_audio=False)
    odd_plan = render.plan_clip(Moment(2.0, 12.0), [], _make_signals(30.0), odd_portrait, has_graphics=False)
    assert odd_plan.crop_width % 2 == 0
    monkeypatch.setattr(settings, "clip_layout", "framed")
    assert render._picture_height(MediaInfo(10.0, 0, 0, 30.0, False), float("nan")) == 0
    assert render._picture_height(info, float("nan")) % 2 == 0

    # with_end_hold never inverts a segment if source_duration < segment end.
    held = render.with_end_hold([(10.1, 10.3)], source_duration=10.0)
    assert held[0][1] >= held[0][0]

    # Zero/NaN speed and NaN face centres do not crash _crop_pieces, speaker_pieces, or build_filtergraph.
    monkeypatch.setattr(settings, "clip_layout", "fill")
    pieces = render._crop_pieces(
        Moment(5.0, 20.0), [(5.0, 20.0)], sig, info, 404, faces={5.0: float("nan")}, speed=0.0
    )
    assert pieces and all(math.isfinite(x) and int(x) % 2 == 0 for _, x in pieces)

    slow_plan = render.plan_clip(Moment(5.0, 20.0), [], sig, info, has_graphics=False)
    slow_plan.speed = 0.25
    slow_graph = render.build_filtergraph(slow_plan, info, 0.0)
    assert "atempo=0.5,atempo=0.5" in slow_graph

    # Temp .tmp.mp4 file is always cleaned up when render fails after writing partial bytes,
    # and a missing resolved font file degrades cleanly instead of crashing on shutil.copy.
    out_clip = tmp_path / "out" / "rendered.mp4"
    partial_clip = Path(f"{out_clip}.tmp.mp4")
    monkeypatch.setattr(render, "resolve_font", lambda *a, **k: str(tmp_path / "missing_font.ttf"))

    def failing_ffmpeg(args, *, cwd=None, timeout=3600):
        Path(args[-1]).write_bytes(b"x" * 500)  # written <1000 bytes before failure
        raise MediaError("encode failed")

    monkeypatch.setattr(render, "run_ffmpeg", failing_ffmpeg)
    with pytest.raises(MediaError, match="encode failed"):
        render.render_moment("source.mp4", info, sig, Moment(5.0, 20.0, hook="Hook"), [], str(out_clip))
    assert not partial_clip.exists()


# ---------- 3. timeline.py, signals.py, sfx.py ----------


def test_timeline_signals_and_sfx_numerical_and_buffer_guards(tmp_path, monkeypatch):
    assert timeline.on_grid(float("nan")) == 0.0
    assert timeline.on_grid(float("inf")) == 0.0
    assert timeline.on_grid(-4.0) == 0.0

    assert timeline.keep_segments(10.0, 5.0, [], 0.35) == []
    assert timeline.keep_segments(float("nan"), 10.0, [], 0.35) == []
    assert timeline.output_time([(0.0, 5.0)], float("nan")) is None
    assert timeline.source_time([(2.0, 5.0)], float("nan")) == 2.0

    # Odd-byte truncated PCM output from ffmpeg must not raise ValueError in np.frombuffer(..., int16).
    monkeypatch.setattr(signals, "read_ffmpeg", lambda args, **k: b"\x01\x02\x03\x04\x05")
    loud = signals._loudness("clip.mp4", 3, has_audio=True)
    assert loud.shape == (3,) and np.all(np.isfinite(loud))

    # Empty Signals arrays and NaN span endpoints stay finite and safe.
    empty_sig = _make_signals(0.0, n=0)
    assert empty_sig.interest().size == 0
    assert empty_sig.centre_between(float("nan"), 5.0) == 0.5
    assert empty_sig.centre_spread(0.0, float("inf")) == 0.0

    # sfx.build_track handles non-positive/NaN duration, unknown sound, NaN cue times, and heavy overlap clamping.
    wav_path = tmp_path / "sfx.wav"
    heavy_cues = [
        sfx.Cue(float("nan"), sfx.IMPACT),
        sfx.Cue(0.0, "unknown_cue"),
        *[sfx.Cue(0.0, sfx.IMPACT) for _ in range(20)],
        *[sfx.Cue(0.0, sfx.WHOOSH) for _ in range(20)],
    ]
    sfx.build_track(str(wav_path), float("nan"), heavy_cues)
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == sfx.RATE
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        assert pcm.size > 0 and int(pcm.max()) <= 32767 and int(pcm.min()) >= -32768


# ---------- 4. shots.py & faces.py ----------


def test_shots_zero_cv2_and_tiny_empty_or_non_rgb_thumbnails(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", "import sys, app.clipper.shots; assert 'cv2' not in sys.modules"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    assert shots._write_sheet([], str(tmp_path / "empty.jpg")) is False
    assert shots._as_rendered(np.zeros((0, 10, 3), dtype=np.uint8)).shape == (1, 1, 3)

    # Grayscale 2D and RGBA 4-channel tiny odd thumbnails render cleanly onto contact sheet.
    sheet_path = tmp_path / "sheet.jpg"
    gray_tiny = np.full((3, 5), 180, dtype=np.uint8)
    rgba_tiny = np.full((5, 3, 4), 200, dtype=np.uint8)
    assert shots._write_sheet([gray_tiny, rgba_tiny], str(sheet_path)) is True
    assert sheet_path.is_file() and sheet_path.stat().st_size > 0

    assert shots._reply_search({"search": "rain\x00\x07storm"}) == "rainstorm"


def test_faces_nan_boxes_out_of_bounds_and_ultrawide_even_height(monkeypatch):
    class FakeDetector:
        def detectMultiScale(self, frame, **kwargs):
            return [
                (float("nan"), 10, 50, 50),
                (10, 10, 0, 40),
                (900, 900, 50, 50),  # completely outside 200x320 frame -> empty slice
                (80, 50, 60, 60),    # valid face box
            ]

    monkeypatch.setattr(faces, "_detector", lambda: FakeDetector())
    first = np.zeros((200, 320), dtype=np.uint8)
    second = np.zeros((200, 320), dtype=np.uint8)
    second[50:120, 80:140] = 120
    box = faces.speaker_box_in_pair(first, second)
    assert box is not None
    cx, cy, score = box
    assert 0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0 and math.isfinite(score)

    # Ultra-wide aspect ratio must never compute height=0 for ffmpeg rawvideo scale.
    heights_requested = []

    def fake_frames_at(source, when, height):
        heights_requested.append(height)
        return np.zeros((height, faces._DETECT_WIDTH), dtype=np.uint8), np.zeros((height, faces._DETECT_WIDTH), dtype=np.uint8)

    monkeypatch.setattr(faces, "_frames_at", fake_frames_at)
    ultrawide = MediaInfo(duration=10.0, width=4000, height=10, fps=30.0, has_audio=False)
    faces.face_centre("wide.mp4", 0.0, 5.0, ultrawide)
    faces.speaker_position("wide.mp4", 0.0, 5.0, ultrawide)
    assert heights_requested and all(h >= 2 and h % 2 == 0 for h in heights_requested)


# ---------- 5. subtitles.py, audio_qa.py, ai.py, subprocess audit ----------


def test_subtitles_nan_duration_zero_byte_frames_and_vtt_parsing(monkeypatch):
    monkeypatch.setattr(subtitles.ai, "available", lambda: True)
    ffmpeg_calls = []
    monkeypatch.setattr(subtitles, "run_ffmpeg", lambda args: ffmpeg_calls.append(args))

    nan_info = MediaInfo(duration=float("nan"), width=1280, height=720, fps=30.0, has_audio=True)
    assert subtitles.already_captioned("v.mp4", nan_info) is False
    assert ffmpeg_calls == []

    # 0-byte frame outputs must not be sent to vision AI.
    monkeypatch.setattr(subtitles, "run_ffmpeg", lambda args: Path(args[-1]).write_bytes(b""))
    ai_calls = []
    monkeypatch.setattr(subtitles.ai, "ask_json", lambda *a, **k: ai_calls.append(1) or {"captions": True})
    assert subtitles.already_captioned("v.mp4", MediaInfo(20.0, 1280, 720, 30.0, True)) is False
    assert ai_calls == []

    # Malformed / non-UTF8 / BOM WebVTT cue parsing.
    raw_vtt = (
        b"\xef\xbb\xbfWEBVTT\r\n\r\n"
        b"NOTE malformed comment block\r\n\r\n"
        b"00:01.000 --> 00:03.500 align:start\r\n"
        b"<c.yellow>Hello</c> \xff world\r\n\r\n"
        b"bad --> timestamp\r\nignored\r\n\r\n"
        b"00:08.000 --> 00:05.000\r\ninverted\r\n"
    )
    cues = subtitles.parse_vtt(raw_vtt)
    assert len(cues) == 1
    assert cues[0][0] == pytest.approx(1.0) and cues[0][1] == pytest.approx(3.5)
    assert "Hello" in cues[0][2] and "<c" not in cues[0][2]


def test_audio_qa_and_ai_timeout_non_utf8_and_malformed_json_guards(tmp_path, monkeypatch):
    assert audio_qa.summarise({"verdict": "ok", "score": True, "actions": "not-a-list"}) == {
        "verdict": "ok",
        "score": None,
        "in_range": None,
        "scored": None,
        "issues": [],
    }
    assert audio_qa.summarise({"verdict": "ok", "score": float("nan"), "actions": None})["score"] is None
    assert audio_qa.summarise({"verdict": "ok", "score": float("inf"), "actions": [None, 123]})["score"] is None

    # Directory path is ignored without shelling out; non-positive timeout is clamped > 0.
    monkeypatch.setattr(audio_qa, "earshot_bin", lambda: "/bin/earshot")
    monkeypatch.setattr(settings, "earshot_qa_timeout_sec", 0)
    seen_timeouts = []

    def fake_earshot(cmd, **kw):
        seen_timeouts.append(kw.get("timeout"))
        return subprocess.CompletedProcess(
            cmd, 0, stdout=b"warning line\n" + b'{"verdict":"good","score":0.9,"actions":[]}', stderr=b"\xff"
        )

    monkeypatch.setattr(audio_qa.subprocess, "run", fake_earshot)
    assert audio_qa.score(str(tmp_path)) is None
    assert seen_timeouts == []

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"mp4")
    scored = audio_qa.score(str(clip))
    assert scored is not None and scored["verdict"] == "good"
    assert seen_timeouts[0] >= 1

    # ai stream parsers tolerate BOM, raw non-UTF8 bytes, and malformed JSON lines; ask_json requires dict.
    raw_events = b"\xef\xbb\xbf{bad json\n" + b'{"type":"text","part":{"text":"hi \xff"}}\n'
    assert "hi" in ai._text_from_events(raw_events)
    raw_claude = b"\xef\xbb\xbfnot-json\n" + b'{"type":"result","is_error":false,"result":"done \xfe"}\n'
    assert "done" in (ai._result_from_stream(raw_claude) or "")

    monkeypatch.setattr(ai, "ask", lambda *a, **k: "[1, 2, 3]")
    assert ai.ask_json("give object") is None


def test_all_owned_subprocesses_close_stdin_and_use_background_kwargs():
    for rel in ("app/clipper/media.py", "app/clipper/audio_qa.py", "app/clipper/ai.py"):
        tree = ast.parse(Path(rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("run", "Popen"):
                kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
                has_star_kwargs = any(kw.arg is None for kw in node.keywords)
                assert has_star_kwargs, f"{rel}:{node.lineno} missing **background_subprocess_kwargs"
                assert ("stdin" in kw_names or "input" in kw_names or has_star_kwargs), (
                    f"{rel}:{node.lineno} missing stdin=DEVNULL or input="
                )
                if rel != "app/clipper/ai.py":
                    assert "stdin" in kw_names or "input" in kw_names, f"{rel}:{node.lineno} must pass stdin=DEVNULL"


# ---------- 6. job.py orphaned running recovery ----------


def test_job_recover_interrupted_jobs_recovers_orphans_without_clobbering_active_run(monkeypatch):
    init_db()
    with session_scope() as db:
        db.query(ClipJob).delete()
        orphan = ClipJob(source="https://example.com/orphan", status="running", stage="rendering clip 2 of 3")
        done = ClipJob(source="https://example.com/done", status="done", stage="")
        failed = ClipJob(source="https://example.com/failed", status="failed", error="bad")
        db.add_all([orphan, done, failed])
        db.flush()
        orphan_id, done_id, failed_id = orphan.id, done.id, failed.id

    recovered = job.recover_interrupted_jobs()
    assert recovered == 1
    with session_scope() as db:
        row = db.get(ClipJob, orphan_id)
        assert row.status == "queued" and row.stage == "" and row.started_at is None
        assert db.get(ClipJob, done_id).status == "done"
        assert db.get(ClipJob, failed_id).status == "failed"

    # Now verify an active in-process run is NEVER clobbered by recover_interrupted_jobs() or start_queue_worker().
    entered = threading.Event()
    proceed = threading.Event()
    observed_mid_flight = {}

    def slow_clip_source(source, **kwargs):
        entered.set()
        assert proceed.wait(timeout=5.0)
        return {"ok": True, "clips": []}

    monkeypatch.setattr(job, "clip_source", slow_clip_source)
    worker = threading.Thread(target=job.clip_queue_job, daemon=True)
    worker.start()
    assert entered.wait(timeout=5.0)
    try:
        # While worker is actively running orphan_id in-process:
        observed_mid_flight["recovered_during_active"] = job.recover_interrupted_jobs()
        with session_scope() as db:
            observed_mid_flight["status_during_active"] = db.get(ClipJob, orphan_id).status
    finally:
        proceed.set()
        worker.join(timeout=5.0)

    assert observed_mid_flight["recovered_during_active"] == 0
    assert observed_mid_flight["status_during_active"] == "running"
    with session_scope() as db:
        assert db.get(ClipJob, orphan_id).status == "done"
