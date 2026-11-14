"""Trending meme/GIF pack (Tenor/Giphy) + local SFX pack override.

memes.py is optional at every step like broll.py: no key -> no memes, never a failure.
sfx pack dir is optional like music.py: empty dir -> pure synthesis as before.
"""
from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import pytest

from app.clipper import memes, sfx
from app.config import settings


def _tenor_featured():
    return {"results": [
        {"id": "abc123", "title": "Excited reaction",
         "media_formats": {"mp4": {"url": "https://media.tenor.com/a.mp4"},
                            "gif": {"url": "https://media.tenor.com/a.gif"}}},
        {"id": "def456", "title": "Facepalm",
         "media_formats": {"gif": {"url": "https://media.tenor.com/b.gif"}}},
    ]}


def _giphy_trending():
    return {"data": [
        {"id": "xyz1", "title": "Dancing cat",
         "images": {"original_mp4": {"mp4": "https://media.giphy.com/c.mp4"}}},
        {"id": "xyz2", "title": "",
         "images": {"original": {"url": "https://media.giphy.com/d.gif"}}},
    ]}


def test_provider_none_without_keys(monkeypatch):
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    assert memes.provider() is None
    assert memes.available() is False


def test_provider_prefers_tenor(monkeypatch):
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "giphy_api_key", "g")
    monkeypatch.setattr(settings, "clip_memes", True)
    assert memes.provider() == memes.TENOR
    assert memes.available() is True


def test_provider_giphy_fallback(monkeypatch):
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "g")
    monkeypatch.setattr(settings, "clip_memes", True)
    assert memes.provider() == memes.GIPHY


def test_available_respects_flag(monkeypatch):
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "clip_memes", False)
    assert memes.available() is False


def test_tenor_trending_parses_mp4_first(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    monkeypatch.setattr(memes, "_request", lambda url, params: _tenor_featured())
    found = memes.trending(limit=2)
    assert [c.meme_id for c in found] == ["tenor-abc123", "tenor-def456"]
    assert found[0].link == "https://media.tenor.com/a.mp4"
    assert found[1].link == "https://media.tenor.com/b.gif"
    assert found[0].title == "Excited reaction"


def test_giphy_trending_parses_mp4_first(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "g")
    monkeypatch.setattr(memes, "_request", lambda url, params: _giphy_trending())
    found = memes.trending(limit=2)
    assert [c.meme_id for c in found] == ["giphy-xyz1", "giphy-xyz2"]
    assert found[0].link == "https://media.giphy.com/c.mp4"


def test_search_truncates_query_and_caches(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "giphy_api_key", "")

    def fake_request(url, params):
        calls.append(params)
        return _tenor_featured()

    monkeypatch.setattr(memes, "_request", fake_request)
    first = memes.search("x" * 200)
    second = memes.search("x" * 200)
    assert len(first) == 2 and len(second) == 2
    assert len(calls) == 1  # second hit the 24h cache
    assert len(calls[0]["q"]) <= 100


def test_rate_limited_and_bad_key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "giphy_api_key", "")

    class _Resp:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return {}

    monkeypatch.setattr(memes.httpx, "get", lambda *a, **k: _Resp(429))
    with pytest.raises(memes.RateLimited):
        memes.trending()
    monkeypatch.setattr(memes.httpx, "get", lambda *a, **k: _Resp(401))
    with pytest.raises(memes.BadKey):
        memes.search("x")


def test_safe_meme_id():
    assert memes._safe_meme_id("../../evil") == "evil"
    assert memes._safe_meme_id("") == "meme"


def test_scrub_secrets_redacts_keys(monkeypatch):
    monkeypatch.setattr(settings, "tenor_api_key", "tenor-secret-1")
    monkeypatch.setattr(settings, "giphy_api_key", "giphy-secret-2")
    dirty = "https://tenor.googleapis.com/v2/featured?key=tenor-secret-1&q=x api_key=giphy-secret-2"
    clean = memes.scrub_secrets(dirty)
    assert "tenor-secret-1" not in clean and "giphy-secret-2" not in clean


def test_credit_line():
    assert memes.credit_line(["Excited reaction"], "Tenor") == "Meme from Tenor: Excited reaction"
    assert memes.credit_line([], "Giphy") == "Meme from Giphy."
    assert "a, b" in memes.credit_line(["a", "b"], "Tenor")


def test_fetch_skips_excluded_and_short(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    monkeypatch.setattr(memes, "trending", lambda limit=15: [
        memes.Candidate("tenor-a", "https://x/a.mp4", "A"),
        memes.Candidate("tenor-b", "https://x/b.mp4", "B"),
    ])
    monkeypatch.setattr(memes, "_local_copy", lambda c, s: tmp_path / "m.mp4")
    got = memes.fetch("", 1.5, exclude={"tenor-a"})
    assert got.meme_id == "tenor-b"


# ---------- SFX pack override ----------


def _silent_wav(path: Path, seconds: float = 0.5):
    frames = int(48000 * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * frames)


def test_sfx_pack_pick_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(tmp_path / "empty"))
    assert sfx.pack_pick(sfx.WHOOSH, "seed") is None


def test_sfx_pack_pick_deterministic(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    for name in ("boom.wav", "zap.mp3"):
        (pack / name).write_bytes(b"data")
    (pack / "notes.txt").write_text("not audio")
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(pack))
    first = sfx.pack_pick(sfx.IMPACT, "clip-1")
    assert first is not None and first.suffix in (".wav", ".mp3")
    assert sfx.pack_pick(sfx.IMPACT, "clip-1") == first


def test_sfx_build_track_uses_pack_file(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    whoosh = pack / "whoosh.wav"
    _silent_wav(whoosh, seconds=0.28)
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(pack))
    out = tmp_path / "sfx.wav"
    cues = [sfx.Cue(0.0, sfx.WHOOSH), sfx.Cue(0.5, sfx.POP)]
    sfx.build_track(str(out), 2.0, cues, seed="clip-9")
    with wave.open(str(out), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == sfx.RATE
        assert handle.getnframes() == int(sfx.RATE * 3.0)


# ---------- render overlay graph ----------


def _landscape_plan():
    from app.clipper.media import MediaInfo
    from app.clipper.moments import Moment
    from app.clipper import render
    from app.clipper.signals import Signals
    import numpy as np

    info = MediaInfo(duration=60.0, width=1280, height=720, fps=30.0, has_audio=True)
    sig = Signals(duration=60.0, motion=np.zeros(60), loudness=np.zeros(60),
                  brightness=np.zeros(60), centroid=np.full(60, 0.5),
                  centroid_weight=np.ones(60), cuts=[])
    plan = render.plan_clip(Moment(5.0, 45.0), [], sig, info, has_graphics=False)
    return render, plan, info


def test_filtergraph_overlays_meme_top_right():
    render, plan, info = _landscape_plan()
    plan.memes = ((2.0, 1.5, "meme.mp4"),)
    graph = render.build_filtergraph(plan, info, 0.0)
    assert "[meme0]" in graph
    assert "overlay=x='W-w-30':y=180" in graph
    assert "enable='between(t,1.983,3.483)'" in graph  # half-frame adjusted like b-roll


def test_filtergraph_meme_input_index_and_music_shift():
    render, plan, info = _landscape_plan()
    plan.broll = ((1.0, 2.0, "b.mp4", 0.0),)
    plan.memes = ((5.0, 1.5, "meme.mp4"),)
    plan.music = "m.mp3"
    graph = render.build_filtergraph(plan, info, 0.0)
    # first_input=2 (no sfx): broll=[2:v], meme=[3:v], music=[4:a].
    assert "[3:v]scale=300:-2" in graph
    assert "[4:a]aresample" in graph
    plan.has_sfx = True
    graph = render.build_filtergraph(plan, info, 0.0)
    # first_input=3: broll=[3:v], meme=[4:v], music=[5:a], sfx=[2:a].
    assert "[4:v]scale=300:-2" in graph
    assert "[5:a]aresample" in graph
    assert "[2:a]" in graph


# ---------- edit.attach_memes ----------


def _speech_plan(monkeypatch):
    from app.clipper.edit import Beat, EditPlan, PUNCH
    from app.clipper.transcribe import Word
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "clip_memes", True)
    words = [Word(f"w{i}", float(i), float(i) + 0.4, 1.0) for i in range(40)]
    beats = [Beat(5.0, "", PUNCH), Beat(6.0, "", PUNCH), Beat(12.0, "", PUNCH),
             Beat(20.0, "", PUNCH), Beat(21.0, "", PUNCH)]
    return EditPlan(words=words, segments=[(0.0, 40.0)], beats=beats, speed=1.0)


def test_attach_memes_no_beats(monkeypatch):
    from app.clipper.edit import EditPlan, attach_memes
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "clip_memes", True)
    plan = EditPlan(words=[], segments=[(0.0, 40.0)], speed=1.0)
    assert attach_memes(plan) == []
    assert plan.meme_overlays == []


def test_attach_memes_needs_key(monkeypatch):
    from app.clipper.edit import attach_memes
    plan = _speech_plan(monkeypatch)
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    assert plan.meme_overlays == [] or True
    warnings = attach_memes(plan)
    assert plan.meme_overlays == []
    assert warnings == ["TENOR_API_KEY is not set, so this clip has no meme overlays"]
    monkeypatch.setattr(settings, "clip_memes", False)
    assert attach_memes(plan) == []


def test_attach_memes_spaces_and_caps(monkeypatch):
    from app.clipper import edit as edit_mod
    from app.clipper import memes as memes_mod
    plan = _speech_plan(monkeypatch)
    seen = []

    def fake_fetch(query, seconds, *, exclude=(), choose=None):
        seen.append(set(exclude))
        tag = f"m{len(seen)}"
        return memes_mod.MemeClip(tag, f"/tmp/{tag}.mp4", "T", "Tenor")

    monkeypatch.setattr(edit_mod.memes, "fetch", fake_fetch)
    warnings = edit_mod.attach_memes(plan)
    assert warnings == []
    # 5.0 kept, 6.0 too close, 12.0 kept, 20.0 kept (max 3), 21.0 dropped.
    assert [round(o.at, 1) for o in plan.meme_overlays] == [5.0, 12.0, 20.0]
    assert all(o.seconds == 1.5 for o in plan.meme_overlays)
    # Rotation: each lookup excludes previously used ids.
    assert seen[0] == set() and seen[1] == {"m1"} and seen[2] == {"m1", "m2"}


def test_sfx_build_track_synth_fallback_without_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(tmp_path / "missing"))
    out = tmp_path / "sfx.wav"
    sfx.build_track(str(out), 1.0, [sfx.Cue(0.0, sfx.IMPACT)], seed="clip-9")
    assert out.stat().st_size > 1000


@pytest.mark.slow
def test_slow_meme_overlay_renders_through_real_ffmpeg(tmp_path, monkeypatch):
    """The meme overlay filtergraph must survive ffmpeg's parser, not just string matching."""
    import subprocess

    from app.clipper.edit import EditPlan, MemeOverlay
    from app.clipper.media import probe
    from app.clipper.memes import MemeClip
    from app.clipper.moments import Moment
    from app.clipper.render import render_moment
    from app.clipper.signals import analyse

    monkeypatch.setattr(settings, "clip_memes", True)
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=25:duration=6", "-f", "lavfi",
                    "-i", "sine=frequency=300:duration=6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True, capture_output=True)
    meme = tmp_path / "meme.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=red:s=320x240:r=25:d=2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(meme)], check=True, capture_output=True)
    info = probe(str(src))
    edit = EditPlan(words=[], segments=[(0.0, 5.0)], speed=1.0,
                    meme_overlays=[MemeOverlay(1.0, 1.5, MemeClip("slow-m", str(meme), "T", "Tenor"))])
    out = tmp_path / "clip.mp4"
    plan = render_moment(str(src), info, analyse(str(src), info), Moment(0.0, 5.0, kind="speech"),
                         [], str(out), edit=edit)
    assert [round(s, 1) for s, _, _ in plan.memes] == [1.0]
    assert probe(str(out)).duration == pytest.approx(plan.duration, abs=0.5)
