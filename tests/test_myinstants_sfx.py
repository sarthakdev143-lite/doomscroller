"""Tests for MyInstants SFX fetcher, language-scoped SFX selection, and longform viral polish."""
from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from app.clipper import music, myinstants, render as clipper_render, sfx
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.config import settings
from app.longform import planner, render as lf_render
from scripts import fetch_sfx


def _silent_wav(path: Path, seconds: float = 0.4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sfx.RATE)
        handle.writeframes(b"\x00\x00" * int(sfx.RATE * seconds))


def test_myinstants_idle_import_has_no_numpy():
    proc = subprocess.run(
        [sys.executable, "-c", "import sys, app.clipper.myinstants; assert 'numpy' not in sys.modules"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_myinstants_parse_buttons_and_captcha_detection():
    sample_html = """
    <div class="instants">
      <button class="small-button" onclick="play('/media/sounds/vine-boom.mp3', 'loader-1', 'vine-boom-1')" title="Play Vine Boom sound"></button>
      <button title="Play Emotional &amp; Damage sound" onclick='play("/media/sounds/emotional-damage.mp3", "loader-2")'></button>
      <button class="small-button" onclick="play('/media/sounds/vine-boom.mp3', 'loader-3')" title="Play Duplicate"></button>
      <button class="small-button" onclick="play('/media/sounds/aayein.wav', 'l4', 'aayein-99')"></button>
    </div>
    """
    parsed = myinstants.parse_buttons(sample_html)
    assert parsed == [
        ("Vine Boom", "/media/sounds/vine-boom.mp3"),
        ("Emotional & Damage", "/media/sounds/emotional-damage.mp3"),
        ("aayein", "/media/sounds/aayein.wav"),
    ]

    with pytest.raises(RuntimeError, match="CAPTCHA"):
        myinstants._check_challenge(200, "<html><div class='cf-challenge'>Verify you are human</div></html>")
    with pytest.raises(RuntimeError, match="blocked"):
        myinstants._check_challenge(403, "<html>Cloudflare captcha required</html>")


def test_myinstants_download_duration_bounds_rejects_long_and_short(tmp_path, monkeypatch):
    class _StreamResp:
        def __init__(self, payload: bytes, status_code: int = 200, ctype: str = "audio/mpeg"):
            self.status_code = status_code
            self.headers = {"content-type": ctype}
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield self._payload

        def read(self):
            return self._payload

    class _FakeClient:
        def stream(self, method, url):
            return _StreamResp(b"ID3" + b"\x00" * 1024)

    client = _FakeClient()
    dest = tmp_path / "en"
    sync = tmp_path / "trending"

    seen_stdin = []

    def fake_ffprobe_for(dur: float):
        def _run(cmd, **kw):
            seen_stdin.append(kw.get("stdin"))
            out = json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": str(dur)}})
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return _run

    # Reject > 6.0s copyrighted song
    monkeypatch.setattr(myinstants.subprocess, "run", fake_ffprobe_for(12.5))
    assert myinstants.download_sound("/media/sounds/song.mp3", "Full Song", dest, sync_dir=sync, client=client) is None
    assert not (dest / "full-song.mp3").exists()

    # Reject < 0.15s glitch
    monkeypatch.setattr(myinstants.subprocess, "run", fake_ffprobe_for(0.05))
    assert myinstants.download_sound("/media/sounds/blip.mp3", "Tiny Blip", dest, sync_dir=sync, client=client) is None
    assert not (dest / "tiny-blip.mp3").exists()

    # Accept 1.8s clip and sync to trending
    monkeypatch.setattr(myinstants.subprocess, "run", fake_ffprobe_for(1.8))
    saved = myinstants.download_sound("/media/sounds/vine-boom.mp3", "Vine Boom!", dest, sync_dir=sync, client=client)
    assert saved == dest / "vine-boom.mp3"
    assert saved.is_file()
    assert (sync / "vine-boom.mp3").is_file()
    assert all(s is subprocess.DEVNULL for s in seen_stdin)


def test_fetch_sfx_cli_source_auto_and_lang(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "freesound_api_key", "")
    monkeypatch.setattr(settings, "clip_trending_sfx", True)
    called: list[tuple[str, int, str]] = []

    def fake_myinstants_all(*, limit=10, lang="both", searches=(), pack_root=None, client=None):
        called.append(("myinstants", limit, lang))
        return {"ok": True, "provider": "MyInstants", "lang": lang, "downloaded": 2, "skipped": 0, "files": ["a.mp3", "b.mp3"]}

    monkeypatch.setattr(fetch_sfx.myinstants, "fetch_all", fake_myinstants_all)
    rc = fetch_sfx.main(["--source", "auto", "--lang", "en", "--limit", "5", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "MyInstants"
    assert called == [("myinstants", 5, "en")]

    # Freesound takes priority under auto when FREESOUND_API_KEY is set.
    monkeypatch.setattr(settings, "freesound_api_key", "fs-key")
    monkeypatch.setattr(fetch_sfx, "_fetch_freesound", lambda limit, searches: {"ok": True, "provider": "Freesound", "downloaded": 1, "skipped": 0, "files": ["fs.mp3"]})
    res = fetch_sfx.fetch_packs(3, [])
    assert res["provider"] == "Freesound"


def test_sfx_language_separation_and_render_wiring(tmp_path, monkeypatch):
    pack = tmp_path / "sfx"
    _silent_wav(pack / "en" / "whoosh-en.wav")
    _silent_wav(pack / "en" / "vine-boom.wav")
    _silent_wav(pack / "hi" / "whoosh-hi.wav")
    _silent_wav(pack / "hi" / "aayein.wav")
    _silent_wav(pack / "trending" / "aayein.wav")
    _silent_wav(pack / "trending" / "shared-wow.wav")
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(pack))

    # Default (no language) stays 100% backward-compatible -> trending/ only
    assert set(sfx.trending()) == {"aayein", "shared-wow"}
    # English never leaks hi/
    assert set(sfx.trending(language="en")) == {"vine-boom", "whoosh-en"}
    # Hindi merges hi/ + trending/
    assert set(sfx.trending(language="hi")) == {"aayein", "shared-wow", "whoosh-hi"}

    assert sfx.pack_pick(sfx.WHOOSH, "s1", language="en") == pack / "en" / "whoosh-en.wav"
    assert sfx.pack_pick(sfx.WHOOSH, "s1", language="hi") == pack / "hi" / "whoosh-hi.wav"

    # Empty en/ fallback still excludes hi/ stems
    for p in (pack / "en").iterdir():
        p.unlink()
    assert set(sfx.trending(language="en")) == {"shared-wow"}

    # clipper.render.render_moment forwards language to sfx.build_track
    seen_langs: list[str] = []
    orig_build_track = clipper_render.build_track

    def spy_build_track(path, duration, cues, seed="", *, language=""):
        seen_langs.append(language)
        return orig_build_track(path, duration, cues, seed=seed, language=language)

    src = tmp_path / "src.mp4"
    src.write_bytes(b"MP4")
    monkeypatch.setattr(clipper_render, "build_track", spy_build_track)
    monkeypatch.setattr(clipper_render, "load_sting", lambda p: np.full(int(sfx.RATE * 0.5), 0.5, dtype=np.float64))
    monkeypatch.setattr(clipper_render, "_bright_sharp_opening_offset", lambda *a, **kw: 0.0)
    monkeypatch.setattr(clipper_render, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"X" * 2048))
    monkeypatch.setattr(clipper_render, "probe", lambda p: MediaInfo(10.0, 1080, 1920, 30.0, True))
    monkeypatch.setattr(settings, "clip_sfx", True)

    sig = Signals(10.0, np.zeros(10), np.zeros(10), np.zeros(10), np.full(10, 0.5), np.ones(10), [])
    clipper_render.render_moment(
        str(src), MediaInfo(10.0, 1080, 1920, 30.0, True), sig,
        Moment(0.0, 8.0, kind="visual"), [], str(tmp_path / "out.mp4"),
        captions=False, sting=str(pack / "hi" / "aayein.wav"), language="hi",
    )
    assert seen_langs == ["hi"]


def test_longform_planner_and_render_viral_polish_smoke(tmp_path, monkeypatch):
    assert planner._SEGMENT_MIN_SEC <= planner._SEGMENT_TARGET_SEC <= planner._SEGMENT_MAX_SEC
    assert planner._SEGMENT_TARGET_SEC <= 48
    assert planner._SEGMENT_MAX_SEC <= 70

    # Real music track vs _synth exclusion
    mdir = tmp_path / "music"
    mdir.mkdir()
    monkeypatch.setattr(settings, "clip_music_dir", str(mdir))
    monkeypatch.setattr(settings, "clip_music", True)
    monkeypatch.setattr(settings, "clip_music_synth_fallback", True)
    assert lf_render._pick_real_music("seg-1") is None  # ignores _synth

    real_bed = mdir / "day-off-bed.mp3"
    real_bed.write_bytes(b"ID3" + b"\x00" * 512)
    assert lf_render._pick_real_music("seg-1") == real_bed

    # Kinetic bridge ASS contains scale-in + accent bar
    bridge_ass = tmp_path / "bridge.ass"
    lf_render._write_bridge_ass(bridge_ass, badge_text="#1 · CREATOR", headline="Big Reveal", brand="@Brand")
    ass_txt = bridge_ass.read_text(encoding="utf-8")
    assert r"\fscx114\fscy114\t(0,140,\fscx100\fscy100)" in ass_txt
    assert r"\p1\pos(64,112)" in ass_txt

    # _render_segment mixes entry SFX, ducked lowpass BG music, and subtle motion crop
    src = tmp_path / "vid.mp4"
    src.write_bytes(b"MP4")
    seg = planner.SegmentPlan(
        str(src), "https://youtu.be/1", "v1", "@Creator", "Creator", "Title",
        5.0, 35.0, 90.0, "Crazy Twist", "en",
    )
    monkeypatch.setattr(lf_render, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(lf_render.timeline, "detect_silences", lambda *a, **k: [])
    monkeypatch.setattr(lf_render.timeline, "audio_keep_segments", lambda a, b, s, **k: [(a, b)])
    monkeypatch.setattr(settings, "clip_sfx", True)
    captured_fc: list[str] = []
    monkeypatch.setattr(
        lf_render,
        "run_ffmpeg",
        lambda args, **kw: captured_fc.append(args[args.index("-filter_complex") + 1]) or Path(args[-1]).write_bytes(b"X" * 2048),
    )

    lf_render._render_segment(
        seg, tmp_path / "seg.mp4", tmp_path,
        index=1, display_rank=1, is_cold_open=False, format_kind="best_of", brand_name="@Brand",
    )
    fc = captured_fc[-1]
    assert "scale=1996:1124:force_original_aspect_ratio=increase" in fc
    assert "sin(t*0.18)" in fc
    assert "volume=0.045,lowpass=f=2400" in fc
    assert "amix=inputs=3:" in fc
    assert "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,aformat=channel_layouts=stereo" in fc
