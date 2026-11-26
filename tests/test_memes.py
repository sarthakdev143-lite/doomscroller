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
    # MemeGen needs no key, so the pack is always available behind the flag.
    assert memes.provider() == memes.MEMEGEN
    monkeypatch.setattr(settings, "clip_memes", True)
    assert memes.available() is True


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


def test_filtergraph_overlays_meme_in_the_band_above_a_framed_picture(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    render, plan, info = _landscape_plan()
    assert plan.picture_height
    plan.memes = ((2.0, 1.5, "meme.mp4", "impact"),)
    graph = render.build_filtergraph(plan, info, 0.0)
    assert "[meme0]" in graph
    top = (1920 - plan.picture_height) // 2
    assert f"overlay=x='W-w-40+max(0,(1-(t-2.000)/0.14))*140':y='max(120,{top - 90}-h)'" in graph
    # Composited on the canvas after the backdrop, so it never lands on the speaker's face.
    assert graph.index("[backdrop][framed]overlay") < graph.index("[munder0][meme0]overlay")
    assert "enable='between(t,1.983,3.483)'" in graph  # half-frame adjusted like b-roll


def test_filtergraph_meme_on_a_full_canvas_sits_under_the_brand(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "fill")
    render, plan, info = _landscape_plan()
    plan.memes = ((2.0, 1.5, "meme.mp4", "impact"),)
    graph = render.build_filtergraph(plan, info, 0.0)
    assert "overlay=x='W-w-40+max(0,(1-(t-2.000)/0.14))*140':y=230" in graph


def test_filtergraph_meme_input_index_and_music_shift():
    render, plan, info = _landscape_plan()
    plan.broll = ((1.0, 2.0, "b.mp4", 0.0),)
    plan.memes = ((5.0, 1.5, "meme.mp4", "impact"),)
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


def _speech_plan(monkeypatch, moods=("laugh", "shock", "fail", "awkward", "hype")):
    from app.clipper.edit import EditPlan, Reaction
    from app.clipper.transcribe import Word
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "clip_memes", True)
    words = [Word(f"w{i}", float(i), float(i) + 0.4, 1.0) for i in range(70)]
    marks = [Reaction(t, m) for t, m in zip((5.0, 6.0, 20.0, 40.0, 41.0), moods)]
    return EditPlan(words=words, segments=[(0.0, 70.0)], reactions=marks, speed=1.0)


def _quiet_sounds(monkeypatch):
    """No SFX pack and no probe-able files: every meme falls back to a synthesised hit."""
    from app.clipper import edit as edit_mod
    monkeypatch.setattr(edit_mod.sfx, "mood_sting", lambda *a, **k: None)


def test_attach_memes_no_reactions(monkeypatch):
    from app.clipper.edit import Beat, EditPlan, PUNCH, attach_memes
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "clip_memes", True)
    # Punch beats are emphasis, not jokes: without a marked reaction there is no meme.
    plan = EditPlan(words=[], segments=[(0.0, 40.0)], beats=[Beat(5.0, "", PUNCH)], speed=1.0)
    assert attach_memes(plan) == []
    assert plan.meme_overlays == []


def test_attach_memes_needs_key(monkeypatch):
    from app.clipper import edit as edit_mod
    from app.clipper import memes as memes_mod
    _quiet_sounds(monkeypatch)
    plan = _speech_plan(monkeypatch)
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    monkeypatch.setattr(edit_mod.memes, "fetch",
                        lambda q, s, *, exclude=(), language="": memes_mod.MemeClip(f"m-{q}", "/tmp/m1.jpg", "T", "MemeGen"))
    # Default: static MemeGen overlays are skipped unless clip_memes_allow_stills=True.
    assert edit_mod.attach_memes(plan) == []
    assert plan.meme_overlays == []
    # Explicit opt-in serves MemeGen overlays behind the flag.
    monkeypatch.setattr(settings, "clip_memes_allow_stills", True)
    plan_opt = _speech_plan(monkeypatch)
    assert edit_mod.attach_memes(plan_opt) == []
    assert [o.at for o in plan_opt.meme_overlays] == [5.0, 20.0, 40.0]
    assert all(o.clip.provider == "MemeGen" for o in plan_opt.meme_overlays)
    # Flag off clears.
    plan2 = _speech_plan(monkeypatch)
    monkeypatch.setattr(settings, "clip_memes", False)
    assert edit_mod.attach_memes(plan2) == []
    assert plan2.meme_overlays == []


def test_attach_memes_spaces_caps_and_searches_by_mood(monkeypatch):
    from app.clipper import edit as edit_mod
    from app.clipper import memes as memes_mod
    _quiet_sounds(monkeypatch)
    plan = _speech_plan(monkeypatch)
    seen = []

    def fake_fetch(query, seconds, *, exclude=(), language=""):
        seen.append((query, set(exclude)))
        tag = f"m{len(seen)}"
        return memes_mod.MemeClip(tag, f"/tmp/{tag}.mp4", "T", "Tenor")

    monkeypatch.setattr(edit_mod.memes, "fetch", fake_fetch)
    warnings = edit_mod.attach_memes(plan)
    assert warnings == []
    # 5.0 kept, 6.0 too close, 20.0 kept, 40.0 kept: a 70s clip allows three (one per 20s), 41.0 too close.
    assert [round(o.at, 1) for o in plan.meme_overlays] == [5.0, 20.0, 40.0]
    assert [o.mood for o in plan.meme_overlays] == ["laugh", "fail", "awkward"]
    assert all(o.seconds == 1.5 for o in plan.meme_overlays)
    # Each search is for its reaction's mood, and excludes every meme already placed.
    from app.clipper import reactions
    assert [q for q, _ in seen] == [reactions.QUERIES["laugh"][0], reactions.QUERIES["fail"][1],
                                    reactions.QUERIES["awkward"][2]]
    assert seen[0][1] == set() and "m1" in seen[1][1] and {"m1", "m2"} <= seen[2][1]


def test_a_short_clip_gets_at_most_one_meme(monkeypatch):
    from app.clipper import edit as edit_mod
    from app.clipper import memes as memes_mod
    from app.clipper.edit import EditPlan, Reaction
    from app.clipper.transcribe import Word
    _quiet_sounds(monkeypatch)
    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(settings, "clip_memes", True)
    monkeypatch.setattr(edit_mod.memes, "fetch",
                        lambda q, s, *, exclude=(), language="": memes_mod.MemeClip(f"x{len(exclude)}", "/tmp/x.mp4", "T", "Tenor"))
    plan = EditPlan(words=[Word("a", 0.0, 0.3)], segments=[(0.0, 30.0)], speed=1.0,
                    reactions=[Reaction(4.0, "laugh"), Reaction(16.0, "shock"), Reaction(27.0, "fail")])
    edit_mod.attach_memes(plan)
    assert [o.at for o in plan.meme_overlays] == [4.0]


def _memegen_templates():
    return [
        {"id": "3hd", "name": "Three-Headed Dragon", "keywords": ["King Ghidorah"],
         "blank": "https://api.memegen.link/images/3hd.jpg",
         "source": "https://knowyourmeme.com/memes/three-headed-dragon"},
        {"id": "drake", "name": "Drake Hotline Bling", "keywords": ["drake", "hotline"],
         "blank": "https://api.memegen.link/images/drake.jpg", "source": ""},
    ]


def test_memegen_trending_needs_no_key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    monkeypatch.setattr(memes, "_request", lambda url, params: _memegen_templates())
    found = memes.trending(limit=2)
    assert [c.meme_id for c in found] == ["memegen-3hd", "memegen-drake"]
    assert found[0].link == "https://api.memegen.link/images/3hd.jpg"
    assert found[0].provider == memes.MEMEGEN


def test_memegen_search_matches_keywords(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    monkeypatch.setattr(memes, "_request", lambda url, params: _memegen_templates())
    found = memes.search("ghidorah")
    assert [c.meme_id for c in found] == ["memegen-3hd"]
    assert memes.search("nothing matches this") == []


def test_playable_still_needs_one_frame_but_clip_needs_window(monkeypatch, tmp_path):
    monkeypatch.setattr(memes, "probe", lambda path: None)
    monkeypatch.setattr(memes, "read_ffmpeg", lambda args: b"\x80" * 64)  # exactly one frame
    assert memes._playable(tmp_path / "m.jpg", 1.5) is True
    assert memes._playable(tmp_path / "m.mp4", 1.5) is False
    monkeypatch.setattr(memes, "read_ffmpeg", lambda args: b"\x80" * 64 * 8)
    assert memes._playable(tmp_path / "m.mp4", 1.5) is True


def test_sfx_build_track_synth_fallback_without_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(tmp_path / "missing"))
    out = tmp_path / "sfx.wav"
    sfx.build_track(str(out), 1.0, [sfx.Cue(0.0, sfx.IMPACT)], seed="clip-9")
    assert out.stat().st_size > 1000


def test_fetch_packs_handles_trending_path_and_search_memeclip(tmp_path, monkeypatch):
    from scripts import fetch_memes

    monkeypatch.setattr(settings, "tenor_api_key", "t")
    monkeypatch.setattr(memes, "trending", lambda limit=10: [
        memes.Candidate("tenor-1", "https://x/1.mp4", "One"),
    ])
    monkeypatch.setattr(memes, "_local_copy", lambda c, s: tmp_path / "trend_1.mp4")
    monkeypatch.setattr(memes, "fetch", lambda q, s: memes.MemeClip("tenor-2", str(tmp_path / "search_2.mp4"), "Two", "Tenor"))

    summary = fetch_memes.fetch_packs(limit=1, searches=["wow"])
    assert summary == {
        "ok": True,
        "provider": "Tenor",
        "downloaded": 2,
        "skipped": 0,
        "files": ["search_2.mp4", "trend_1.mp4"],
    }


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


def test_local_meme_pack_language_scoping_and_native_audio_cue(tmp_path, monkeypatch):
    import numpy as np
    from app.clipper import edit as clip_edit, media as clip_media, render as clip_render
    from app.clipper.media import MediaInfo
    from app.clipper.moments import Moment
    from app.clipper.signals import Signals

    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    monkeypatch.setattr(settings, "clip_memes", True)
    monkeypatch.setattr(settings, "clip_memes_allow_stills", False)

    hi_dir = tmp_path / "memes" / "hi"
    hi_dir.mkdir(parents=True)
    hi_mp4 = hi_dir / "shocked_khatam_tata.mp4"
    hi_mp4.write_bytes(b"FAKE_MP4")
    monkeypatch.setattr(memes, "_playable", lambda p, s: True)
    monkeypatch.setattr(memes, "search", lambda q: pytest.fail("an owner file fits, so no library search"))

    assert memes.has_local_pack("hi") is True
    got = memes.fetch("", 1.5, language="hi")
    assert got is not None and got.provider == memes.LOCAL and got.path == str(hi_mp4)

    plan_e = clip_edit.EditPlan(words=[], segments=[(0.0, 10.0)], speed=1.0,
                                reactions=[clip_edit.Reaction(4.0, "shock")])
    # The owner's file has its own audio, so that is its sound.
    monkeypatch.setattr(clip_media, "probe", lambda p: MediaInfo(duration=2.0, width=320, height=240, fps=30.0, has_audio=True))
    clip_edit.attach_memes(plan_e, language="hi")
    assert len(plan_e.meme_overlays) == 1
    assert plan_e.meme_overlays[0].clip.path == str(hi_mp4)
    assert plan_e.meme_overlays[0].sound == str(hi_mp4)

    # render_moment mixes that sound under the voice, cut to the overlay, alongside the kinetic pop-in.
    src_mp4 = tmp_path / "src.mp4"
    src_mp4.write_bytes(b"SRC")
    built_cues = []
    monkeypatch.setattr(clip_render, "probe", lambda p: MediaInfo(duration=10.0, width=1080, height=1920, fps=30.0, has_audio=True))
    monkeypatch.setattr(clip_render, "build_track", lambda path, dur, cues, **kw: built_cues.extend(cues) or Path(path).write_bytes(b"WAV"))
    monkeypatch.setattr(clip_render, "run_ffmpeg", lambda args, cwd=None: Path(args[-1]).write_bytes(b"0" * 2048))
    monkeypatch.setattr(clip_render, "_bright_sharp_opening_offset", lambda *a, **kw: 0.0)

    out_mp4 = tmp_path / "out.mp4"
    sig = Signals(duration=10.0, motion=np.zeros(10), loudness=np.zeros(10),
                  brightness=np.zeros(10), centroid=np.full(10, 0.5),
                  centroid_weight=np.ones(10), cuts=[])
    fg_plan = clip_render.render_moment(
        str(src_mp4),
        MediaInfo(duration=10.0, width=1080, height=1920, fps=30.0, has_audio=True),
        sig,
        Moment(0.0, 10.0, kind="speech"),
        [],
        str(out_mp4),
        edit=plan_e,
        captions=False,
    )
    assert fg_plan.has_sfx is True
    meme_cues = [c for c in built_cues if Path(c.sound) == hi_mp4]
    assert len(meme_cues) == 1 and meme_cues[0].gain < 1.0 and 0 < meme_cues[0].max_sec <= 2.5
    fg = clip_render.build_filtergraph(fg_plan, MediaInfo(duration=10.0, width=1080, height=1920, fps=30.0, has_audio=True), 0.0)
    assert "max(0,(1-(t-" in fg


def test_clean_vlog_edits_get_no_mechanical_memes(tmp_path, monkeypatch):
    """Hindi clean-vlog edits have no marked reactions, so they get no memes (they once got two at 36%/72%)."""
    from app.clipper import edit as clip_edit

    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    monkeypatch.setattr(settings, "clip_memes", True)
    hi_dir = tmp_path / "memes" / "hi"
    hi_dir.mkdir(parents=True)
    for idx in range(2):
        (hi_dir / f"meme_{idx}.mp4").write_bytes(b"MP4")
    monkeypatch.setattr(memes, "_playable", lambda p, s: True)
    plan = clip_edit.EditPlan(words=[], segments=[(5.0, 17.0), (19.0, 31.0)], beats=[], speed=1.0)
    assert clip_edit.attach_memes(plan, language="hi") == []
    assert plan.meme_overlays == []
    assert not hasattr(clip_edit, "clean_vlog_meme_times")


def test_giphy_parses_downsized_small_and_original_mp4_and_playable_loops_short_clips(tmp_path, monkeypatch):
    payload = {
        "data": [
            {
                "id": "g1",
                "title": "Reaction 1",
                "images": {
                    "downsized_small": {"mp4": "https://media.giphy.com/ds.mp4"},
                    "original": {"url": "https://media.giphy.com/ds.gif"},
                },
            },
            {
                "id": "g2",
                "title": "Reaction 2",
                "images": {
                    "original": {"mp4": "https://media.giphy.com/orig.mp4", "url": "https://media.giphy.com/orig.gif"},
                },
            },
        ]
    }
    found = memes._giphy_memes(payload["data"])
    assert [c.link for c in found] == ["https://media.giphy.com/ds.mp4", "https://media.giphy.com/orig.mp4"]

    seen_args: list[list[str]] = []
    monkeypatch.setattr(memes, "probe", lambda path: None)

    def fake_read(args):
        seen_args.append(list(args))
        return b"\x80" * 64 * 8

    monkeypatch.setattr(memes, "read_ffmpeg", fake_read)
    assert memes._playable(tmp_path / "short.mp4", 1.5) is True
    assert seen_args and "-stream_loop" in seen_args[0] and "-1" in seen_args[0]


def test_fetch_memes_limits_errors_oserror_bailout_and_secret_scrubbing(tmp_path, monkeypatch, capsys):
    from scripts import fetch_memes

    canary_proxy = "http://u:CANARY_MEME_W21@10.0.0.1:8080"
    monkeypatch.setattr(settings, "proxy_url", canary_proxy)

    # 1. Limit bounds + bool rejection
    for bad in (0, -1, 51, True, False):
        with pytest.raises(ValueError, match="limit must be 1..50"):
            fetch_memes.fetch_packs(bad, [])

    # 2. memes.trending() raising SearchBlocked/MemeError is scrubbed & wrapped in ValueError
    monkeypatch.setattr(
        memes,
        "trending",
        lambda limit=10: (_ for _ in ()).throw(memes.SearchBlocked(f"blocked {canary_proxy}")),
    )
    rc_err = fetch_memes.main(["--json"])
    assert rc_err == 1
    out_err = capsys.readouterr().out
    assert "CANARY_MEME_W21" not in out_err
    assert "[REDACTED]" in out_err

    # 3. _local_copy raising OSError counts toward MAX_FAILED_FILES & scrubs secret on bailout
    cands = [memes.Candidate(f"m-{i}", f"https://x/{i}.mp4", f"M{i}") for i in range(memes.MAX_FAILED_FILES + 1)]
    monkeypatch.setattr(memes, "trending", lambda limit=10: cands)
    monkeypatch.setattr(
        memes,
        "_local_copy",
        lambda c, s: (_ for _ in ()).throw(OSError(f"disk err {canary_proxy}")),
    )
    with pytest.raises(ValueError) as exc_info:
        fetch_memes.fetch_packs(5, [])
    assert "CANARY_MEME_W21" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)

    # 4. Partial OSError + success resets failed_files; searches MemeError skipped vs SearchBlocked raised; main() exit 0
    monkeypatch.setattr(memes, "trending", lambda limit=10: cands[:3])
    calls = {"n": 0}

    def flaky_local(c, s):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return tmp_path / f"{c.meme_id}.mp4"

    monkeypatch.setattr(memes, "_local_copy", flaky_local)
    monkeypatch.setattr(memes, "fetch", lambda q, s: (_ for _ in ()).throw(memes.MemeError("miss")) if q == "skip" else None)
    assert fetch_memes.main(["--limit", "3", "--search", "skip", "--search", "empty", "--json"]) == 0
    ok_payload = json.loads(capsys.readouterr().out)
    assert ok_payload["downloaded"] == 2
    assert ok_payload["skipped"] == 3

    monkeypatch.setattr(memes, "fetch", lambda q, s: (_ for _ in ()).throw(memes.SearchBlocked(f"429 {canary_proxy}")))
    with pytest.raises(ValueError) as search_exc:
        fetch_memes.fetch_packs(3, ["blocked"])
    assert "CANARY_MEME_W21" not in str(search_exc.value)


def test_wave23_local_pack_ignores_api_cache_and_attach_memes_rotates_reaction_queries(tmp_path, monkeypatch):
    from app.clipper import edit as edit_mod
    from app.clipper import moments
    from app.clipper.transcribe import Word

    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "g")
    monkeypatch.setattr(settings, "clip_memes", True)
    monkeypatch.setattr(settings, "clip_memes_allow_stills", False)
    monkeypatch.setattr(edit_mod.sfx, "mood_sting", lambda *a, **k: None)

    meme_dir = tmp_path / "memes"
    meme_dir.mkdir(parents=True)
    for ignored in ("giphy-abc.mp4", "tenor-def.mp4", "memegen-3hd.jpg", "foo.mp4.part"):
        (meme_dir / ignored).write_bytes(b"FAKE")
    custom = meme_dir / "custom_clip.mp4"
    custom.write_bytes(b"OWNER_MEME")

    for lang in ("", "hi"):
        items = memes.local_pack(lang)
        assert [c.meme_id for c in items] == ["local-custom_clip"]

    # Trending requests full _RESULTS_PER_SEARCH pool and caches it across limit sizes.
    req_params = []
    pool_data = [
        {"id": f"id{i}", "title": f"React {i}", "images": {"original_mp4": {"mp4": f"https://media.giphy.com/{i}.mp4"}}}
        for i in range(6)
    ]

    def fake_request(url, params):
        req_params.append(dict(params))
        return {"data": pool_data}

    monkeypatch.setattr(memes, "_request", fake_request)
    first = memes.trending(limit=2)
    second = memes.trending(limit=5)
    assert len(req_params) == 1
    assert req_params[0]["limit"] == memes._RESULTS_PER_SEARCH
    assert len(first) == 2
    assert len(second) == 5

    # Without local pack files, attach_memes searches by mood and excludes what the job already used.
    custom.unlink()
    assert memes.has_local_pack("") is False
    seen_calls = []

    def fake_fetch(query, seconds, *, exclude=(), language=""):
        seen_calls.append({"query": query, "exclude": set(exclude), "language": language})
        for idx in range(1, 10):
            mid = f"giphy-r{idx}"
            if mid not in exclude:
                return memes.MemeClip(mid, str(tmp_path / f"{mid}.mp4"), f"Title {idx}", memes.GIPHY)
        return None

    monkeypatch.setattr(edit_mod.memes, "fetch", fake_fetch)

    session = memes.MemeSession()
    p1 = edit_mod.EditPlan(words=[], segments=[(0.0, 45.0)],
                           reactions=[edit_mod.Reaction(4.0, "laugh"), edit_mod.Reaction(24.0, "shock")])
    p2 = edit_mod.EditPlan(words=[], segments=[(0.0, 45.0)],
                           reactions=[edit_mod.Reaction(4.0, "laugh"), edit_mod.Reaction(24.0, "shock")])
    assert edit_mod.attach_memes(p1, session, language="en") == []
    assert edit_mod.attach_memes(p2, session, language="en") == []
    ids1 = {ov.clip.meme_id for ov in p1.meme_overlays}
    ids2 = {ov.clip.meme_id for ov in p2.meme_overlays}
    assert len(ids1) == 2 and len(ids2) == 2 and ids1.isdisjoint(ids2)
    assert all(call["query"] != "" for call in seen_calls[:4])
    assert ids1.issubset(seen_calls[2]["exclude"])

    # Hindi speech uses Hindi/Bollywood reaction queries and Hindi SFX variety guidance in prompt.
    hi_plan = edit_mod.EditPlan(words=[], segments=[(0.0, 22.0)], reactions=[edit_mod.Reaction(5.0, "laugh")])
    hi_start = len(seen_calls)
    assert edit_mod.attach_memes(hi_plan, memes.MemeSession(), language="hi") == []
    hi_query = seen_calls[hi_start]["query"].lower()
    assert any(tok in hi_query for tok in ("indian", "bollywood"))

    sents = moments.build_sentences([Word("Bhai", 0.0, 0.4), Word("kya.", 0.4, 0.8)])
    hi_prompt = moments._speech_prompt(sents, 1, "Test", sounds=["aayein-meme", "vine-boom-sound"], language="hi")
    assert "vine-boom-sound" in hi_prompt
    assert "hindi" in hi_prompt.lower()



