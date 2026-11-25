"""Milestone M2 tests: Shorts duration/quality gates, Hinglish hooks, opening frame skip,
framed backdrop, clean_vlog visual energy/captions, language lock, upload alignment, and titles.
"""
from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from app.clipper import ai, captions, edit, job, moments, render, sfx, transcribe
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.clipper.transcribe import Word
from app.config import settings
from app.db import init_db, session_scope
from app.growth import titles
from app.metadata import (
    _SHORTS_TITLE_MAX,
    _TITLE_MAX,
    compose,
    detect_language,
    sanitize_shorts_title,
    youtube_title,
)
from app.models import Channel, MetadataDraft, Video, VideoStatus
from app.publishers.base import PublishResult
from tasks import pipeline


def _sig(duration: float = 120.0, *, cuts: list[float] | None = None, loud_peaks: list[int] | None = None) -> Signals:
    n = max(1, int(math.ceil(duration)))
    loud = np.full(n, -28.0)
    for sec in (loud_peaks or ()):
        if 0 <= sec < n:
            loud[sec] = -6.0
    return Signals(
        duration=duration,
        motion=np.full(n, 0.3),
        loudness=loud,
        brightness=np.full(n, 0.5),
        centroid=np.full(n, 0.5),
        centroid_weight=np.ones(n),
        cuts=list(cuts or []),
    )


def _words_range(count: int, step: float = 0.5, suffix: str = ".") -> list[Word]:
    out: list[Word] = []
    for i in range(count):
        text = f"Shabd{i}{suffix}" if (i + 1) % 6 == 0 else f"Shabd{i}"
        out.append(Word(text, round(i * step, 2), round(i * step + step * 0.8, 2), 0.95))
    return out


# ---------- 1. Duration cap, quality gate, Hinglish prompt & payoff trimming ----------


def test_pick_speech_rejects_below_min_score_without_rule_fallback(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(
        ai,
        "ask_json",
        lambda *a, **kw: {
            "clips": [
                {"first": 0, "last": 3, "title": "Weak moment", "hook": "Dekho", "score": 68},
            ]
        },
    )
    words = _words_range(80)
    picked = moments.pick_speech(
        words, _sig(60.0), count=1, min_sec=15, max_sec=30, target_sec=22, min_score=75.0, language="hi"
    )
    assert picked == []


def test_pick_speech_prompt_enforces_latin_hinglish_and_captures_reveal(monkeypatch):
    seen_prompts: list[str] = []
    monkeypatch.setattr(ai, "available", lambda: True)

    def fake_ask(prompt, **kw):
        seen_prompts.append(prompt)
        return {
            "clips": [
                {
                    "first": 0,
                    "last": 5,
                    "title": "Bhai ne Coffee Me kya Daal Diya",
                    "hook": "YEH KYA HUA",
                    "reveal": "Last reaction dekho",
                    "description": "End me sab shock ho gaye.",
                    "score": 88,
                }
            ]
        }

    monkeypatch.setattr(ai, "ask_json", fake_ask)
    words = [Word("अरे" if i == 0 else f"Word{i}." if i % 6 == 5 else f"Word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(80)]
    picked = moments.pick_speech(
        words, _sig(60.0), count=1, min_sec=15, max_sec=30, target_sec=22, min_score=75.0, language="hi"
    )
    assert len(picked) == 1
    assert picked[0].reveal == "Last reaction dekho"
    assert picked[0].duration <= 30.0 + moments._EDGE_PADDING_SEC
    assert "NEVER Devanagari" in seen_prompts[0]
    assert "<= 70 chars" in seen_prompts[0]


def test_fit_range_trims_from_start_to_preserve_ending_payoff():
    words = _words_range(100, step=0.5)
    sentences = moments.build_sentences(words)
    fitted = moments._fit_range(0, len(sentences) - 1, sentences, words, 60.0, min_sec=15, max_sec=25)
    assert fitted is not None
    first, last = fitted
    assert first > 0
    assert last == len(sentences) - 1


def test_pick_visual_enforces_min_score_and_max_sec(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: True)
    info = MediaInfo(duration=120.0, width=1920, height=1080, fps=30.0, has_audio=True)

    monkeypatch.setattr(
        moments,
        "_ai_rank_visual",
        lambda *a, **kw: {"clips": [{"candidate": 1, "title": "Low Clip", "hook": "Meh", "score": 60}]},
    )
    assert moments.pick_visual("v.mp4", info, _sig(120.0), count=1, min_score=75.0, max_sec=30) == []

    monkeypatch.setattr(
        moments,
        "_ai_rank_visual",
        lambda *a, **kw: {"clips": [{"candidate": 1, "title": "Good Clip", "hook": "Look", "score": 85}]},
    )
    picked = moments.pick_visual("v.mp4", info, _sig(120.0, cuts=[10.0, 42.0]), count=1, min_score=75.0, max_sec=30)
    assert len(picked) == 1
    assert picked[0].duration <= 30.0


def test_hindi_clean_vlog_skips_visual_fallback_and_passes_min_score(tmp_path, monkeypatch):
    init_db()
    src_mp4 = tmp_path / "hi.mp4"
    src_mp4.write_bytes(b"mp4")
    words = [Word("Oye!", 0.0, 0.3), Word("Aaj", 0.4, 0.7), Word("crazy", 0.8, 1.1), Word("twist.", 1.2, 1.6)]
    words += [Word(f"baat{i}", 2.0 + i * 0.4, 2.3 + i * 0.4) for i in range(90)]

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(job, "require_tools", lambda: None)
    monkeypatch.setattr(job, "probe", lambda p: MediaInfo(60.0, 1280, 720, 30.0, True))
    monkeypatch.setattr(job, "analyse", lambda p, i: _sig(60.0))
    monkeypatch.setattr(
        job,
        "transcribe",
        lambda p, i: transcribe.Transcript(words=words, language="hi", duration=60.0, engine="sarvam"),
    )
    monkeypatch.setattr(job.subtitles, "already_captioned", lambda p, i: False)
    monkeypatch.setattr(job, "_earlier_clips", lambda s: [])
    seen: dict = {}

    def fake_pick_speech(*a, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(job, "pick_speech", fake_pick_speech)
    monkeypatch.setattr(job, "pick_visual", lambda *a, **kw: pytest.fail("clean_vlog must not fall back to visual"))
    from app.clipper import ingest

    res = job.clip_source(ingest.SourceVideo(path=str(src_mp4), title="Hindi Vlog", source="local", source_id="hi_no_vis"))
    assert res["ok"] is False
    assert seen["min_score"] == 75.0
    assert seen["language"] == "hi"
    assert seen["max_sec"] <= 30


# ---------- 2. Dark/blurry opening skip, framed backdrop & clean_vlog energy/captions ----------


def test_bright_sharp_opening_offset_skips_dark_or_blurry_intro(tmp_path, monkeypatch):
    fake_src = tmp_path / "src.mp4"
    fake_src.write_bytes(b"0" * 20_000)

    def _frame(luma: int, stripe: int) -> np.ndarray:
        arr = np.full((90, 160), luma, dtype=np.uint8)
        if stripe:
            arr[:, ::2] = np.clip(luma + stripe, 0, 255)
        return arr

    # 1. Dark frames [0.0, 0.25, 0.50] followed by bright sharp frame at 0.75s -> offset 0.75
    seq_dark = np.stack([_frame(10, 0), _frame(15, 0), _frame(20, 0), _frame(110, 45), _frame(115, 45), _frame(115, 45)])
    monkeypatch.setattr(render, "read_ffmpeg", lambda *a, **kw: seq_dark.tobytes())
    assert render._bright_sharp_opening_offset(str(fake_src), 0.0, 20.0) == pytest.approx(0.75)

    # 2. Blurry/flat bright frames [0.0, 0.25, 0.50] followed by sharp frame at 0.75s -> offset 0.75
    seq_blur = np.stack([_frame(90, 0), _frame(90, 2), _frame(90, 2), _frame(95, 50), _frame(95, 50), _frame(95, 50)])
    monkeypatch.setattr(render, "read_ffmpeg", lambda *a, **kw: seq_blur.tobytes())
    assert render._bright_sharp_opening_offset(str(fake_src), 0.0, 20.0) == pytest.approx(0.75)

    # 3. Already bright & sharp at frame 0 -> 0.0
    seq_good = np.stack([_frame(110, 45), _frame(112, 45), _frame(115, 45), _frame(115, 45)])
    monkeypatch.setattr(render, "read_ffmpeg", lambda *a, **kw: seq_good.tobytes())
    assert render._bright_sharp_opening_offset(str(fake_src), 0.0, 20.0) == 0.0

    # 4. Uniform pure-black clip -> 0.0
    seq_black = np.zeros((4, 90, 160), dtype=np.uint8)
    monkeypatch.setattr(render, "read_ffmpeg", lambda *a, **kw: seq_black.tobytes())
    assert render._bright_sharp_opening_offset(str(fake_src), 0.0, 20.0) == 0.0


def test_framed_backdrop_constants_and_filtergraph(monkeypatch):
    monkeypatch.setattr(settings, "clip_layout", "framed")
    monkeypatch.setattr(settings, "clip_backdrop_blur", 14)
    assert render._BACKDROP_KEEP == pytest.approx(0.80)
    assert render._BACKDROP_TOP == pytest.approx(0.04)
    assert render._FRAMED_ZOOM_SHARE == pytest.approx(0.65)
    info = MediaInfo(duration=30.0, width=1280, height=720, fps=30.0, has_audio=True)
    plan = render.plan_clip(Moment(5.0, 25.0, kind="speech"), [], _sig(30.0), info, has_graphics=True)
    graph = render.build_filtergraph(plan, info, 0.0)
    assert "ih*0.8" in graph and "ih*0.04" in graph
    assert "brightness=-0.42" in graph and "boxblur=14:1" in graph


def test_clean_vlog_rhythm_points_summary_cues_and_first_sentence(monkeypatch):
    plan = edit.EditPlan(words=[], segments=[(0.0, 22.0)], beats=[], speed=1.08)
    sig = _sig(30.0, cuts=[3.2, 8.5, 14.0], loud_peaks=[5, 11, 17])
    pts = render.rhythm_points(plan, signals=sig, moment=Moment(0.0, 22.0, kind="speech"))
    assert len(pts) >= 7
    assert any(abs(p - 3.2 / 1.08) < 0.05 for p in pts)

    ass = captions.build_ass(
        [],
        "YEH KYA HUA",
        20.0,
        "Arial",
        summary_cues=[(2.6, 10.5, "Bhai ne sabko shock diya"), (10.5, 19.8, "End me twist nikla")],
    )
    caption_events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,") and ",Caption,," in ln]
    assert len(caption_events) == 2
    assert "BHAI NE SABKO" in caption_events[0]
    assert "END ME" in caption_events[1] and "TWIST NIKLA" in caption_events[1]

    # Tiny <10-char leading exclamation joins with next sentence; >=10-char stays standalone.
    short_words = [
        Word("Oye!", 0.0, 0.3),
        Word("Aaj", 0.5, 0.8),
        Word("crazy", 0.9, 1.2),
        Word("trip", 1.3, 1.6),
        Word("hai.", 1.7, 2.0),
    ]
    assert edit.first_sentence_text(Moment(0.0, 5.0), short_words) == "Oye! Aaj crazy trip hai."
    normal_words = [Word("Aaj", 0.0, 0.3), Word("crazy", 0.4, 0.7), Word("trip.", 0.8, 1.1), Word("Next.", 1.2, 1.5)]
    assert edit.first_sentence_text(Moment(0.0, 5.0), normal_words) == "Aaj crazy trip."


# ---------- 3. Language lock, upload language alignment & title filtering ----------


def test_detect_language_handles_hinglish_and_devanagari_without_id_tl_misclassification():
    assert detect_language("Car Ban Gayi Patakha Bar, Diwali Dhamaka") == "hi"
    assert detect_language("Bike Ke Aage Kutta Aa Gaya Scene Ho Gaya") == "hi"
    assert detect_language("Gaiyon Ko Khapli Atta Khilaya, Line Lag Gayi") == "hi"
    assert detect_language("Chhoti Diwali Pe Patakhe Aur Healthy Laddu") == "hi"
    assert detect_language("रात को रोने की आवाज़ आई") == "hi"
    assert detect_language("Pointing Out the Cliffs From the Boat") == "en"


def test_channel_language_lock_blocks_english_visual_clips_on_indianvlogs(monkeypatch):
    init_db()
    monkeypatch.setattr(settings, "target_languages", '["en", "hi"]')
    monkeypatch.setattr(settings, "allow_unknown_language", True)

    with session_scope() as db:
        ch = Channel(
            slug="indianvlogs_lock",
            name="Indian Vlogs",
            niche="hindi vlog",
            meta={},
        )
        db.add(ch)
        db.flush()

        bad_en_visual = Video(
            source="youtube",
            source_id="en_vis_1",
            source_url="u",
            title="Checking Phones on the Boat",
            language="",
            channel_id=ch.id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "visual", "clip_hook": "Look at this view!"},
        )
        bad_en_explicit = Video(
            source="youtube",
            source_id="en_exp_1",
            source_url="u",
            title="English Title",
            language="en",
            channel_id=ch.id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech"},
        )
        good_hinglish = Video(
            source="youtube",
            source_id="hi_ok_1",
            source_url="u",
            title="Diwali Dhamaka",
            language="",
            channel_id=ch.id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_opening_line": "Aaj crazy trip hai."},
        )
        legacy_visual = Video(
            source="youtube",
            source_id="legacy_vis_1",
            source_url="u",
            title="Clip 0",
            language="",
            channel_id=None,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "visual"},
        )
        db.add_all([bad_en_visual, bad_en_explicit, good_hinglish, legacy_visual])
        db.flush()

        assert pipeline.clip_gate_reason(bad_en_visual) == "language: en"
        assert pipeline.clip_gate_reason(bad_en_explicit) == "language: en"
        assert pipeline.clip_gate_reason(good_hinglish) is None
        assert pipeline.clip_gate_reason(legacy_visual) is None

    res = pipeline.apply_queue_filters()
    assert res["rejected"] == 2


def test_publish_task_separates_title_and_audio_languages(tmp_path, monkeypatch):
    init_db()
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "youtube_auto_comment", True)
    monkeypatch.setattr(settings, "use_mock", False)

    clip_path = tmp_path / "c.mp4"
    clip_path.write_bytes(b"mp4")

    uploads: list[dict] = []
    comments: list[tuple[str, str]] = []

    class FakePub:
        def upload(self, **kw):
            uploads.append(kw)
            return PublishResult(video_id=f"YT_{len(uploads)}", status="uploaded", raw={})

        def post_comment(self, vid, text):
            comments.append((vid, text))
            return "CMT_1"

    monkeypatch.setattr(pipeline, "_publisher_for", lambda cid: FakePub())
    monkeypatch.setattr(titles, "engagement_comment", lambda t, **kw: comments.append(("LANG", kw["language"])) or "Kaisa laga?")

    with session_scope() as db:
        ch_hi = Channel(slug="indianvlogs_pub", name="Indian Vlogs", niche="hindi vlog", meta={})
        db.add(ch_hi)
        db.flush()

        v_silent = Video(
            source="youtube",
            source_id="silent_vis",
            source_url="u",
            title="Diwali Ghar Decoration",
            language="",
            transformed_path=str(clip_path),
            status=VideoStatus.TRANSFORMED,
            channel_id=ch_hi.id,
            meta={"clip_rendered": True, "clip_kind": "visual"},
        )
        v_speech = Video(
            source="youtube",
            source_id="hi_speech",
            source_url="u",
            title="Piyush Shocked",
            language="hi",
            transformed_path=str(clip_path),
            status=VideoStatus.TRANSFORMED,
            channel_id=ch_hi.id,
            meta={"clip_rendered": True, "clip_kind": "speech"},
        )
        db.add_all([v_silent, v_speech])
        db.flush()
        db.add(MetadataDraft(video_id=v_silent.id, title="Diwali Ghar Decoration", description="D", tags=["vlog"], detected_language="en"))
        db.add(MetadataDraft(video_id=v_speech.id, title="Aaj hadh ho gayi", description="D", tags=["vlog"], detected_language="hi"))
        id_silent, id_speech = v_silent.id, v_speech.id

    assert pipeline.publish_task(id_silent)["ok"] is True
    assert uploads[0]["default_language"] == "hi"
    assert "default_audio_language" not in uploads[0]

    assert pipeline.publish_task(id_speech)["ok"] is True
    assert uploads[1]["default_language"] == "hi"
    assert uploads[1]["default_audio_language"] == "hi"
    assert ("LANG", "hi") in comments


def test_titles_explore_styles_cap_70_chars_and_filter_sensitive_words(monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: False)
    assert titles.available_styles() == ("plain", "quote")

    monkeypatch.setattr(ai, "available", lambda: True)
    assert titles.available_styles() == ("question", "curiosity", "quote")
    assert titles.available_styles(has_opening_line=False) == ("question", "curiosity")
    assert "plain" not in titles.available_styles()

    # Devanagari and sensitive words rejected from AI output; fallback is <= 70 chars & sanitized.
    assert titles._sanitize_ai_title("रात को भूत दिखा") is None
    assert titles._sanitize_ai_title("Bike Accident Ho Gaya") is None
    assert titles._sanitize_ai_title("Kachche Me Chori") is None
    assert titles._sanitize_ai_title("x" * 71) is None

    monkeypatch.setattr(ai, "ask_json", lambda *a, **kw: {"title": "Bhoot Mil Gaya Raat Ko"})
    out_t, out_s = titles.styled_title("question", "Kachche aur Bhoot ka Accident", "line")
    assert out_s == "plain"
    assert "bhoot" not in out_t.lower()
    assert "kachche" not in out_t.lower()
    assert "accident" not in out_t.lower()
    assert len(out_t) <= _SHORTS_TITLE_MAX

    # compose() caps Shorts at 70 chars while keeping long-form / youtube_title at 100 chars.
    short_md = compose(title=" ".join(["word"] * 25), description="", keywords=[], is_short=True)
    long_md = compose(title=" ".join(["word"] * 25), description="", keywords=[], is_short=False)
    assert len(short_md.title) <= _SHORTS_TITLE_MAX
    assert _SHORTS_TITLE_MAX < len(long_md.title) <= _TITLE_MAX
    assert len(youtube_title("x" * 150)) == _TITLE_MAX
    assert sanitize_shorts_title("भूत और Accident") == "राज़ और scene"


# ---------- 4. Iteration 2 regression tests ----------


def test_beat_trim_drops_out_of_range_beats_and_sting_caps_budget_at_30s(tmp_path, monkeypatch):
    # 1. captions._beat_events and render_moment tolerate None/out-of-range beats cleanly.
    b_in = edit.Beat(at=5.0, text="10%", style="stat")
    b_out = edit.Beat(at=40.0, text="50%", style="stat")
    ed = edit.EditPlan(words=[], segments=[(0.0, 31.5)], beats=[b_in, b_out])
    ass = captions.build_ass(
        [], "HOOK", 31.5, "Arial",
        beats=[(None, "50%", "stat", "", 1.6), (ed.to_output(5.0), "10%", "stat", "", 1.6)],
    )
    assert "50%" not in ass and "10%" in ass

    # 2. render_moment filters out beats where edit.to_output(b.at) is None.
    src_mp4 = tmp_path / "src.mp4"
    src_mp4.write_bytes(b"0" * 20_000)
    seen_beats: list[list] = []
    monkeypatch.setattr(render, "_bright_sharp_opening_offset", lambda *a, **kw: 0.0)
    monkeypatch.setattr(render, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"0" * 2048))
    monkeypatch.setattr(render, "probe", lambda p: MediaInfo(31.5, 1080, 1920, 30.0, True))
    orig_build_ass = render.build_ass

    def spy_ass(words_arg, hook, dur, font, font_path=None, beats=(), **kw):
        seen_beats.append(list(beats))
        return orig_build_ass(words_arg, hook, dur, font, font_path, beats=beats, **kw)

    monkeypatch.setattr(render, "build_ass", spy_ass)
    render.render_moment(
        str(src_mp4),
        MediaInfo(60.0, 1920, 1080, 30.0, True),
        _sig(60.0),
        Moment(0.0, 45.0, kind="speech", hook="HOOK"),
        [],
        str(tmp_path / "out_beat.mp4"),
        edit=ed,
        captions=False,
    )
    assert len(seen_beats) == 1 and len(seen_beats[0]) == 1 and seen_beats[0][0][1] == "10%"

    # 3. job.clip_source prunes beats past trimmed tail and budgets real 2.5s WAV sting within 32.0s.
    init_db()
    sting_wav = tmp_path / "boom.wav"
    n_samples = int(sfx.RATE * (sfx._STING_MAX_SEC + 0.5))
    with wave.open(str(sting_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sfx.RATE)
        wf.writeframes(struct.pack("<" + "h" * n_samples, *([8000] * n_samples)))

    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(job, "require_tools", lambda: None)
    monkeypatch.setattr(job, "probe", lambda p: MediaInfo(60.0, 1280, 720, 30.0, True))
    monkeypatch.setattr(job, "analyse", lambda p, i: _sig(60.0))
    words = _words_range(90, step=0.5)
    monkeypatch.setattr(job, "transcribe", lambda p, i: transcribe.Transcript(words=words, language="en", duration=60.0))
    monkeypatch.setattr(job.subtitles, "already_captioned", lambda p, i: False)
    monkeypatch.setattr(job, "_earlier_clips", lambda s: [])
    monkeypatch.setattr(job, "pick_speech", lambda *a, **kw: [Moment(0.0, 45.0, kind="speech", title="T", hook="H", sound="vine-boom", score=90.0)])
    monkeypatch.setattr(job.sfx, "trending", lambda: {"vine-boom": sting_wav})
    monkeypatch.setattr(job, "attach_stock", lambda *a, **kw: [])
    monkeypatch.setattr(job, "attach_memes", lambda *a, **kw: [])
    monkeypatch.setattr(job.music, "pick", lambda *a: None)
    monkeypatch.setattr(job, "_store_audio_qa", lambda *a: None)
    monkeypatch.setattr(pipeline, "generate_metadata_for_video", lambda *a: None)
    monkeypatch.setattr(settings, "clip_sfx", True)

    long_edit = edit.EditPlan(words=words, segments=[(0.0, 40.0)], beats=[b_in, b_out], speed=1.0)
    monkeypatch.setattr(job, "build_edit", lambda *a, **kw: long_edit)
    captured_edit: list[edit.EditPlan] = []
    captured_plan: list[render.ClipPlan] = []
    real_render = render.render_moment
    monkeypatch.setattr(render, "probe", lambda p: MediaInfo(32.0, 1080, 1920, 30.0, True))

    def spy_render(src, info, signals, moment, w, out_p, edit=None, **kw):
        captured_edit.append(edit)
        plan = real_render(src, info, signals, moment, w, out_p, edit=edit, **kw)
        captured_plan.append(plan)
        return plan

    monkeypatch.setattr(job, "render_moment", spy_render)
    from app.clipper import ingest

    res = job.clip_source(ingest.SourceVideo(path=str(src_mp4), title="Test", source="local", source_id="beat_sting"))
    assert res["ok"] is True
    assert captured_edit[0].duration <= 28.65
    assert [b.at for b in captured_edit[0].beats] == [5.0]
    assert captured_plan[0].tail == pytest.approx(sfx._STING_MAX_SEC + render._STING_PAD_SEC, abs=0.02)
    assert captured_plan[0].duration <= 32.0
    assert res["clips"][0]["duration"] <= 32.0


def test_opening_skip_handles_mid_video_segment_and_keeps_words_and_beats_synced(tmp_path, monkeypatch):
    src_mp4 = tmp_path / "mid.mp4"
    src_mp4.write_bytes(b"0" * 20_000)

    seen_calls: list[tuple[float, float]] = []

    def fake_offset(source, start, end, *, max_skip=1.5, min_remain=4.0):
        seen_calls.append((start, max_skip))
        return 1.0

    captured: list[tuple[list[Word], list[tuple]]] = []
    orig_build_ass = render.build_ass

    def spy_ass(words_arg, hook, dur, font, font_path=None, beats=(), **kw):
        captured.append((list(words_arg), list(beats)))
        return orig_build_ass(words_arg, hook, dur, font, font_path, beats=beats, **kw)

    monkeypatch.setattr(render, "_bright_sharp_opening_offset", fake_offset)
    monkeypatch.setattr(render, "build_ass", spy_ass)
    monkeypatch.setattr(render, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"0" * 2048))
    monkeypatch.setattr(render, "probe", lambda p: MediaInfo(8.0, 1080, 1920, 30.0, True))
    monkeypatch.setattr(settings, "clip_loop_ending", False)

    # Case A: dark 0.0..1.0s with Word("Hello", 1.3, 1.8) -> output word starts at ~0.3s, beat shifted by 1.0s
    ed_a = edit.EditPlan(
        words=[Word("Hello", 1.3, 1.8), Word("World.", 3.0, 3.5)],
        segments=[(0.0, 8.0)],
        beats=[edit.Beat(at=4.0, text="50%", style="stat")],
        speed=1.0,
    )
    plan_a = render.render_moment(
        str(src_mp4),
        MediaInfo(20.0, 1920, 1080, 30.0, True),
        _sig(20.0),
        Moment(0.0, 8.0, kind="speech", hook="HOOK"),
        ed_a.words,
        str(tmp_path / "out_a.mp4"),
        edit=ed_a,
    )
    seg0_a = plan_a.segments[0][0]
    assert seg0_a == pytest.approx(1.0, abs=0.05)
    assert captured[0][0][0].start == pytest.approx(1.3 - seg0_a, abs=0.05)
    assert captured[0][1][0][0] == pytest.approx(4.0 - seg0_a, abs=0.05)

    # Case B: Moment(3.0, 9.0) with full-video transcript starting at 0.35s and segment word at 4.5s -> skip still fires!
    seen_calls.clear()
    captured.clear()
    full_words = [Word("Intro", 0.35, 0.8), Word("MidStart", 4.5, 5.0), Word("MidEnd.", 8.0, 8.5)]
    ed_b = edit.EditPlan(
        words=[Word("MidStart", 4.5, 5.0), Word("MidEnd.", 8.0, 8.5)],
        segments=[(3.0, 9.0)],
        speed=1.0,
    )
    plan_b = render.render_moment(
        str(src_mp4),
        MediaInfo(20.0, 1920, 1080, 30.0, True),
        _sig(20.0),
        Moment(3.0, 9.0, kind="speech", hook="HOOK"),
        full_words,
        str(tmp_path / "out_b.mp4"),
        edit=ed_b,
    )
    assert seen_calls and seen_calls[0][0] == pytest.approx(3.0)
    assert seen_calls[0][1] == pytest.approx(1.44, abs=0.02)
    assert plan_b.segments[0][0] == pytest.approx(4.0, abs=0.05)
    assert captured[0][0][0].start == pytest.approx(4.5 - plan_b.segments[0][0], abs=0.05)


def test_min_score_rejects_empty_or_none_ai_replies_without_unvetted_fallback(monkeypatch):
    words = _words_range(80)
    sig = _sig(120.0)
    info = MediaInfo(duration=120.0, width=1920, height=1080, fps=30.0, has_audio=True)

    monkeypatch.setattr(ai, "available", lambda: True)
    for bad_reply in ({"clips": []}, None):
        monkeypatch.setattr(ai, "ask_json", lambda *a, r=bad_reply, **kw: r)
        assert moments.pick_speech(words, sig, count=1, min_sec=15, max_sec=30, min_score=75.0) == []

        monkeypatch.setattr(moments, "_ai_rank_visual", lambda *a, r=bad_reply, **kw: r)
        assert moments.pick_visual("v.mp4", info, sig, count=1, min_score=75.0, max_sec=30) == []

    monkeypatch.setattr(ai, "available", lambda: False)
    assert moments.pick_visual("v.mp4", info, sig, count=1, min_score=75.0, max_sec=30) == []


def test_fill_layout_source_captioned_suppresses_summary_cues(tmp_path, monkeypatch):
    src_mp4 = tmp_path / "fill.mp4"
    src_mp4.write_bytes(b"0" * 20_000)
    seen_summary_cues: list[list] = []
    orig_build_ass = render.build_ass

    def spy_ass(words_arg, hook, dur, font, font_path=None, beats=(), summary_cues=(), **kw):
        seen_summary_cues.append(list(summary_cues))
        return orig_build_ass(words_arg, hook, dur, font, font_path, beats=beats, summary_cues=summary_cues, **kw)

    monkeypatch.setattr(settings, "clip_layout", "fill")
    monkeypatch.setattr(render, "_bright_sharp_opening_offset", lambda *a, **kw: 0.0)
    monkeypatch.setattr(render, "build_ass", spy_ass)
    monkeypatch.setattr(render, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"0" * 2048))
    monkeypatch.setattr(render, "probe", lambda p: MediaInfo(15.0, 1080, 1920, 30.0, True))

    render.render_moment(
        str(src_mp4),
        MediaInfo(20.0, 1920, 1080, 30.0, True),
        _sig(20.0),
        Moment(0.0, 15.0, kind="speech", title="Bhai Shock Ho Gaya", hook="WAIT"),
        _words_range(20),
        str(tmp_path / "out_fill.mp4"),
        captions=True,
        source_captioned=True,
    )
    assert seen_summary_cues == [[]]


def test_clean_vlog_source_captioned_fill_and_vertical_suppress_summary_cues(tmp_path, monkeypatch):
    src_mp4 = tmp_path / "vlog.mp4"
    src_mp4.write_bytes(b"0" * 20_000)
    seen_summary_cues: list[list] = []
    orig_build_ass = render.build_ass

    def spy_ass(words_arg, hook, dur, font, font_path=None, beats=(), summary_cues=(), **kw):
        seen_summary_cues.append(list(summary_cues))
        return orig_build_ass(words_arg, hook, dur, font, font_path, beats=beats, summary_cues=summary_cues, **kw)

    monkeypatch.setattr(render, "_bright_sharp_opening_offset", lambda *a, **kw: 0.0)
    monkeypatch.setattr(render, "build_ass", spy_ass)
    monkeypatch.setattr(render, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"0" * 2048))
    monkeypatch.setattr(render, "probe", lambda p: MediaInfo(15.0, 1080, 1920, 30.0, True))

    moment = Moment(0.0, 15.0, kind="speech", title="Bhai Shock Ho Gaya", hook="WAIT", reveal="End Tak Dekho")

    # 1. Vertical 9:16 source with burned-in captions (crop_bottom == 0.0) -> summary_cues suppressed
    monkeypatch.setattr(settings, "clip_layout", "framed")
    render.render_moment(
        str(src_mp4),
        MediaInfo(20.0, 1080, 1920, 30.0, True),
        _sig(20.0),
        moment,
        [],
        str(tmp_path / "out_vert.mp4"),
        captions=False,
        source_captioned=True,
    )
    assert seen_summary_cues[-1] == []

    # 2. 16:9 source in fill layout with burned-in captions (crop_bottom == 0.0) -> summary_cues suppressed
    monkeypatch.setattr(settings, "clip_layout", "fill")
    render.render_moment(
        str(src_mp4),
        MediaInfo(20.0, 1920, 1080, 30.0, True),
        _sig(20.0),
        moment,
        [],
        str(tmp_path / "out_fill_vlog.mp4"),
        captions=False,
        source_captioned=True,
    )
    assert seen_summary_cues[-1] == []

    # 3. 16:9 source in framed layout (crop_bottom == 0.2 crops out burned-in band) -> summary_cues kept
    monkeypatch.setattr(settings, "clip_layout", "framed")
    render.render_moment(
        str(src_mp4),
        MediaInfo(20.0, 1920, 1080, 30.0, True),
        _sig(20.0),
        moment,
        [],
        str(tmp_path / "out_framed_vlog.mp4"),
        captions=False,
        source_captioned=True,
    )
    assert len(seen_summary_cues[-1]) >= 1


def test_metadata_hinglish_particles_homographs_and_sensitive_plurals():
    assert sanitize_shorts_title("Accidents happen") == "scene happen"
    assert sanitize_shorts_title("Bhooton ka ghar") == "raaz ka ghar"
    assert sanitize_shorts_title("Murders and Ghosts") == "drama and mystery"
    assert sanitize_shorts_title("Murders and Ghosts", language="hi") == "drama and raaz"
    assert sanitize_shorts_title("Ghost Bikini Accident", language="en") == "mystery outfit scene"
    assert detect_language("Ho Ho Hum") == "en"
    assert detect_language("Piyush Ka Surprise") == "hi"
    assert detect_language("Pointing Out the Cliffs From the Boat") == "en"
    assert detect_language("Checking Phones on the Boat...") == "en"
    assert detect_language("Clip 0") == "en"

    init_db()
    with session_scope() as db:
        ch = Channel(slug="indianvlogs_ka", name="Indian Vlogs", niche="hindi vlog", meta={})
        db.add(ch)
        db.flush()
        v = Video(
            source="youtube",
            source_id="hi_ka_1",
            source_url="u",
            title="Piyush Ka Surprise",
            language="",
            channel_id=ch.id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_opening_line": "Surprise!"},
        )
        db.add(v)
        db.flush()
        assert pipeline.clip_gate_reason(v) is None


def test_moments_dedupes_sfx_stings_and_locks_hinglish_even_on_english_source_title():
    # Three 15s sentences separated by 1.5s gaps so build_sentences keeps all 3 distinct.
    words = []
    t = 0.0
    for s_idx in range(3):
        for w_idx in range(25):
            words.append(Word("khatam." if w_idx == 24 else "baat", t, t + 0.6))
            t += 0.6
        t += 1.5
    sentences = moments.build_sentences(words)
    assert len(sentences) == 3

    prompt_en = moments._speech_prompt(sentences, 3, "English Source Title", sounds=("vine-boom-sound", "bruh"), language="hi")
    assert "Never reuse the same sound" in prompt_en
    assert "Even if the source Video title is in English" in prompt_en

    reply = {
        "clips": [
            {"first": 0, "last": 0, "title": "Pehla Clip", "score": 90, "sound": "vine-boom-sound"},
            {"first": 1, "last": 1, "title": "Doosra Clip", "score": 88, "sound": "vine-boom-sound"},
            {"first": 2, "last": 2, "title": "Teesra Clip", "score": 85, "sound": "bruh"},
        ]
    }
    picked = moments._moments_from_reply(
        reply, sentences, words, t, sounds=("vine-boom-sound", "bruh"), min_sec=10, max_sec=30
    )
    assert [m.sound for m in picked] == ["vine-boom-sound", "", "bruh"]


def test_clean_hook_and_description_sanitize_risky_words_and_dangling_tail():
    from app.metadata import has_risky_title_words

    assert has_risky_title_words("Who took a shit in the toilet") is True
    assert "shit" not in sanitize_shorts_title("Who took a shit in the toilet", language="en").lower()
    clean_v = sanitize_shorts_title("cursed voodoo doll with blood", language="en").lower()
    assert "cursed" not in clean_v and "voodoo" not in clean_v and "blood" not in clean_v

    assert "khoon" not in moments.clean_hook("doll ki aankhen se khoon aa raha").lower()
    assert "shit" not in moments.clean_hook("Who took a shit in the toilet").lower()
    assert moments.clean_hook("If I don't get this right, my friend") == "If I don't get this right"

    clean_d = moments.clean_description("Blood streaming from cursed voodoo doll eyes").lower()
    assert "blood" not in clean_d and "cursed" not in clean_d and "voodoo" not in clean_d

    words = [Word("Hello", 0.0, 0.5), Word("world.", 0.5, 1.0)]
    en_prompt = moments._speech_prompt(moments.build_sentences(words), 1, "Challenge", language="en")
    assert "advertiser-friendly" in en_prompt.lower()


def test_clean_vlog_summary_cues_blocks_english_description_leak_for_hindi():
    cues = render._clean_vlog_summary_cues(
        Moment(
            0.0,
            20.0,
            kind="speech",
            title="Piyush Ka Surprise",
            description="An emotional goodbye to their first home.",
        ),
        20.0,
        "Kya Hua Dekho",
        language="hi",
    )
    texts = [t for _, _, t in cues]
    assert not any("emotional goodbye" in t.lower() for t in texts)
    assert "END TAK DEKHO" in texts
