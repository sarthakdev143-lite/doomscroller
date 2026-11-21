"""Phase 2 RED/GREEN tests: Hindi clean-vlog style, memes/SFX/music polish, loop endings, clean titles, engaged learning & language tags."""
from __future__ import annotations

import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app.clipper import edit as edit_mod
from app.clipper import job, media, moments, music, render, sfx, timeline, transcribe
from app.clipper.signals import Signals
from app.config import settings
from app.growth import autopilot, factors, learn, recipes, titles
from app.metadata import compose, finished_title
from app.publishers import MockPublisher, YouTubePublisher


# ---------- 1. Hindi clean vlog: audio silence trim + tighter picking + recipe + opening line ----------

def test_audio_keep_segments_trims_only_true_silences_with_padding():
    # Silences at [2.0, 4.0] (2.0s -> cut) and [6.0, 6.2] (0.2s -> keep)
    kept = timeline.audio_keep_segments(0.0, 10.0, [(2.0, 4.0), (6.0, 6.2)], pad_sec=0.15, min_cut_sec=0.45)
    assert kept == [(0.0, 2.13), (3.87, 10.0)] or (len(kept) == 2 and kept[0][1] < 2.3 and kept[1][0] > 3.7)
    # Empty silences keeps whole window on grid
    assert timeline.audio_keep_segments(1.0, 12.0, []) == [(1.0, 12.0)]


def test_detect_silences_parses_ffmpeg_stderr_and_fails_safe(monkeypatch):
    import subprocess as sp

    stderr = (
        "[silencedetect @ 0x1] silence_start: 1.50\n"
        "[silencedetect @ 0x1] silence_end: 2.80 | silence_duration: 1.30\n"
        "[silencedetect @ 0x1] silence_start: 5.00\n"
    )
    monkeypatch.setattr(timeline.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a[0], 0, stdout="", stderr=stderr))
    silences = timeline.detect_silences("src.mp4", 10.0, 20.0)
    assert silences == [(11.5, 12.8), (15.0, 20.0)]

    monkeypatch.setattr(timeline.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no ffmpeg")))
    assert timeline.detect_silences("src.mp4", 10.0, 20.0) == []


def test_build_clean_vlog_edit_uses_audio_silences_without_captions(monkeypatch):
    monkeypatch.setattr(edit_mod.timeline, "detect_silences", lambda src, a, b, **k: [(12.0, 14.0)])
    plan = edit_mod.build_clean_vlog_edit(moments.Moment(10.0, 30.0, kind="speech"), "v.mp4", speed=1.08)
    assert plan.words == []
    assert plan.beats == []
    assert plan.speed == pytest.approx(1.08)
    assert len(plan.segments) == 2
    assert edit_mod.timeline.total_duration(plan.segments) < 20.0


def test_hindi_pick_speech_caps_duration_at_tighter_vlog_range(monkeypatch):
    words = [transcribe.Word(f"shabd{i}", i * 0.5, i * 0.5 + 0.4) for i in range(120)]
    words[89] = transcribe.Word("khatam.", 44.5, 44.9)
    sig = Signals(60.0, np.zeros(60), np.zeros(60), np.zeros(60), np.full(60, 0.5), np.ones(60), [])
    monkeypatch.setattr(moments.ai, "ask_json", lambda prompt, **k: {"clips": [{"first": 0, "last": 0, "title": "T", "score": 90}]})
    # Build 40-word sentence spanning 0..20s vs 45s
    picked = moments.pick_speech(words, sig, count=1, min_sec=18, max_sec=35, target_sec=26)
    assert picked and 18 <= picked[0].duration <= 35.5


# ---------- 2. Memes, SFX, Stereo Music Synth Fallback, Loop Endings ----------

def test_attach_memes_skips_memegen_stills_unless_allowed(monkeypatch):
    plan = edit_mod.EditPlan(words=[], segments=[(0.0, 25.0)], beats=[edit_mod.Beat(6.0, "", edit_mod.PUNCH)], speed=1.0)
    monkeypatch.setattr(settings, "clip_memes", True)
    monkeypatch.setattr(settings, "tenor_api_key", "")
    monkeypatch.setattr(settings, "giphy_api_key", "")
    monkeypatch.setattr(settings, "clip_memes_allow_stills", False)
    assert edit_mod.attach_memes(plan) == []
    assert plan.meme_overlays == []


def test_render_meme_args_loop_stills_and_stream_loop_clips(tmp_path, monkeypatch):
    from app.clipper import render as render_mod

    monkeypatch.setattr(settings, "clip_loop_ending", True)
    monkeypatch.setattr(settings, "clip_sfx", False)
    monkeypatch.setattr(settings, "clip_music", False)
    monkeypatch.setattr(settings, "clip_layout", "fill")
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x")
    meme_jpg = tmp_path / "m.jpg"
    meme_jpg.write_bytes(b"jpg")
    meme_mp4 = tmp_path / "m.mp4"
    meme_mp4.write_bytes(b"mp4")
    info = media.MediaInfo(30.0, 1080, 1920, 30.0, True)
    sig = Signals(30.0, np.zeros(30), np.zeros(30), np.zeros(30), np.full(30, 0.5), np.ones(30), [])

    seen_args: list[list[str]] = []

    def fake_run_ffmpeg(args, **_k):
        seen_args.append(list(args))
        Path(args[-1]).write_bytes(b"0" * 2048)

    monkeypatch.setattr(render_mod, "run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(render_mod, "probe", lambda p: media.MediaInfo(20.0, 1080, 1920, 30.0, True))

    edit = edit_mod.EditPlan(
        words=[], segments=[(0.0, 20.0)], speed=1.0,
        meme_overlays=[
            edit_mod.MemeOverlay(3.0, 1.5, edit_mod.memes.MemeClip("j", str(meme_jpg), "J", "MemeGen")),
            edit_mod.MemeOverlay(9.0, 1.5, edit_mod.memes.MemeClip("v", str(meme_mp4), "V", "Tenor")),
        ],
    )
    render_mod.render_moment(str(src), info, sig, moments.Moment(0.0, 20.0, kind="visual"), [], str(tmp_path / "out.mp4"), edit=edit)
    cmd = seen_args[-1]
    assert "-loop" in cmd and "1" in cmd
    assert "-stream_loop" in cmd and "-1" in cmd


def test_sfx_pack_pick_requires_matching_alias(tmp_path, monkeypatch):
    pack = tmp_path / "sfx"
    pack.mkdir()
    (pack / "freesound-99999.mp3").write_bytes(b"data")
    monkeypatch.setattr(settings, "clip_sfx_pack_dir", str(pack))
    assert sfx.pack_pick(sfx.WHOOSH, "seed") is None
    (pack / "whoosh-fast.wav").write_bytes(b"data")
    assert sfx.pack_pick(sfx.WHOOSH, "seed") == pack / "whoosh-fast.wav"


def test_music_pick_falls_back_to_stereo_synth_bed_and_prefers_owner_tracks(tmp_path, monkeypatch):
    mdir = tmp_path / "music"
    mdir.mkdir()
    monkeypatch.setattr(settings, "clip_music_dir", str(mdir))
    monkeypatch.setattr(settings, "clip_music", True)
    monkeypatch.setattr(settings, "clip_music_synth_fallback", True)

    picked = music.pick("clip-abc")
    assert picked is not None and picked.is_file() and "_synth" in str(picked)
    with wave.open(str(picked), "rb") as w:
        assert w.getnchannels() == 2 and w.getframerate() == 48000 and w.getnframes() > 48000 * 5

    owner = mdir / "owner_lofi.mp3"
    owner.write_bytes(b"real-owner-mp3")
    assert music.pick("clip-abc") == owner


def test_loop_ending_skips_end_hold_and_long_fade(monkeypatch):
    from app.clipper import render as render_mod

    monkeypatch.setattr(settings, "clip_loop_ending", True)
    info = media.MediaInfo(30.0, 1080, 1920, 30.0, True)
    sig = Signals(30.0, np.zeros(30), np.zeros(30), np.zeros(30), np.full(30, 0.5), np.ones(30), [])
    plan = render_mod.plan_clip(moments.Moment(5.0, 20.0), [], sig, info, has_graphics=False, end_fade=0.02)
    graph = render_mod.build_filtergraph(plan, info, 0.0)
    assert "d=0.02" in graph and "d=0.35" not in graph


# ---------- 3. Clean titles, Paces, Engaged-Views Reward, Context & YouTube Language ----------

def test_metadata_drops_forced_shorts_from_title_and_keeps_longform_clean():
    assert finished_title("Crazy Stunt") == "Crazy Stunt"
    assert finished_title("Crazy Stunt", shorts_tag=True) == "Crazy Stunt #Shorts"
    md_short = compose(title="Crazy Stunt", description="Watch till end", keywords=["stunt"], is_short=True)
    assert "#Shorts" not in md_short.title
    assert "#Shorts" in md_short.description
    assert "shorts" in md_short.tags and "reels" not in md_short.tags

    md_long = compose(title="Best Moments Part 1", description="Chapters", keywords=["vlog"], is_short=False)
    assert "#Shorts" not in md_long.title
    assert "#Shorts" not in md_long.description
    assert "shorts" not in md_long.tags


def test_factors_paces_tightened_and_dead_sfx_arm_removed():
    assert factors.PACES["tight"][1] <= 24
    assert factors.PACES["normal"][1] <= 32
    assert "sfx" not in factors.CLIP_FACTORS
    assert "sfx" not in factors.RECIPE_FACTORS
    chosen = recipes.choose()
    assert chosen == {} or "pace" in chosen


def test_learn_reward_uses_engaged_rate_when_available():
    low = learn.Result("a", "A", 1000, {}, 0.0, retention=0.6, subscribers=0.0, source_url="", diagnosed=False, engaged_rate=0.30)
    high = learn.Result("b", "B", 1000, {}, 0.0, retention=0.6, subscribers=0.0, source_url="", diagnosed=False, engaged_rate=0.80)
    assert learn.reward(high, 0.6, 0.0, typical_engaged=0.55) > learn.reward(low, 0.6, 0.0, typical_engaged=0.55)


def test_titles_styled_title_includes_context_when_provided(monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(titles.ai, "ask_json", lambda p: prompts.append(p) or {"title": "Who Won?"})
    out, style = titles.styled_title("question", "Challenge", "Watch this", context="MrBeast lost to Speed at 0:25")
    assert style == "question" and out == "Who Won?"
    assert "MrBeast lost to Speed at 0:25" in prompts[0]


def test_youtube_publisher_sends_language_tags(tmp_path, monkeypatch):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    sent = {}

    class Req:
        def next_chunk(self):
            return None, {"id": "yt1"}

    class Videos:
        def insert(self, part, body, media_body):
            sent.update(body)
            return Req()

    class Client:
        def videos(self):
            return Videos()

    pub = YouTubePublisher()
    monkeypatch.setattr(pub, "_client", lambda: Client())
    pub.upload(
        file_path=str(clip), title="Test", description="Desc", tags=["x"],
        category=22, privacy="public", default_language="hi", default_audio_language="hi",
    )
    assert sent["snippet"]["defaultLanguage"] == "hi"
    assert sent["snippet"]["defaultAudioLanguage"] == "hi"


def _mock_clip_pipeline(monkeypatch, tmp_path, *, words, language, engine="deepgram"):
    from app.clipper import ingest
    from app.db import init_db
    from tasks import pipeline as pl

    init_db()
    src_file = tmp_path / f"src_{language}.mp4"
    src_file.write_bytes(b"src")
    info = media.MediaInfo(60.0, 1080, 1920, 30.0, True)
    sig = Signals(60.0, np.zeros(60), np.zeros(60), np.zeros(60), np.full(60, 0.5), np.ones(60), [])

    monkeypatch.setattr(job, "require_tools", lambda: None)
    monkeypatch.setattr(job, "probe", lambda p: info)
    monkeypatch.setattr(job, "analyse", lambda p, i: sig)
    monkeypatch.setattr(job, "transcribe", lambda p, i: transcribe.Transcript(words=words, language=language, duration=60.0, engine=engine))
    monkeypatch.setattr(job, "_earlier_clips", lambda s: [])
    monkeypatch.setattr(job, "music", type("M", (), {"pick": staticmethod(lambda *a, **k: None), "credit": staticmethod(lambda *a: "")}))
    monkeypatch.setattr(pl, "generate_metadata_for_video", lambda vid: None)
    monkeypatch.setattr(job, "_store_audio_qa", lambda vid, p: None)
    monkeypatch.setattr(job.recipes, "choose", lambda *a, **k: {"pace": "normal", "broll": "on"})
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    return ingest.SourceVideo(path=str(src_file), title="Vlog test", source="local", source_id=f"id_{language}")


def test_english_clips_receive_trending_sfx_sounds(tmp_path, monkeypatch):
    words = [transcribe.Word(f"word{i}", i * 0.4, i * 0.4 + 0.3) for i in range(60)]
    words[-1] = transcribe.Word("end.", 23.6, 23.9)
    src = _mock_clip_pipeline(monkeypatch, tmp_path, words=words, language="en", engine="deepgram")
    sting = tmp_path / "vine-boom.mp3"
    sting.write_bytes(b"mp3")
    monkeypatch.setattr(job.sfx, "trending", lambda: {"vine-boom": sting})
    asked = {}

    def fake_pick(*args, sounds=(), **kwargs):
        asked["sounds"] = sounds
        return [moments.Moment(0.0, 23.9, title="Shocking", hook="Wait", ai_title=True, kind="speech", sound="vine-boom")]

    seen = {}
    monkeypatch.setattr(job, "pick_speech", fake_pick)
    monkeypatch.setattr(job, "build_edit", lambda m, w, title="": edit_mod.EditPlan(words=list(w), segments=[(0.0, 23.9)], speed=1.0))

    def fake_render(src_p, info, sig, moment, w, out_p, **kw):
        seen["sting"] = kw.get("sting")
        Path(out_p).parent.mkdir(parents=True, exist_ok=True)
        Path(out_p).write_bytes(b"rendered")
        return type("P", (), {"duration": 23.9, "has_sfx": True, "tail": True, "music": False})()

    monkeypatch.setattr(job, "render_moment", fake_render)
    out = job.clip_source(src)
    assert out["ok"] is True
    assert asked["sounds"] == ("vine-boom",)
    assert seen["sting"] == str(sting)


def test_hindi_clean_vlog_saves_opening_line_to_video_meta(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import Video

    words = [
        transcribe.Word("Aaj", 0.0, 0.3),
        transcribe.Word("crazy", 0.35, 0.7),
        transcribe.Word("trip.", 0.75, 1.1),
        *[transcribe.Word(f"Baat{i}", 1.2 + i * 0.4, 1.5 + i * 0.4) for i in range(45)],
    ]
    src = _mock_clip_pipeline(monkeypatch, tmp_path, words=words, language="hi", engine="sarvam")
    monkeypatch.setattr(job.sfx, "trending", lambda: {})
    monkeypatch.setattr(
        job,
        "pick_speech",
        lambda *a, **k: [moments.Moment(0.0, words[-1].end, title="Hindi Vlog", hook="Dekho", ai_title=True, kind="speech")],
    )
    monkeypatch.setattr(edit_mod.timeline, "detect_silences", lambda *a, **k: [])

    def fake_render(src_p, info, sig, moment, w, out_p, **kw):
        Path(out_p).parent.mkdir(parents=True, exist_ok=True)
        Path(out_p).write_bytes(b"rendered")
        return type("P", (), {"duration": 19.0, "has_sfx": False, "tail": False, "music": False})()

    monkeypatch.setattr(job, "render_moment", fake_render)
    out = job.clip_source(src)
    assert out["ok"] is True and out["clips"]
    with session_scope() as db:
        row = db.get(Video, out["clips"][0]["id"])
        assert row is not None
        assert row.meta.get("clip_opening_line") == "Aaj crazy trip."
        assert row.meta.get("growth_recipe", {}).get("broll") == "off"
