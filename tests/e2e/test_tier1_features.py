"""Tier 1 E2E Tests: Core Feature Coverage across R1, R2, R3, and R4 (Features #1..#13)."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import review as review_mod
from app import review_ui
from app.clipper import ai, captions, edit, ingest, job as clip_job, moments, render as clip_render
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.clipper.transcribe import Transcript, Word
from app.config import settings
from app.db import session_scope
from app.growth import autopilot, channels, notify, safety, scout, supply, titles, watch
from app.longform import job as long_job, planner, render as long_render, voiceover
from app.models import Channel, GrowthEvent, LongJob, MetadataDraft, Video, VideoStatus
from app.winproc import background_subprocess_kwargs
from tasks import pipeline
from tests.e2e.conftest import (
    detect_black_frames,
    detect_silences,
    detect_volume,
    make_sarvam_tts_payload,
    make_synthetic_mp4,
    make_valid_jpeg_bytes,
    make_wav_24k,
    probe_media,
    requires_ffmpeg,
    write_transcript_sidecar,
)


def _indianvlogs_entry(**overrides) -> dict:
    base = {
        "slug": "indianvlogs",
        "name": "Indian Vlogs",
        "niche": "Indian lifestyle vlogs",
        "brand_name": "@MrIndianVlogs001",
        "uploads_per_day": 3,
        "long_uploads_per_day": 1,
        "long_auto_publish": False,
        "timezone": "Asia/Kolkata",
        "require_approval": True,
        "enabled": True,
        "feeds": ["https://www.youtube.com/@souravjvlogs"],
    }
    base.update(overrides)
    return base


def _ambient_entry(**overrides) -> dict:
    base = {
        "slug": "ambient",
        "name": "Deep Focus & Rain Lofi",
        "niche": "lofi ambient rain focus music",
        "brand_name": "@CalmFocusAmbient",
        "uploads_per_day": 0,
        "long_uploads_per_day": 3,
        "long_auto_publish": False,
        "timezone": "UTC",
        "require_approval": True,
        "enabled": True,
        "meta": {
            "mode": "procedural_ambient",
            "presets": ["lofi_chill", "rain_focus", "night_ambient", "deep_study"],
            "generate_while_paused": True,
        },
        "feeds": [],
    }
    base.update(overrides)
    return base


def _dummy_signals(duration: float):
    import numpy as np

    n = max(1, int(duration))
    return Signals(
        duration=duration,
        motion=np.ones(n, dtype=np.float64) * 0.5,
        loudness=np.ones(n, dtype=np.float64) * 0.5,
        brightness=np.ones(n, dtype=np.float64) * 0.5,
        centroid=np.ones(n, dtype=np.float64) * 0.5,
        centroid_weight=np.ones(n, dtype=np.float64),
        cuts=[],
    )


def _hindi_words(start: float, end: float, *, count: int = 24) -> list[Word]:
    step = (end - start) / max(1, count)
    words: list[Word] = []
    for i in range(count):
        ws = start + i * step
        we = ws + step * 0.85
        tok = "गया।" if i == count - 1 else ("भाई" if i == 0 else f"शब्द{i}")
        words.append(Word(tok, round(ws, 2), round(we, 2), 0.95))
    return words


# ==================== WORKSTREAM R1: 16:9 LONG-FORM ENGINE ====================


def test_r1_t1_rejected_and_failed_spans_do_not_block_next_compilation(tmp_path: Path, write_channels, monkeypatch):
    """Feature #1: REJECTED & FAILED long-form rows free spans and part numbers."""
    write_channels([_indianvlogs_entry()])
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        assert ch is not None
        ch_id = ch.id
        db.add(Video(
            source="longform",
            source_id="rejected_p1",
            source_url="https://youtube.com/watch?v=vidA",
            title="Rejected Part 1",
            status=VideoStatus.REJECTED,
            channel_id=ch_id,
            meta={
                "long_form": True,
                "long_format": "best_of",
                "long_targets_key": "@souravjvlogs",
                "part_number": 1,
                "long_segments": [{"source_id": "vidA", "start": 50.0, "end": 120.0}],
            },
        ))
        db.add(Video(
            source="longform",
            source_id="failed_p9",
            source_url="https://youtube.com/watch?v=vidA",
            title="Failed Part 9",
            status=VideoStatus.FAILED,
            channel_id=ch_id,
            meta={
                "long_form": True,
                "long_format": "best_of",
                "long_targets_key": "@souravjvlogs",
                "part_number": 9,
                "long_segments": [{"source_id": "vidA", "start": 130.0, "end": 200.0}],
            },
        ))
        db.add(Video(
            source="longform",
            source_id="pending_p1",
            source_url="https://youtube.com/watch?v=vidB",
            title="Pending Part 1",
            status=VideoStatus.PENDING,
            channel_id=ch_id,
            meta={
                "long_form": True,
                "long_format": "best_of",
                "long_targets_key": "@souravjvlogs",
                "part_number": 1,
                "long_segments": [{"source_id": "vidB", "start": 220.0, "end": 280.0}],
            },
        ))

    with session_scope() as db:
        used = planner.used_spans_for_channel(db, ch_id)
        assert "vidA" not in used
        assert "vidB" in used and len(used["vidB"]) == 1
        assert (used["vidB"][0].start, used["vidB"][0].end) == (220.0, 280.0)
        assert planner.next_part_number(db, ch_id, ["@souravjvlogs"]) == 2

    vid_a = tmp_path / "vidA.mp4"
    vid_b = tmp_path / "vidB.mp4"
    vid_a.write_bytes(b"fake-mp4-a")
    vid_b.write_bytes(b"fake-mp4-b")

    monkeypatch.setattr(
        planner,
        "list_creator_videos",
        lambda url, limit=25: [
            {"id": "vidA", "url": "https://youtube.com/watch?v=vidA", "title": "Vlog A", "duration": 600.0, "creator": "@souravjvlogs"},
            {"id": "vidB", "url": "https://youtube.com/watch?v=vidB", "title": "Vlog B", "duration": 600.0, "creator": "@souravjvlogs"},
        ],
    )
    monkeypatch.setattr(
        ingest,
        "fetch",
        lambda url: ingest.SourceVideo(
            path=str(vid_a if "vidA" in url else vid_b),
            title="Vlog A" if "vidA" in url else "Vlog B",
            author="@souravjvlogs",
            author_name="Sourav Joshi",
            url=url,
            source="youtube",
            source_id="vidA" if "vidA" in url else "vidB",
        ),
    )
    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(duration=600.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, info: _dummy_signals(600.0))
    monkeypatch.setattr(
        planner.transcribe,
        "transcribe",
        lambda p, info: Transcript(words=_hindi_words(50.0, 380.0, count=80), language="hi", duration=600.0, engine="sarvam"),
    )
    monkeypatch.setattr(ai, "available", lambda: False)

    def _fake_pick_speech(words, sig, *, count, title="", used=(), **kw):
        all_candidates = [
            Moment(50.0, 120.0, score=92.0, title="Rejected Span Reusable", kind="speech"),
            Moment(130.0, 200.0, score=89.0, title="Failed Span Reusable", kind="speech"),
            Moment(220.0, 280.0, score=95.0, title="Pending Locked Span", kind="speech"),
            Moment(300.0, 370.0, score=88.0, title="Fresh Span", kind="speech"),
        ]
        free = [m for m in all_candidates if not any(moments.clashes(m, u) for u in used)]
        return free[:count]

    monkeypatch.setattr(moments, "pick_speech", _fake_pick_speech)

    with session_scope() as db:
        plan = planner.plan_compilation(
            format_kind="best_of",
            channel_slug="indianvlogs",
            targets=["@souravjvlogs"],
            db=db,
            channel_id=ch_id,
            min_total_sec=60.0,
        )
    assert plan.part_number == 2
    spans = [(s.source_id, s.start, s.end) for s in plan.segments]
    assert ("vidA", 50.0, 120.0) in spans
    assert ("vidA", 130.0, 200.0) in spans
    assert ("vidB", 220.0, 280.0) not in spans


def test_r1_t1_pool_sizing_and_act_count_reach_8_to_9_min_target(tmp_path: Path, write_channels, monkeypatch):
    """Feature #2: Pool sizing and act cap allow 8–9+ minute compilations without premature truncation."""
    write_channels([_indianvlogs_entry()])
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        assert ch is not None
        ch_id = ch.id

    entries = []
    files: dict[str, Path] = {}
    for idx in range(1, 5):
        vid = f"src{idx}"
        p = tmp_path / f"{vid}.mp4"
        p.write_bytes(b"mp4")
        files[vid] = p
        entries.append({"id": vid, "url": f"https://youtube.com/watch?v={vid}", "title": f"Vlog {idx}", "duration": 720.0, "creator": "@souravjvlogs"})

    monkeypatch.setattr(planner, "list_creator_videos", lambda url, limit=25: entries)
    monkeypatch.setattr(
        ingest,
        "fetch",
        lambda url: ingest.SourceVideo(
            path=str(files[url.split("v=")[-1]]),
            title="Sourav Vlog",
            author="@souravjvlogs",
            author_name="Sourav Joshi",
            url=url,
            source="youtube",
            source_id=url.split("v=")[-1],
        ),
    )
    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(duration=720.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, info: _dummy_signals(720.0))
    monkeypatch.setattr(
        planner.transcribe,
        "transcribe",
        lambda p, info: Transcript(words=_hindi_words(50.0, 650.0, count=120), language="hi", duration=720.0, engine="sarvam"),
    )
    monkeypatch.setattr(
        moments,
        "pick_speech",
        lambda words, sig, *, count, title="", used=(), **kw: [
            Moment(50.0, 120.0, score=85.0, title="Act A", kind="speech"),
            Moment(150.0, 220.0, score=88.0, title="Act B", kind="speech"),
            Moment(260.0, 330.0, score=90.0, title="Act C", kind="speech"),
            Moment(400.0, 465.0, score=93.0, title="Act D", kind="speech"),
        ][:count],
    )
    monkeypatch.setattr(ai, "available", lambda: True)

    def _fake_ask(prompt, **kw):
        if '"indices"' in prompt:
            return {
                "indices": [0, 1, 2, 4, 5, 6, 8, 9],
                "titles": {str(i): f"Chapter {i + 1} Twist" for i in range(10)},
                "theme_hook": "Family road trip",
            }
        return {"title": "Sourav Joshi Best Moments", "description": "Wild trip compilation.", "tags": ["sourav joshi", "vlog"]}

    monkeypatch.setattr(ai, "ask_json", _fake_ask)

    with session_scope() as db:
        plan = planner.plan_compilation(
            format_kind="best_of",
            channel_slug="indianvlogs",
            targets=["@souravjvlogs"],
            target_minutes=9,
            db=db,
            channel_id=ch_id,
        )

    assert plan.target_seconds == 540.0
    total_dur = sum(s.duration for s in plan.segments)
    assert total_dur >= 480.0
    assert len(plan.segments) >= 6
    for seg in plan.segments:
        assert seg.duration <= planner._SEGMENT_MAX_SEC
        assert seg.start >= 45.0
        assert seg.end <= 720.0 - 25.0
    assert "#shorts" not in plan.title.lower()
    assert "#shorts" not in plan.description.lower()
    assert all("#shorts" not in t.lower() for t in plan.tags)


def test_r1_t1_hindi_vs_english_chapter_titles_and_tts_voice_selection(tmp_path: Path, monkeypatch):
    """Feature #3 & #5: Hindi vs English chapter prompts, Devanagari commentary, and Sarvam bulbul:v3 voices."""
    monkeypatch.setattr(settings, "sarvam_api_key", "test-sarvam-key")
    monkeypatch.setattr(ai, "available", lambda: True)

    prompts_seen: list[str] = []

    def _fake_ai(prompt, **kw):
        prompts_seen.append(prompt)
        if '"indices"' in prompt:
            if "Hinglish" in prompt or "Hindi" in prompt:
                return {"indices": [0, 1, 2], "titles": {"0": "Piyush Ki Masti", "1": "Naya Surprise Mein Twist", "2": "Sabse Bada Finale"}}
            return {"indices": [0, 1, 2], "titles": {"0": "Glass Bridge Shock", "1": "Final Vault Trap", "2": "Million Dollar Win"}}
        return {"hook": "Wait until you see @souravjvlogs vs @MrBeast!", "bridges": ["B1", "B2", "B3"], "outro": "Subscribe!"}

    monkeypatch.setattr(ai, "ask_json", _fake_ai)

    hi_segs = [
        planner.SegmentPlan(str(tmp_path / "a.mp4"), "u1", "a", "@souravjvlogs", "Sourav Joshi", "V1", 50.0, 110.0, 90.0, "M1", "hi"),
        planner.SegmentPlan(str(tmp_path / "a.mp4"), "u1", "a", "@souravjvlogs", "Sourav Joshi", "V1", 120.0, 180.0, 92.0, "M2", "hi"),
        planner.SegmentPlan(str(tmp_path / "a.mp4"), "u1", "a", "@souravjvlogs", "Sourav Joshi", "V1", 200.0, 270.0, 95.0, "M3", "hi"),
    ]
    curated_hi = planner._curate_story_arc(
        hi_segs, format_kind="best_of", targets=["@souravjvlogs"], theme="", language="hi", target_sec=480.0, min_total_sec=120.0
    )
    assert any("Hinglish" in p or "Hindi" in p for p in prompts_seen)
    assert "crisp English" not in prompts_seen[0]
    assert [s.moment_title for s in curated_hi] == ["Piyush Ki Masti", "Naya Surprise Mein Twist", "Sabse Bada Finale"]

    hi_plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",),
        theme="", title="Sourav Joshi Best", description="Desc", tags=("vlog",), language="hi",
        target_seconds=480.0, cold_open=curated_hi[-1], segments=tuple(curated_hi),
    )
    en_segs = [
        planner.SegmentPlan(str(tmp_path / "b.mp4"), "u2", "b", "@MrBeast", "MrBeast", "E1", 50.0, 110.0, 90.0, "C1", "en"),
    ]
    en_plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="default", part_number=1, targets=("@MrBeast",),
        theme="", title="MrBeast Best", description="Desc", tags=("challenge",), language="en",
        target_seconds=480.0, cold_open=en_segs[0], segments=tuple(en_segs),
    )

    prompts_seen.clear()
    voiceover.generate_commentary_script(hi_plan)
    assert any("Devanagari" in p for p in prompts_seen)

    tts_posts: list[tuple[str, dict]] = []

    def _fake_post(url, *, headers=None, json=None, timeout=None):
        tts_posts.append((url, json))
        return SimpleNamespace(status_code=200, json=lambda: make_sarvam_tts_payload(1.2), text="ok")

    monkeypatch.setattr(voiceover.httpx, "post", _fake_post)
    monkeypatch.setattr(voiceover, "run_ffmpeg", lambda args, cwd=None: make_wav_24k(args[-1], duration_sec=1.2))

    voiceover.synthesize_wav("@souravjvlogs ka naya vlog!", tmp_path / "hi.wav", language="hi")
    voiceover.synthesize_wav("@MrBeast final challenge!", tmp_path / "en.wav", language="en")

    assert len(tts_posts) == 2
    assert tts_posts[0][0] == "https://api.sarvam.ai/text-to-speech"
    assert tts_posts[0][1]["model"] == "bulbul:v3"
    assert tts_posts[0][1]["target_language_code"] == "hi-IN"
    assert tts_posts[0][1]["speaker"] == "rohan"
    assert "Sourav Joshi" in tts_posts[0][1]["text"]

    assert tts_posts[1][1]["model"] == "bulbul:v3"
    assert tts_posts[1][1]["target_language_code"] == "en-IN"
    assert tts_posts[1][1]["speaker"] == "shubh"
    assert "Mr. Beast" in tts_posts[1][1]["text"]


def test_r1_t1_bridge_and_end_tail_full_duration_ambient_trim_limiting_and_display_name(tmp_path: Path, monkeypatch):
    """Feature #4 & #6: Full-dur bridge/end-tail ambient trim, alimiter <= -0.5 dB, and display_creator_name."""
    src_mp4 = tmp_path / "src.mp4"
    src_mp4.write_bytes(b"mp4")
    wav_path = make_wav_24k(tmp_path / "vo.wav", duration_sec=3.0)

    ffmpeg_calls: list[list[str]] = []
    ass_bodies: list[str] = []

    def _spy_run_ffmpeg(args: list[str], cwd: str | None = None, timeout: int = 3600):
        ffmpeg_calls.append(list(args))
        Path(args[-1]).write_bytes(b"rendered-mp4")

    monkeypatch.setattr(voiceover, "probe", lambda p: MediaInfo(duration=30.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(voiceover, "run_ffmpeg", _spy_run_ffmpeg)
    monkeypatch.setattr(long_render, "probe", lambda p: MediaInfo(duration=30.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(long_render, "run_ffmpeg", _spy_run_ffmpeg)
    monkeypatch.setattr(long_render, "_probe_duration", lambda p, fb: float(fb))
    monkeypatch.setattr(long_render, "_find_bright_offset", lambda p, s, e, n: s)
    monkeypatch.setattr(long_render.timeline, "detect_silences", lambda *a, **kw: [])
    monkeypatch.setattr(long_render, "resolve_font", lambda *a, **kw: str(tmp_path / "font.ttf"))

    orig_write_ass = long_render._write_ass_overlay

    def _spy_ass(out_path: Path, **kw):
        orig_write_ass(out_path, **kw)
        ass_bodies.append(out_path.read_text(encoding="utf-8"))

    monkeypatch.setattr(long_render, "_write_ass_overlay", _spy_ass)

    bridge_dur = 4.0
    voiceover.render_narrator_bridge(
        wav_path,
        src_mp4,
        tmp_path / "bridge.mp4",
        tmp_path,
        badge_text="CHAPTER 1",
        headline="Sourav Joshi",
        creator_label="Sourav Joshi",
        brand_name="@MrIndianVlogs001",
        duration=bridge_dur,
        preview_start=5.0,
        preview_end=15.0,
        music_path=None,
    )
    bridge_fg = ffmpeg_calls[-1][ffmpeg_calls[-1].index("-filter_complex") + 1]
    expected_bridge_dur = max(1.0, bridge_dur + 0.35)
    assert f"[0:a]atrim=0:{expected_bridge_dur:.3f}" in bridge_fg
    lim_m = re.search(r"alimiter=limit=([0-9.]+)", bridge_fg)
    assert lim_m is not None and float(lim_m.group(1)) <= 0.94

    long_render._render_end_tail(
        src_mp4,
        tmp_path / "tail.mp4",
        tmp_path,
        seconds=8.0,
        outro_wav=wav_path,
        seek_sec=2.0,
        music_path=None,
    )
    tail_fg = ffmpeg_calls[-1][ffmpeg_calls[-1].index("-filter_complex") + 1]
    assert "[0:a]atrim=0:8.000" in tail_fg
    lim_t = re.search(r"alimiter=limit=([0-9.]+)", tail_fg)
    assert lim_t is not None and float(lim_t.group(1)) <= 0.94

    seg = planner.SegmentPlan(
        source_path=str(src_mp4), source_url="u", source_id="s1",
        creator="@souravjvlogs", creator_name="Sourav Joshi", video_title="Vlog",
        start=5.0, end=25.0, score=90.0, moment_title="Glass Bridge Twist", language="hi",
    )
    long_render._render_segment(
        seg,
        tmp_path / "seg.mp4",
        tmp_path,
        index=1,
        is_cold_open=False,
        format_kind="best_of",
        brand_name="@MrIndianVlogs001",
    )
    assert ass_bodies
    assert "Sourav Joshi" in ass_bodies[-1]
    assert "@souravjvlogs" not in ass_bodies[-1]


def test_r1_t1_strict_sarvam_tts_quota_and_missing_key_alerting_aborts_render(tmp_path: Path, monkeypatch):
    """Feature #5: Zero Edge-TTS/SAPI fallback; HTTP 402/429 and missing key raise SarvamTTSQuotaError + notify."""
    monkeypatch.setattr(settings, "ntfy_topic", "e2e-topic")
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "send", lambda text, *, title="", link="": alerts.append((title, text)) or True)
    monkeypatch.setattr(ai, "available", lambda: False)
    monkeypatch.setattr(long_render, "probe", lambda p: MediaInfo(duration=60.0, width=1920, height=1080, fps=30.0, has_audio=True))

    src = tmp_path / "src.mp4"
    src.write_bytes(b"mp4")
    seg = planner.SegmentPlan(str(src), "u", "s1", "@souravjvlogs", "Sourav Joshi", "V", 5.0, 25.0, 90.0, "M", "hi")
    plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",),
        theme="", title="Title", description="Desc", tags=("vlog",), language="hi",
        target_seconds=360.0, cold_open=seg, segments=(seg,),
    )

    # Case A: HTTP 402 with secret in body
    voiceover._last_quota_alert_at = 0.0
    monkeypatch.setattr(settings, "sarvam_api_key", "secret_xyz")
    monkeypatch.setattr(
        voiceover.httpx,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=402, text="credit exhausted for secret_xyz"),
    )
    with pytest.raises(voiceover.SarvamTTSQuotaError) as exc_a:
        long_render.render_compilation(plan, str(tmp_path / "out_a.mp4"), include_voiceover=True)
    assert "secret_xyz" not in str(exc_a.value)
    assert len(alerts) == 1
    assert alerts[0][0] == "Sarvam AI TTS Alert"
    assert "secret_xyz" not in alerts[0][1]

    # Case B: Missing API key when voiceover requested
    alerts.clear()
    voiceover._last_quota_alert_at = 0.0
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    with pytest.raises(voiceover.SarvamTTSQuotaError):
        long_render.render_compilation(plan, str(tmp_path / "out_b.mp4"), include_voiceover=True)
    assert len(alerts) == 1
    assert alerts[0][0] == "Sarvam AI TTS Alert"
    assert "edge_tts" not in sys.modules
    assert "pyttsx3" not in sys.modules


# ==================== WORKSTREAM R2: SHORTS 0-VIEW & RETENTION ====================


def test_r2_t1_hindi_clean_vlog_duration_cap_and_opening_line(tmp_path: Path, write_channels, monkeypatch):
    """Feature #10: Hindi clean_vlog enforces <=32s max_sec and populates clip_opening_line."""
    write_channels([_indianvlogs_entry()])
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch_id = ch.id

    src_mp4 = tmp_path / "vlog_hi.mp4"
    src_mp4.write_bytes(b"fake-video")
    words_dicts = [{"text": w.text, "start": w.start, "end": w.end, "confidence": 0.95} for w in _hindi_words(2.0, 38.0, count=24)]
    write_transcript_sidecar(src_mp4, words_dicts, language="hi", engine="sarvam")

    monkeypatch.setattr(clip_job, "require_tools", lambda: None)
    monkeypatch.setattr(clip_job, "probe", lambda p: MediaInfo(duration=60.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(clip_job, "analyse", lambda p, info: _dummy_signals(60.0))
    monkeypatch.setattr(clip_job.subtitles, "already_captioned", lambda p, info: False)
    monkeypatch.setattr(clip_job.sfx, "trending", lambda: {})
    monkeypatch.setattr(clip_job.music, "pick", lambda k: None)
    monkeypatch.setattr(edit.timeline, "detect_silences", lambda *a, **kw: [])

    seen_pick_kw: dict = {}

    def _fake_pick_speech(words, sig, *, count, title="", used=(), sounds=(), **kw):
        seen_pick_kw.update(kw)
        return [Moment(2.0, 38.0, score=88.0, title="Bhai Aaj Twist", hook="Aage Dekho", kind="speech")]

    monkeypatch.setattr(clip_job, "pick_speech", _fake_pick_speech)

    def _fake_render(source, info, signals, moment, words, out_path, **kw):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"short-mp4")
        plan_edit = kw.get("edit")
        assert plan_edit is not None
        dur = plan_edit.duration
        assert dur <= 32.0
        return SimpleNamespace(duration=dur, tail=0.0, music=None, has_sfx=False)

    monkeypatch.setattr(clip_job, "render_moment", _fake_render)
    monkeypatch.setattr(pipeline, "generate_metadata_for_video", lambda vid: {})

    src = ingest.SourceVideo(path=str(src_mp4), title="Hindi Vlog", author="@souravjvlogs", source="youtube", source_id="hi1")
    res = clip_job.clip_source(src, count=1, auto_queue=True, channel_id=ch_id)
    assert res["ok"] is True
    assert seen_pick_kw.get("max_sec", 999) <= 32

    with session_scope() as db:
        v = db.execute(select(Video)).scalar_one()
        assert v.duration_sec <= 32
        assert v.meta.get("clip_opening_line")
        assert v.meta["clip_opening_line"].startswith("भाई")


def test_r2_t1_quality_gate_rejects_low_score_and_weak_visual_clips(tmp_path: Path, write_channels, monkeypatch):
    """Feature #10: Weak visual-only / low-score (<75) clips are blocked from auto-stocking."""
    write_channels([_indianvlogs_entry()])
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch_id = ch.id

    src_mp4 = tmp_path / "src_mix.mp4"
    src_mp4.write_bytes(b"mp4")
    words_dicts = [{"text": w.text, "start": w.start, "end": w.end, "confidence": 0.95} for w in _hindi_words(2.0, 24.0, count=22)]
    write_transcript_sidecar(src_mp4, words_dicts, language="hi", engine="sarvam")

    monkeypatch.setattr(settings, "target_languages", '["hi"]')
    monkeypatch.setattr(clip_job, "require_tools", lambda: None)
    monkeypatch.setattr(clip_job, "probe", lambda p: MediaInfo(duration=60.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(clip_job, "analyse", lambda p, info: _dummy_signals(60.0))
    monkeypatch.setattr(clip_job.subtitles, "already_captioned", lambda p, info: False)
    monkeypatch.setattr(clip_job.sfx, "trending", lambda: {})
    monkeypatch.setattr(clip_job.music, "pick", lambda k: None)
    monkeypatch.setattr(clip_job, "pick_speech", lambda *a, **kw: [])
    monkeypatch.setattr(
        clip_job,
        "pick_visual",
        lambda *a, **kw: [Moment(5.0, 25.0, score=58.0, title="Weak Visual Only", hook="Look", kind="visual")],
    )
    monkeypatch.setattr(
        clip_job,
        "render_moment",
        lambda *a, **kw: (Path(a[5]).parent.mkdir(parents=True, exist_ok=True), Path(a[5]).write_bytes(b"mp4")) and SimpleNamespace(duration=20.0, tail=0.0, music=None, has_sfx=False),
    )
    monkeypatch.setattr(pipeline, "generate_metadata_for_video", lambda vid: {})

    src = ingest.SourceVideo(path=str(src_mp4), title="Hindi Vlog", author="@souravjvlogs", source="youtube", source_id="weak1")
    res = clip_job.clip_source(src, count=1, auto_queue=True, channel_id=ch_id)

    with session_scope() as db:
        rows = db.execute(select(Video)).scalars().all()
    if rows:
        # If saved, weak clip must NOT be auto-stocked into TRANSFORMED ready for auto-publish.
        assert rows[0].status != VideoStatus.TRANSFORMED
    else:
        assert res["clips"] == []

    # Strong speech clip with score 86 passes clip_gate_reason & _screen
    strong_mp4 = tmp_path / "strong.mp4"
    strong_mp4.write_bytes(b"mp4")
    with session_scope() as db:
        strong = Video(
            source="youtube",
            source_id="strong_hi_1",
            source_url="https://youtube.com/watch?v=strong1",
            title="Bhai Aaj Twist",
            language="hi",
            transformed_path=str(strong_mp4),
            duration_sec=22,
            status=VideoStatus.TRANSFORMED,
            channel_id=ch_id,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_score": 86.0, "clip_ai_titled": True},
        )
        db.add(strong)
        db.flush()
        assert pipeline.clip_gate_reason(strong) is None
        passed = autopilot._screen(db, [strong], datetime.now(timezone.utc))
        assert len(passed) == 1


@pytest.mark.slow
@requires_ffmpeg
def test_r2_t1_bright_non_blurry_opening_frame_and_framed_backdrop(tmp_path: Path, monkeypatch):
    """Feature #10: Framed Shorts avoid pure-black opening frame and dim the blurred backdrop."""
    from PIL import Image, ImageStat

    src_mp4 = make_synthetic_mp4(tmp_path / "dark_intro.mp4", duration_sec=10.0, width=1920, height=1080, dark_intro_sec=1.5)
    out_mp4 = tmp_path / "short_framed.mp4"
    monkeypatch.setattr(settings, "clip_layout", "framed")

    info = clip_job.probe(str(src_mp4))
    sig = _dummy_signals(info.duration)
    moment = Moment(0.0, 10.0, score=88.0, title="Bright Frame Test", kind="visual")
    clip_render.render_moment(str(src_mp4), info, sig, moment, [], str(out_mp4), captions=False)

    probed = probe_media(out_mp4)
    assert int(probed["video"]["width"]) == 1080
    assert int(probed["video"]["height"]) == 1920
    assert probed["audio"] is not None

    frame_png = tmp_path / "open.png"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.1", "-i", str(out_mp4), "-frames:v", "1", str(frame_png)],
        stdin=subprocess.DEVNULL,
        check=True,
        **background_subprocess_kwargs(low_priority=True),
    )
    img = Image.open(frame_png).convert("L")
    center_luma = ImageStat.Stat(img.crop((100, 700, 980, 1220))).mean[0]
    top_luma = ImageStat.Stat(img.crop((0, 0, 1080, 280))).mean[0]
    assert center_luma >= 35.0
    assert top_luma < center_luma


def test_r2_t1_clean_vlog_rhythmic_energy_and_hinglish_hook_ass(tmp_path: Path):
    """Feature #10: clean_vlog edits produce rhythmic zoom/pacing points and Hinglish Hook ASS."""
    ed = edit.EditPlan(words=[], segments=[(0.0, 24.0)], beats=[], speed=1.08)
    pts = clip_render.rhythm_points(ed)
    assert len(pts) >= 5
    gaps = [b - a for a, b in zip(pts, pts[1:])]
    assert max(gaps) <= 3.5

    info = MediaInfo(duration=24.0, width=1920, height=1080, fps=30.0, has_audio=True)
    sig = _dummy_signals(24.0)
    plan = clip_render.plan_clip(
        Moment(0.0, 24.0, score=90.0, title="Goa Trip Twist", hook="Aage kya hua dekho!", kind="speech"),
        [],
        sig,
        info,
        has_graphics=True,
        edit=ed,
    )
    assert "t" in plan.zoom_expr and plan.zoom_expr != "1"
    ass_text = captions.build_ass([], "Aage kya hua dekho!", 22.0, "Arial", beats=[], creator="Sourav Joshi", brand="@Brand")
    assert "Dialogue:" in ass_text
    assert "Hook" in ass_text
    assert "aage kya" in ass_text.lower() and "hua dekho!" in ass_text.lower()


def test_r2_t1_language_lock_and_default_language_metadata_alignment(tmp_path: Path, write_channels, monkeypatch):
    """Feature #9: Language-locked channel aligns MetadataDraft and YouTube defaultLanguage/defaultAudioLanguage."""
    write_channels([_indianvlogs_entry()])
    monkeypatch.setattr(settings, "target_languages", '["hi"]')
    monkeypatch.setattr(settings, "use_mock", False)

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch.client_secrets = str(tmp_path / "sec.json")
        ch.token_path = str(tmp_path / "tok.json")
        Path(ch.client_secrets).write_text("{}", encoding="utf-8")
        Path(ch.token_path).write_text("{}", encoding="utf-8")
        mp4 = tmp_path / "clip.mp4"
        mp4.write_bytes(b"mp4")
        v = Video(
            source="youtube",
            source_id="hi_align_1",
            source_url="https://youtube.com/watch?v=hi_align_1",
            title="Piyush Ka Naya Surprise",
            language="hi",
            transformed_path=str(mp4),
            duration_sec=22,
            status=VideoStatus.TRANSFORMED,
            channel_id=ch.id,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_score": 88.0},
        )
        db.add(v)
        db.flush()
        vid = v.id

    pipeline.generate_metadata_for_video(vid)
    with session_scope() as db:
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == vid)).scalar_one()
        assert "#shorts" not in draft.title.lower()
        assert "#Shorts" in draft.description
        assert draft.detected_language == "hi"

    upload_calls: list[dict] = []

    class _FakePub:
        def upload(self, **kw):
            upload_calls.append(kw)
            return SimpleNamespace(video_id="YT_HI_123")

        def post_comment(self, yt_id, text):
            return "CMT_HI_1"

    monkeypatch.setattr(pipeline, "_publisher_for", lambda cid: _FakePub())
    res = pipeline.publish_task(vid)
    assert res["ok"] is True
    assert len(upload_calls) == 1
    assert upload_calls[0]["default_language"] == "hi"
    assert upload_calls[0]["default_audio_language"] == "hi"


def test_r2_t1_titles_style_diversity_clean_hinglish_and_auto_comment(tmp_path: Path, write_channels, monkeypatch):
    """Feature #9: titles.py yields non-plain <=70-char Hinglish styles and publish_task posts engagement comment."""
    monkeypatch.setattr(ai, "available", lambda: True)

    def _fake_ai(prompt, **kw):
        if '{"comment"' in prompt:
            return {"comment": "Aapko kaunsa part sabse funny laga?"}
        return {"title": "Piyush ne yeh kya bol diya end me"}

    monkeypatch.setattr(ai, "ask_json", _fake_ai)

    for style in ("question", "curiosity"):
        t_out, used_style = titles.styled_title(style, "Fallback Title", "Piyush aaj hadh kar di")
        assert used_style == style
        assert len(t_out) <= 70
        assert "#" not in t_out

    q_out, q_style = titles.styled_title("quote", "Fallback Title", "Piyush aaj hadh ho gayi yaar")
    assert q_style == "quote"
    assert len(q_out) <= 70

    write_channels([_indianvlogs_entry()])
    monkeypatch.setattr(settings, "youtube_auto_comment", True)
    monkeypatch.setattr(settings, "use_mock", False)

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        mp4 = tmp_path / "cmt.mp4"
        mp4.write_bytes(b"mp4")
        v = Video(
            source="youtube",
            source_id="cmt_vid_1",
            source_url="https://youtube.com/watch?v=cmt_vid_1",
            title="Piyush Funny Twist",
            language="hi",
            transformed_path=str(mp4),
            duration_sec=20,
            status=VideoStatus.TRANSFORMED,
            channel_id=ch.id,
            meta={"clip_rendered": True},
        )
        db.add(v)
        db.flush()
        vid = v.id

    posted_comments: list[tuple[str, str]] = []

    class _FakePub:
        def upload(self, **kw):
            return SimpleNamespace(video_id="REAL_YT_999")

        def post_comment(self, yt_id, text):
            posted_comments.append((yt_id, text))
            return "CMT_999"

    monkeypatch.setattr(pipeline, "_publisher_for", lambda cid: _FakePub())
    res = pipeline.publish_task(vid)
    assert res["ok"] is True
    assert len(posted_comments) == 1
    assert posted_comments[0][0] == "REAL_YT_999"
    with session_scope() as db:
        row = db.get(Video, vid)
        assert row.meta.get("auto_comment_id") == "CMT_999"


# ==================== WORKSTREAM R3: CHANNEL #3 PROCEDURAL AMBIENT ====================


@pytest.mark.slow
@requires_ffmpeg
def test_r3_t1_procedural_ambient_generates_16x9_claim_free_media(tmp_path: Path):
    """Feature #11: app.longform.ambient generates 1080p claim-free stereo media with zero dropouts/clipping."""
    from app.longform import ambient

    assert "lofi_chill" in ambient.AMBIENT_PRESETS
    assert "rain_focus" in ambient.AMBIENT_PRESETS

    for preset in ("lofi_chill", "rain_focus"):
        out_mp4 = tmp_path / f"{preset}.mp4"
        thumbs_dir = tmp_path / f"{preset}_thumbs"
        res = ambient.generate_ambient_video(
            preset,
            output_path=str(out_mp4),
            duration_sec=18.0,
            thumbs_dir=thumbs_dir,
        )
        assert out_mp4.is_file() and out_mp4.stat().st_size > 50_000

        probed = probe_media(out_mp4)
        assert int(probed["video"]["width"]) == 1920
        assert int(probed["video"]["height"]) == 1080
        assert probed["video"]["codec_name"] == "h264"
        assert probed["audio"] is not None
        assert int(probed["audio"]["channels"]) == 2
        assert int(probed["audio"]["sample_rate"]) == 48_000

        assert detect_silences(out_mp4, noise_db=-40.0, min_dur=2.0) == []
        vol = detect_volume(out_mp4)
        assert -36.0 <= vol["mean_volume"] <= -6.0
        assert vol["max_volume"] <= -0.5
        assert detect_black_frames(out_mp4, duration=0.2, pic_th=0.98) == []
        assert res.chapters
        assert len(res.thumbnails) >= 3
        assert (thumbs_dir / "hero_raw.jpg").is_file()


def test_r3_t1_numpy_lazy_import_and_winproc_ffmpeg_invariants(tmp_path: Path, monkeypatch):
    """Feature #11 & #13: Importing ambient/main/review/scheduler keeps numpy & cv2 unloaded; ambient ffmpeg uses winproc & threads."""
    code = (
        "import sys, app.main, app.review, app.scheduler, app.longform.ambient; "
        "assert 'numpy' not in sys.modules and 'cv2' not in sys.modules, 'numpy or cv2 eagerly imported!'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    from app.longform import ambient

    ambient_src = Path(ambient.__file__).read_text(encoding="utf-8")
    tree = ast.parse(ambient_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "Popen"):
            kw_names = {k.arg for k in node.keywords if k.arg}
            assert "stdin" in kw_names
            assert any(isinstance(k, ast.keyword) and k.arg is None for k in node.keywords)


def test_r3_t1_channel3_feedless_config_and_pipeline_isolation(write_channels):
    """Feature #12: Channel #3 requires zero feeds and is skipped cleanly by Shorts supply/watch/scout."""
    write_channels([_indianvlogs_entry(), _ambient_entry()])
    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        assert ch3 is not None
        assert channels.is_procedural(ch3) is True
        step = channels.next_step(db, ch3)
        assert "give it a creator to clip" not in step

    sup = supply.supply_job()
    assert "ambient" not in sup or "starved" not in str(sup.get("ambient", ""))
    wt = watch.watch_job()
    assert wt["feeds"] == 1  # only indianvlogs feed
    sc = scout.scout_job()
    assert "added" not in (sc.get("ambient") or {})

    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        gap = db.get(channels.GrowthState, channels.state_key(ch3, supply.SUPPLY_GAP))
        assert gap is None or not gap.value


def test_r3_t1_long_supply_job_queues_up_to_3_per_day_into_pending(tmp_path: Path, write_channels, monkeypatch):
    """Feature #13: long_supply_job builds Channel #3 up to 3/day into VideoStatus.PENDING without feeds."""
    from app.longform import ambient, thumbnails

    write_channels([_ambient_entry()])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    monkeypatch.setattr(long_job, "_STOCK_CAP", 5)

    def _fake_ambient(preset, target_minutes, output_path, **kw):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake-ambient-mp4")
        tdir = Path(output_path).parent / f"{Path(output_path).stem}_thumbs"
        tdir.mkdir(parents=True, exist_ok=True)
        tp = tdir / "v1.jpg"
        tp.write_bytes(make_valid_jpeg_bytes())
        return ambient.AmbientResult(
            output_path=str(output_path),
            duration=float(target_minutes) * 60.0,
            preset=preset,
            width=1920,
            height=1080,
            fps=30,
            title=f"{preset} Focus Session",
            description="Claim-free procedural focus ambience.",
            tags=("lofi", "focus", "ambient"),
            chapters=((0.0, "Intro"),),
            chapter_text="0:00 Intro",
            thumbnails=({"id": "v1", "label": "V1", "path": str(tp)},),
            hero_frame=str(tp),
            thumbnail_prompt="minimal rain window",
            language="en",
            theme="",
            part_number=1,
        )

    monkeypatch.setattr(ambient, "generate_ambient_video", _fake_ambient)
    monkeypatch.setattr(thumbnails, "generate_thumbnails_with_prompt", lambda *a, **kw: ([], ""))

    for _ in range(4):
        res = long_job.long_supply_job()
        amb_res = res.get("ambient", {})
        if amb_res.get("queued"):
            long_job.run_next_long_job()

    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        videos = db.execute(select(Video).where(Video.channel_id == ch3.id)).scalars().all()
        assert 1 <= len(videos) <= 3
        for v in videos:
            assert v.status == VideoStatus.PENDING
            assert v.meta.get("long_form") is True
            assert v.meta.get("clip_rendered") is False
            assert "#shorts" not in (v.title or "").lower()
            assert "#shorts" not in (v.description or "").lower()


def test_r3_t1_long_autopilot_triple_lock_blocks_unlinked_and_unapproved(tmp_path: Path, write_channels, monkeypatch):
    """Feature #13: Triple safety lock blocks Channel #3 upload unless enabled, linked, unpaused, and approved."""
    write_channels([_ambient_entry(enabled=True, long_auto_publish=False)])
    monkeypatch.setattr(settings, "use_mock", False)
    monkeypatch.setattr(settings, "enable_publish", True)

    mp4 = tmp_path / "amb.mp4"
    mp4.write_bytes(b"mp4")
    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        v = Video(
            source="longform",
            source_id="amb_1",
            source_url="",
            title="Lofi Rain Focus",
            transformed_path=str(mp4),
            duration_sec=3600,
            status=VideoStatus.TRANSFORMED,
            channel_id=ch3.id,
            meta={"long_form": True, "clip_rendered": False},
        )
        db.add(v)
        db.flush()
        vid = v.id
        ch3_id = ch3.id

    pub_calls: list[str] = []
    monkeypatch.setattr(pipeline, "publish_task", lambda v_id, meta=None: pub_calls.append(v_id) or {"ok": True, "youtube_id": "YT_AMB"})

    # 1) Unlinked credentials -> blocked
    long_job.long_autopilot_job()
    assert pub_calls == []

    # 2) Valid creds on disk, but paused -> blocked
    sec = tmp_path / "amb_sec.json"
    tok = tmp_path / "amb_tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    with session_scope() as db:
        ch3 = db.get(Channel, ch3_id)
        ch3.client_secrets = str(sec)
        ch3.token_path = str(tok)
        ch3.youtube_channel_id = "UC_AMBIENT_TEST"
    safety.pause("ambient")
    long_job.long_autopilot_job()
    assert pub_calls == []
    safety.resume("ambient")

    # 3) Unapproved PENDING with long_auto_publish=False -> blocked
    with session_scope() as db:
        row = db.get(Video, vid)
        row.status = VideoStatus.PENDING
    long_job.long_autopilot_job()
    assert pub_calls == []

    # 4) Owner-approved TRANSFORMED + linked + enabled + unpaused -> allowed
    with session_scope() as db:
        row = db.get(Video, vid)
        row.status = VideoStatus.TRANSFORMED
        row.meta = {**(row.meta or {}), "owner_approved": "2026-05-01T00:00:00Z"}
    long_job.long_autopilot_job()
    assert pub_calls == [vid]


def test_r3_t1_review_api_and_long_tab_ui_preset_wiring(anon_client, auth_client, write_channels, monkeypatch):
    """Feature #13: Control Panel exposes Channel #3 presets behind _guard and passes node --check."""
    write_channels([_ambient_entry()])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)

    assert anon_client.get("/api/long/videos").status_code == 401
    assert anon_client.get("/api/channels").status_code == 401
    assert anon_client.post("/api/long/build", json={"channel_slug": "ambient", "format": "ambient"}).status_code == 401

    assert auth_client.get("/api/long/videos").status_code == 200
    assert auth_client.get("/api/channels").status_code == 200
    resp = auth_client.post(
        "/api/long/build",
        json={"channel_slug": "ambient", "format": "ambient", "theme": "lofi_chill", "target_minutes": 60},
    )
    assert resp.status_code == 200
    assert resp.json().get("status") == "queued"

    assert not hasattr(review_mod, "ClipJob")
    assert "lofi_chill" in review_ui.PAGE and "rain_focus" in review_ui.PAGE


# ==================== WORKSTREAM R4: SAFETY, PANEL & INVARIANTS ====================


def test_r4_t1_new_longform_defaults_to_pending_and_autopilot_blocks_unapproved(tmp_path: Path, write_channels, monkeypatch):
    """Feature #13: Newly built 16:9 compilations land in VideoStatus.PENDING and cannot auto-publish."""
    sec = tmp_path / "sec.json"
    tok = tmp_path / "tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    write_channels([_indianvlogs_entry(client_secrets=str(sec), token_path=str(tok), long_auto_publish=False)])

    from app.longform import thumbnails

    monkeypatch.setattr(
        planner,
        "plan_compilation",
        lambda **kw: planner.CompilationPlan(
            format_kind="best_of",
            channel_slug="indianvlogs",
            part_number=1,
            targets=("@souravjvlogs",),
            theme="",
            title="Best of Sourav Joshi",
            description="Fun moments.",
            tags=("vlog",),
            language="hi",
            target_seconds=480.0,
            cold_open=planner.SegmentPlan("s.mp4", "u", "s1", "@souravjvlogs", "Sourav Joshi", "V", 10.0, 20.0, 90.0, "M1", "hi"),
            segments=(planner.SegmentPlan("s.mp4", "u", "s1", "@souravjvlogs", "Sourav Joshi", "V", 50.0, 120.0, 90.0, "M1", "hi"),),
        ),
    )

    def _fake_render(plan, out_path, **kw):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"mp4")
        return SimpleNamespace(output_path=str(out_path), duration=500.0, chapters=[(0.0, "Intro")], chapter_text="0:00 Intro", timestamps_for_thumbs=[10.0])

    monkeypatch.setattr(long_render, "render_compilation", _fake_render)
    monkeypatch.setattr(thumbnails, "generate_thumbnails_with_prompt", lambda *a, **kw: ([], ""))

    long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs")
    res = long_job.run_next_long_job()
    assert res["ok"] is True

    with session_scope() as db:
        v = db.get(Video, res["video_id"])
        assert v.status == VideoStatus.PENDING
        assert v.transformed_at is None
        assert v.meta.get("long_form") is True

    pub_calls: list[str] = []
    monkeypatch.setattr(pipeline, "publish_task", lambda vid, meta=None: pub_calls.append(vid) or {"ok": True})
    monkeypatch.setattr(settings, "use_mock", False)
    auto = long_job.long_autopilot_job()
    assert auto["indianvlogs"]["skipped"] == "none_ready"
    assert pub_calls == []


def test_r4_t1_long_autopilot_blocks_paused_held_disabled_unlinked_and_mock(tmp_path: Path, write_channels, monkeypatch):
    """Feature #13: long_autopilot_job enforces all 5 publishing safety gates."""
    sec = tmp_path / "sec.json"
    tok = tmp_path / "tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    write_channels([_indianvlogs_entry(client_secrets=str(sec), token_path=str(tok), enabled=True)])

    mp4 = tmp_path / "ready_long.mp4"
    mp4.write_bytes(b"mp4")
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch_id = ch.id
        v = Video(
            source="longform",
            source_id="long_ready_1",
            source_url="",
            title="Ready Long Video",
            transformed_path=str(mp4),
            duration_sec=500,
            status=VideoStatus.TRANSFORMED,
            channel_id=ch_id,
            meta={"long_form": True, "owner_approved": "2026-05-01T00:00:00Z"},
        )
        db.add(v)
        db.flush()
        vid = v.id

    pub_calls: list[str] = []
    orig_publish_task = pipeline.publish_task
    monkeypatch.setattr(pipeline, "publish_task", lambda v_id, meta=None: pub_calls.append(v_id) or {"ok": True, "youtube_id": "YT_OK"})
    monkeypatch.setattr(settings, "use_mock", False)

    # 1) Disabled channel
    with session_scope() as db:
        db.get(Channel, ch_id).enabled = False
    long_job.long_autopilot_job()
    assert pub_calls == []

    # 2) Paused channel
    with session_scope() as db:
        db.get(Channel, ch_id).enabled = True
    safety.pause("indianvlogs")
    long_job.long_autopilot_job()
    assert pub_calls == []
    safety.resume("indianvlogs")

    # 3) Active hold
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        ch = db.get(Channel, ch_id)
        safety.set_hold(db, now, "quota", ch)
    long_job.long_autopilot_job(now=now)
    assert pub_calls == []
    with session_scope() as db:
        safety.clear_hold(db, db.get(Channel, ch_id))

    # 4) Unlinked credentials (missing token file)
    tok.unlink()
    long_job.long_autopilot_job()
    assert pub_calls == []
    tok.write_text("{}", encoding="utf-8")

    # 5) enable_publish=False blocks completely; use_mock=True uses MockPublisher and never touches YouTubePublisher
    monkeypatch.setattr(settings, "enable_publish", False)
    long_job.long_autopilot_job()
    assert pub_calls == []

    with session_scope() as db:
        assert db.get(Video, vid).status == VideoStatus.TRANSFORMED
        assert db.execute(select(GrowthEvent).where(GrowthEvent.kind == "long_posted")).scalars().all() == []

    from app.publishers import YouTubePublisher

    monkeypatch.setattr(pipeline, "publish_task", orig_publish_task)
    monkeypatch.setattr(YouTubePublisher, "upload", lambda *a, **kw: pytest.fail("YouTubePublisher.upload must not run under use_mock=True"))
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "use_mock", True)
    long_job.long_autopilot_job()
    with session_scope() as db:
        row = db.get(Video, vid)
        assert row.status == VideoStatus.PUBLISHED
        assert (row.youtube_video_id or "").startswith("MOCK_")


def test_r4_t1_control_panel_long_and_channels_routes_enforce_guard_and_lifecycle(tmp_path: Path, anon_client, auth_client, write_channels, monkeypatch):
    """Feature #13: Control Panel /api/long/* & /api/channels* enforce _guard and lifecycle mutations."""
    write_channels([_indianvlogs_entry()])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)

    mp4_1 = tmp_path / "l1.mp4"
    mp4_2 = tmp_path / "l2.mp4"
    mp4_1.write_bytes(b"mp4")
    mp4_2.write_bytes(b"mp4")
    tdir = tmp_path / "thumbs"
    tdir.mkdir()
    t1 = tdir / "v1.jpg"
    t2 = tdir / "v2.jpg"
    hero = tdir / "hero_raw.jpg"
    for p in (t1, t2, hero):
        p.write_bytes(make_valid_jpeg_bytes())

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        v1 = Video(
            source="longform", source_id="lv1", source_url="", title="Long 1",
            transformed_path=str(mp4_1), duration_sec=480, status=VideoStatus.PENDING, channel_id=ch.id,
            meta={"long_form": True, "hero_frame": str(hero), "thumbnails": [{"id": "v1", "label": "V1", "path": str(t1)}, {"id": "v2", "label": "V2", "path": str(t2)}]},
        )
        v2 = Video(
            source="longform", source_id="lv2", source_url="", title="Long 2",
            transformed_path=str(mp4_2), duration_sec=480, status=VideoStatus.PENDING, channel_id=ch.id,
            meta={"long_form": True},
        )
        db.add_all([v1, v2])
        db.flush()
        id1, id2 = v1.id, v2.id

    for method, url in (
        ("get", "/api/long/videos"),
        ("post", "/api/long/build"),
        ("post", f"/api/long/{id1}/metadata"),
        ("post", f"/api/long/{id1}/thumbnail"),
        ("post", f"/api/long/{id1}/approve"),
        ("post", f"/api/long/{id2}/reject"),
        ("post", "/api/channels/indianvlogs/pause"),
    ):
        resp_unauth = getattr(anon_client, method)(url) if method == "get" else getattr(anon_client, method)(url, json={})
        assert resp_unauth.status_code == 401

    assert auth_client.get("/api/long/videos").status_code == 200
    assert auth_client.post("/api/long/build", json={"channel_slug": "indianvlogs", "format": "best_of", "target": "@souravjvlogs"}).status_code == 200
    assert auth_client.post(f"/api/long/{id1}/metadata", json={"title": "Clean Updated Title", "tags": ["vlog", "hindi"]}).status_code == 200
    assert auth_client.post(f"/api/long/{id1}/thumbnail", json={"variant_id": "v2"}).status_code == 200
    app_resp = auth_client.post(f"/api/long/{id1}/approve", json={})
    assert app_resp.status_code == 200 and app_resp.json()["status"] == "transformed"
    rej_resp = auth_client.post(f"/api/long/{id2}/reject")
    assert rej_resp.status_code == 200 and rej_resp.json()["status"] == "rejected"

    assert auth_client.post("/api/channels/indianvlogs/settings", json={"long_uploads_per_day": 2}).status_code == 200
    assert auth_client.post("/api/channels/indianvlogs/pause").status_code == 200
    assert auth_client.post("/api/channels/indianvlogs/resume").status_code == 200

    with session_scope() as db:
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == id1)).scalar_one()
        assert "#shorts" not in draft.title.lower()
        assert db.get(Video, id1).meta.get("owner_approved")


def test_r4_t1_review_ui_long_tab_elements_and_node_check_js_syntax(auth_client, tmp_path: Path):
    """Feature #13: Control Panel HTML has #tab-long elements and inline JS passes node --check."""
    resp = auth_client.get("/")
    assert resp.status_code == 200
    html = resp.text
    for token in ('id="tab-long"', "tab-long", "long-build-form", "long-jobs", "long-list", "best_of", "vs", "theme"):
        assert token in html

    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", review_ui.PAGE, flags=re.DOTALL | re.IGNORECASE)
    assert scripts
    node = shutil.which("node")
    assert node, "node binary required for inline JS syntax check"
    for idx, body in enumerate(scripts):
        js_file = tmp_path / f"page_{idx}.js"
        js_file.write_text(body, encoding="utf-8")
        proc = subprocess.run(
            [node, "--check", str(js_file)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        assert proc.returncode == 0, f"node --check failed on script[{idx}]: {proc.stderr}"


def test_r4_t1_idle_import_and_winproc_invariants():
    """Feature #13: Idle server/shots import invariants & winproc subprocess AST safety."""
    sub1 = subprocess.run(
        [sys.executable, "-c", "import sys, app.main, app.review, app.config, app.db, app.logging_config; assert 'numpy' not in sys.modules"],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert sub1.returncode == 0, sub1.stderr

    sub2 = subprocess.run(
        [sys.executable, "-c", "import sys, app.clipper.shots; assert 'cv2' not in sys.modules"],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert sub2.returncode == 0, sub2.stderr
    assert not hasattr(review_mod, "ClipJob")

    root = Path(__file__).resolve().parent.parent.parent
    for py_dir in (root / "app", root / "tasks"):
        for py_file in py_dir.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" and node.func.attr in ("run", "Popen"):
                        kw_names = {k.arg for k in node.keywords if k.arg}
                        has_kwargs_splat = any(isinstance(k, ast.keyword) and k.arg is None and isinstance(k.value, ast.Name) and k.value.id == "kwargs" for k in node.keywords)
                        assert "stdin" in kw_names or "input" in kw_names or has_kwargs_splat, f"Missing stdin/input in {py_file}:{node.lineno}"
                        assert any(isinstance(k, ast.keyword) and k.arg is None for k in node.keywords), (
                            f"Missing **background_subprocess_kwargs in {py_file}:{node.lineno}"
                        )
