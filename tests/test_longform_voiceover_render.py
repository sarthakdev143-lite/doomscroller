"""Unit and integration tests for M1 voiceover & render audio limiting, full-duration trims, pills, outro CTA, and watermark."""
from __future__ import annotations

import base64
import io
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from app.clipper.media import MediaInfo
from app.config import settings
from app.longform import planner, render as lf_render, voiceover


def _valid_wav_bytes(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x01\x00" * int(24000 * seconds))
    return buf.getvalue()


def test_synthesize_wav_filter_includes_alimiter(tmp_path, monkeypatch):
    valid = _valid_wav_bytes(1.0)
    b64 = base64.b64encode(valid).decode("ascii")
    ffmpeg_cmds: list[list[str]] = []

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"audios": [b64]}

    def fake_run_ffmpeg(args, **kwargs):
        ffmpeg_cmds.append(list(args))
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(valid)

    monkeypatch.setattr(settings, "sarvam_api_key", "sk_test")
    monkeypatch.setattr(voiceover.httpx, "post", lambda *a, **kw: FakeResp())
    monkeypatch.setattr(voiceover, "run_ffmpeg", fake_run_ffmpeg)

    dur = voiceover.synthesize_wav("Testing limiter", tmp_path / "out.wav", language="en")
    assert dur and dur >= 0.4
    af = ffmpeg_cmds[0][ffmpeg_cmds[0].index("-af") + 1]
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1" in af


def test_render_narrator_bridge_full_audio_trim_and_alimiter(tmp_path, monkeypatch):
    wav = tmp_path / "vo.wav"
    wav.write_bytes(_valid_wav_bytes(2.0))
    src = tmp_path / "src.mp4"
    src.write_bytes(b"MP4")
    music = tmp_path / "music.mp3"
    music.write_bytes(b"MP3")

    monkeypatch.setattr(lf_render, "_find_bright_offset", lambda *a, **k: 5.0)
    monkeypatch.setattr(lf_render, "_probe_duration", lambda p, f: f)

    captured_fc: list[str] = []

    def fake_ffmpeg(args, **kwargs):
        captured_fc.append(args[args.index("-filter_complex") + 1])
        Path(args[-1]).write_bytes(b"X" * 1000)

    monkeypatch.setattr(voiceover, "run_ffmpeg", fake_ffmpeg)

    # Branch 1: src_info.has_audio=True, no music -> trims [0:a] over full dur (4.350), not dur/1.18 (3.686)
    monkeypatch.setattr(voiceover, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, True))
    voiceover.render_narrator_bridge(
        wav, src, tmp_path / "b1.mp4", tmp_path,
        badge_text="#1 · SOURAV JOSHI", headline="Pahadon Mein Train", brand_name="@Brand", duration=4.0,
    )
    fc1 = captured_fc[-1]
    assert "[0:a]atrim=0:4.350," in fc1
    assert "[0:a]atrim=0:3.686" not in fc1
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in fc1

    # Branch 2: has_music=True
    voiceover.render_narrator_bridge(
        wav, src, tmp_path / "b2.mp4", tmp_path,
        badge_text="#1 · SOURAV JOSHI", headline="Pahadon Mein Train", brand_name="@Brand", duration=4.0, music_path=music,
    )
    fc2 = captured_fc[-1]
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in fc2

    # Branch 3: src_info.has_audio=False, no music -> voiceover-only branch still applies alimiter
    monkeypatch.setattr(voiceover, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, False))
    voiceover.render_narrator_bridge(
        wav, src, tmp_path / "b3.mp4", tmp_path,
        badge_text="#1 · SOURAV JOSHI", headline="Pahadon Mein Train", brand_name="@Brand", duration=4.0,
    )
    fc3 = captured_fc[-1]
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in fc3


def test_render_end_tail_full_audio_trim_alimiter_and_outro_ass(tmp_path, monkeypatch):
    src = tmp_path / "tail.mp4"
    src.write_bytes(b"MP4")
    vo = tmp_path / "outro.wav"
    vo.write_bytes(_valid_wav_bytes(2.0))
    music = tmp_path / "music.mp3"
    music.write_bytes(b"MP3")

    monkeypatch.setattr(lf_render, "_find_bright_offset", lambda *a, **k: 2.0)
    captured_fc: list[str] = []

    def fake_ffmpeg(args, **kwargs):
        if "-filter_complex" in args:
            captured_fc.append(args[args.index("-filter_complex") + 1])
        Path(args[-1]).write_bytes(b"X" * 1000)

    monkeypatch.setattr(lf_render, "run_ffmpeg", fake_ffmpeg)

    # Branch 1: live motion >=4s with has_audio=True and outro_wav, Hindi CTA
    monkeypatch.setattr(lf_render, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, True))
    lf_render._render_end_tail(
        src, tmp_path / "tail1.mp4", tmp_path,
        seconds=8.0, outro_wav=vo, brand_name="@Brand", language="hi",
    )
    fc1 = captured_fc[-1]
    assert "[0:a]atrim=0:8.000," in fc1
    assert "[0:a]atrim=0:6.400" not in fc1
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in fc1
    assert "ass=" in fc1
    outro_ass = (tmp_path / "outro.ass").read_text(encoding="utf-8")
    assert "आगे भी देखें" in outro_ass
    assert "Style: OutroBadge," in outro_ass and ",100,100,0,0,3,14,0,8," in outro_ass
    assert "Style: Brand," in outro_ass and ",30,&H38FFFFFF," in outro_ass and ",3,1.5,9," in outro_ass

    # Branch 2: has_music=True
    lf_render._render_end_tail(
        src, tmp_path / "tail2.mp4", tmp_path,
        seconds=8.0, outro_wav=vo, music_path=music, brand_name="@Brand", language="en",
    )
    fc2 = captured_fc[-1]
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in fc2
    outro_ass_en = (tmp_path / "outro.ass").read_text(encoding="utf-8")
    assert "UP NEXT" in outro_ass_en

    # Branch 3: has_audio=False, outro_wav present
    monkeypatch.setattr(lf_render, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, False))
    lf_render._render_end_tail(
        src, tmp_path / "tail3.mp4", tmp_path,
        seconds=8.0, outro_wav=vo, brand_name="@Brand", language="en",
    )
    fc3 = captured_fc[-1]
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in fc3

    # Branch 4: <4s still-frame fallback with outro_wav
    monkeypatch.setattr(lf_render, "probe", lambda p: MediaInfo(2.5, 1920, 1080, 30.0, True))
    lf_render._render_end_tail(
        src, tmp_path / "tail4.mp4", tmp_path,
        seconds=8.0, outro_wav=vo, brand_name="@Brand", language="en",
    )
    fc4 = captured_fc[-1]
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in fc4
    assert "ass=" in fc4


def test_render_segment_uses_display_creator_name_respects_show_pill_and_limits_peak(tmp_path, monkeypatch):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"MP4")
    seg = planner.SegmentPlan(
        str(src), "https://youtu.be/1", "1", "@souravjvlogs", "Sourav Joshi", "Vlog",
        10.0, 40.0, 90.0, "Pahadon Mein Train Cafe", "hi",
    )
    monkeypatch.setattr(lf_render, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(lf_render.timeline, "detect_silences", lambda *a, **k: [])
    monkeypatch.setattr(lf_render.timeline, "audio_keep_segments", lambda a, b, s, **k: [(a, b)])

    captured_fc: list[str] = []

    def fake_ffmpeg(args, **kwargs):
        captured_fc.append(args[args.index("-filter_complex") + 1])
        Path(args[-1]).write_bytes(b"X" * 1000)

    monkeypatch.setattr(lf_render, "run_ffmpeg", fake_ffmpeg)

    # show_pill=True -> uses clean display_creator_name ("Sourav Joshi"), NOT raw "@souravjvlogs"
    lf_render._render_segment(
        seg, tmp_path / "s1.mp4", tmp_path,
        index=1, display_rank=4, is_cold_open=False, format_kind="best_of", brand_name="@Brand", show_pill=True,
    )
    ass1 = (tmp_path / "overlay_1_main.ass").read_text(encoding="utf-8")
    assert "#4 · Sourav Joshi — Pahadon Mein Train Cafe" in ass1
    assert "@souravjvlogs" not in ass1
    assert ",30,&H38FFFFFF," in ass1 and ",3,1.5,9," in ass1
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in captured_fc[-1]

    # show_pill=False -> suppresses redundant top-left Pill, keeps @Brand watermark
    lf_render._render_segment(
        seg, tmp_path / "s2.mp4", tmp_path,
        index=2, display_rank=3, is_cold_open=False, format_kind="best_of", brand_name="@Brand", show_pill=False,
    )
    ass2 = (tmp_path / "overlay_2_main.ass").read_text(encoding="utf-8")
    assert "Pill,,0,0,0" not in ass2
    assert "Brand,,0,0,0,,@Brand" in ass2


def test_render_compilation_suppresses_post_bridge_pill_and_guards_mock_mode(tmp_path, monkeypatch):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"MP4")
    s1 = planner.SegmentPlan(str(src), "https://youtu.be/1", "1", "@souravjvlogs", "Sourav Joshi", "Vid", 5.0, 35.0, 88.0, "Bridge Par Darr", "hi")
    s2 = planner.SegmentPlan(str(src), "https://youtu.be/2", "2", "@souravjvlogs", "Sourav Joshi", "Vid", 40.0, 75.0, 95.0, "Pahadon Mein Train", "hi")
    plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",), theme="",
        title="Sourav Joshi Ke Best Moments", description="Desc", tags=("vlog",), language="hi", target_seconds=180.0,
        cold_open=planner.pick_cold_open([s1, s2], climax=s2), segments=(s1, s2),
    )

    monkeypatch.setattr(lf_render, "probe", lambda p: MediaInfo(100.0, 1920, 1080, 30.0, False))
    monkeypatch.setattr(lf_render, "_probe_duration", lambda p, f: f)
    monkeypatch.setattr(lf_render, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"X" * 2000))

    seen_show_pill: list[bool] = []
    orig_render_seg = lf_render._render_segment

    def spy_render_seg(*args, show_pill=True, **kwargs):
        if not kwargs.get("is_cold_open"):
            seen_show_pill.append(show_pill)
        return orig_render_seg(*args, show_pill=show_pill, **kwargs)

    monkeypatch.setattr(lf_render, "_render_segment", spy_render_seg)

    # 1. With active bridge voiceover, show_pill is False on segments after bridges
    monkeypatch.setattr(voiceover, "generate_commentary_script", lambda p: {"hook": "H", "bridges": ["B1", "B2"], "outro": "O"})
    monkeypatch.setattr(voiceover, "synthesize_wav", lambda text, out, **kw: Path(out).write_bytes(_valid_wav_bytes(1.5)) and 1.5)
    monkeypatch.setattr(voiceover, "render_narrator_bridge", lambda *a, **kw: 2.0)
    monkeypatch.setattr(lf_render, "_render_cold_open_with_hook", lambda *a, **kw: 5.0)
    lf_render.render_compilation(plan, str(tmp_path / "with_vo.mp4"), brand_name="@Brand", include_end_screen=False, include_voiceover=True)
    assert seen_show_pill == [False, False]

    # 2. With include_voiceover=None in mock mode (use_mock=True, sarvam_api_key=""), skips VO instead of raising SarvamTTSQuotaError
    seen_show_pill.clear()
    monkeypatch.setattr(settings, "use_mock", True)
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    monkeypatch.setattr(settings, "longform_voiceover", True)
    lf_render.render_compilation(plan, str(tmp_path / "mock_no_key.mp4"), brand_name="@Brand", include_end_screen=False, include_voiceover=None)
    assert seen_show_pill == [True, True]


def test_cold_open_uncapped_up_to_11_5s_and_bridge_seeks_mid_broll_and_ducks_audio(tmp_path, monkeypatch):
    wav = tmp_path / "vo.wav"
    wav.write_bytes(_valid_wav_bytes(2.0))
    src = tmp_path / "src.mp4"
    src.write_bytes(b"MP4")
    climax = planner.SegmentPlan(str(src), "https://youtu.be/1", "1", "@souravjvlogs", "Sourav Joshi", "Vlog", 10.0, 70.0, 95.0, "Bridge", "hi")
    cold = planner.SegmentPlan(str(src), "https://youtu.be/1", "1", "@souravjvlogs", "Sourav Joshi", "Vlog", 40.0, 45.5, 95.0, "Teaser", "hi")

    monkeypatch.setattr(lf_render, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(lf_render, "_find_bright_offset", lambda p, a, b, need: a)
    monkeypatch.setattr(lf_render, "_probe_duration", lambda p, f: f)

    co_calls: list[list[str]] = []
    monkeypatch.setattr(lf_render, "run_ffmpeg", lambda args, **kw: co_calls.append(list(args)) or Path(args[-1]).write_bytes(b"X" * 1000))
    co_dur = lf_render._render_cold_open_with_hook(cold, climax, wav, 8.4, tmp_path / "co.mp4", tmp_path, format_kind="best_of", brand_name="@Brand")
    assert co_dur >= 9.0
    co_t = co_calls[0][co_calls[0].index("-t") + 1]
    assert float(co_t) >= 9.0
    co_fc = co_calls[0][co_calls[0].index("-filter_complex") + 1]
    assert "volume=0.06,lowpass=f=900" in co_fc

    seeks_seen: list[float] = []
    monkeypatch.setattr(lf_render, "_find_bright_offset", lambda p, a, b, need: seeks_seen.append(a) or a)
    monkeypatch.setattr(voiceover, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, True))
    br_calls: list[list[str]] = []
    monkeypatch.setattr(voiceover, "run_ffmpeg", lambda args, **kw: br_calls.append(list(args)) or Path(args[-1]).write_bytes(b"X" * 1000))
    voiceover.render_narrator_bridge(
        wav, src, tmp_path / "br.mp4", tmp_path,
        badge_text="#1 · SOURAV JOSHI", headline="Khaas Pal", duration=4.5, preview_start=10.0, preview_end=70.0,
    )
    assert seeks_seen and seeks_seen[0] >= 35.0
    br_fc = br_calls[0][br_calls[0].index("-filter_complex") + 1]
    assert "volume=0.06,lowpass=f=900" in br_fc
    assert voiceover._clean_spoken(voiceover._clean_spoken("MrBeast and @rug vs FaZe Rug")) == "Mr. Beast and Faze Rug vs Faze Rug"


def test_longform_invariants_winproc_and_no_idle_numpy():
    proc = subprocess.run(
        [sys.executable, "-c", "import sys, app.main, app.review, app.config, app.db, app.logging_config; assert 'numpy' not in sys.modules"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.slow
@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not on PATH")
def test_real_ffmpeg_bridge_segment_and_outro_peak_and_continuity(tmp_path):
    # Synthesize a hot 0 dBFS audio source (ffmpeg sine defaults to 0.125 = -18 dBFS, so scale 8x)
    # to verify alimiter clamps peaks <= -0.5 dBFS and ambient trim leaves zero dropouts.
    src_mp4 = tmp_path / "hot_src.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=14",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=14",
            "-af", "volume=8.0",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", str(src_mp4),
        ],
        check=True, capture_output=True,
    )
    vo_wav = tmp_path / "hot_vo.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=520:sample_rate=48000:duration=3.5",
            "-af", "volume=8.0", "-ar", "48000", "-ac", "2", str(vo_wav),
        ],
        check=True, capture_output=True,
    )

    bridge_mp4 = tmp_path / "bridge.mp4"
    voiceover.render_narrator_bridge(
        vo_wav, src_mp4, bridge_mp4, tmp_path,
        badge_text="#1 · SOURAV JOSHI", headline="Pahadon Mein Train", brand_name="@Brand", duration=3.5,
    )
    seg = planner.SegmentPlan(str(src_mp4), "https://youtu.be/1", "1", "@souravjvlogs", "Sourav Joshi", "Hot", 1.0, 9.0, 95.0, "Bridge Par Darr", "hi")
    seg_mp4 = tmp_path / "seg.mp4"
    lf_render._render_segment(
        seg, seg_mp4, tmp_path, index=1, display_rank=1, is_cold_open=False,
        format_kind="best_of", brand_name="@Brand", gentle_trim=False, show_pill=False,
    )
    tail_mp4 = tmp_path / "tail.mp4"
    lf_render._render_end_tail(
        src_mp4, tail_mp4, tmp_path, seconds=6.0, outro_wav=vo_wav, seek_sec=1.0, brand_name="@Brand", language="hi",
    )

    for clip in (bridge_mp4, seg_mp4, tail_mp4):
        astats = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(clip), "-af", "astats=metadata=0:reset=0", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        ).stderr
        peaks = [float(line.split("Peak level dB:")[-1].strip()) for line in astats.splitlines() if "Peak level dB:" in line]
        assert peaks, f"no peak telemetry for {clip.name}: {astats}"
        assert max(peaks) <= -0.5, f"{clip.name} clipped/overshot: max peak {max(peaks)} dB"

        sil = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(clip), "-af", "silencedetect=n=-35dB:d=0.35", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        ).stderr
        assert "silence_start" not in sil, f"unexpected silence dropout in {clip.name}: {sil}"
