"""Tier 2 E2E Tests: Boundary, Edge & Error-Handling Cases across R1, R2, R3, and R4."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app import logging_config, quota, review as review_mod, scheduler
from app.clipper import ai, edit, ingest, job as clip_job, moments, render as clip_render
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.clipper.transcribe import Transcript, Word
from app.config import PROJECT_ROOT, settings
from app.db import session_scope
from app.growth import approval, autopilot, channels, notify, safety, titles
from app.longform import job as long_job, planner, render as long_render, voiceover
from app.models import Channel, LongJob, QuotaUsage, Video, VideoStatus
from scripts import compile as compile_cli, doctor, final_audit
from tasks import pipeline
from tests.e2e.conftest import (
    _assert_e2e_isolated,
    make_synthetic_mp4,
    make_valid_jpeg_bytes,
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


# ==================== R1 BOUNDARIES ====================


def test_r1_t2_quota_alert_cooldown_and_secret_scrubbing(tmp_path: Path, monkeypatch):
    """R1-T2-01: Consecutive Sarvam 429s trigger notify once within cooldown and scrub secrets."""
    secret_key = "sarvam_super_secret_token_999"
    monkeypatch.setattr(settings, "sarvam_api_key", secret_key)
    monkeypatch.setattr(settings, "ntfy_topic", "e2e-alerts")
    voiceover._last_quota_alert_at = 0.0

    alerts: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, *, title="", link="": alerts.append(text) or True)
    monkeypatch.setattr(
        voiceover.httpx,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=429,
            text=f"rate limit exceeded key={secret_key} review={settings.review_token}",
        ),
    )

    for idx in range(2):
        with pytest.raises(voiceover.SarvamTTSQuotaError) as exc_info:
            voiceover.synthesize_wav("Hello test speech", tmp_path / f"vo_{idx}.wav", language="en")
        msg = str(exc_info.value)
        assert secret_key not in msg
        assert settings.review_token not in msg

    assert len(alerts) == 1
    assert secret_key not in alerts[0]
    assert settings.review_token not in alerts[0]


def test_r1_t2_mock_published_rehearsal_does_not_consume_spans_or_part_number(write_channels):
    """R1-T2-02: MOCK_ published rehearsal rows do not consume spans or increment part numbers."""
    write_channels([_indianvlogs_entry()])
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        db.add(Video(
            source="longform",
            source_id="mock_rehearsal_1",
            source_url="",
            title="Rehearsal Video",
            status=VideoStatus.PUBLISHED,
            youtube_video_id="MOCK_rehearsal_1",
            channel_id=ch.id,
            meta={
                "long_form": True,
                "long_format": "best_of",
                "long_targets_key": "@souravjvlogs",
                "part_number": 5,
                "long_segments": [{"source_id": "v1", "start": 60.0, "end": 120.0}],
            },
        ))

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        assert planner.used_spans_for_channel(db, ch.id) == {}
        assert planner.next_part_number(db, ch.id, ["@souravjvlogs"]) == 1


def test_r1_t2_short_source_or_corrupt_sarvam_wav_handling(tmp_path: Path, write_channels, monkeypatch):
    """R1-T2-03: Starved sources fail cleanly with LongJob=failed; corrupt Sarvam base64 returns None."""
    write_channels([_indianvlogs_entry()])
    monkeypatch.setattr(planner, "list_creator_videos", lambda *a, **kw: [])

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        with pytest.raises(ValueError, match="no eligible source"):
            planner.plan_compilation(format_kind="best_of", channel_slug="indianvlogs", targets=["@souravjvlogs"], db=db, channel_id=ch.id)

    jid = long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs")
    res = long_job.run_next_long_job()
    assert res["ok"] is False
    with session_scope() as db:
        job_row = db.get(LongJob, jid)
        assert job_row.status == "failed"
        assert "ValueError" in (job_row.error or "")

    # Corrupt / tiny Sarvam payload returns None cleanly
    monkeypatch.setattr(settings, "sarvam_api_key", "key123")
    monkeypatch.setattr(
        voiceover.httpx,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {"audios": ["!!!not-base64!!!"]}, text="ok"),
    )
    assert voiceover.synthesize_wav("Valid text", tmp_path / "bad.wav", language="en") is None


def test_r1_t2_source_chronology_preserved_and_monetization_unsafe_filtered(tmp_path: Path, monkeypatch):
    """R1-T2-04: Monetization-unsafe segments are dropped; same-source segments stay chronological."""
    monkeypatch.setattr(ai, "available", lambda: False)
    unsafe = planner.SegmentPlan(
        str(tmp_path / "s.mp4"), "u", "src1", "@souravjvlogs", "Sourav Joshi", "V",
        300.0, 360.0, 99.0, "Cocaine prank gone wrong", "en",
    )
    filler = planner.SegmentPlan(
        str(tmp_path / "s.mp4"), "u", "src1", "@souravjvlogs", "Sourav Joshi", "V",
        380.0, 440.0, 96.0, "Hotel toilet and laundry tour", "en",
    )
    s_late = planner.SegmentPlan(
        str(tmp_path / "s.mp4"), "u", "src1", "@souravjvlogs", "Sourav Joshi", "V",
        480.0, 540.0, 95.0, "Final Race", "en",
    )
    s_early = planner.SegmentPlan(
        str(tmp_path / "s.mp4"), "u", "src1", "@souravjvlogs", "Sourav Joshi", "V",
        60.0, 120.0, 88.0, "Morning Start", "en",
    )
    s_mid = planner.SegmentPlan(
        str(tmp_path / "s.mp4"), "u", "src1", "@souravjvlogs", "Sourav Joshi", "V",
        200.0, 260.0, 82.0, "Mid Twist", "en",
    )
    curated = planner._curate_story_arc(
        [s_late, unsafe, filler, s_early, s_mid],
        format_kind="best_of",
        targets=["@souravjvlogs"],
        theme="",
        language="en",
        target_sec=360.0,
        min_total_sec=120.0,
    )
    assert all("cocaine" not in s.moment_title.lower() for s in curated)
    assert all("toilet" not in s.moment_title.lower() and "laundry" not in s.moment_title.lower() for s in curated)
    starts = [s.start for s in curated if s.source_id == "src1"]
    assert starts == sorted(starts)
    assert starts == [60.0, 200.0, 480.0]


def test_r1_t2_compile_cli_run_now_and_retry_exit_codes(tmp_path: Path, write_channels, monkeypatch):
    """R1-T2-05: scripts/compile.py exits 1 on failed run-now and exits 0 on --retry --run-now."""
    write_channels([_indianvlogs_entry()])
    monkeypatch.setattr(planner, "list_creator_videos", lambda *a, **kw: [])

    rc_fail = compile_cli.main([
        "--channel", "indianvlogs", "--format", "best_of", "--creator", "@souravjvlogs",
        "--minutes", "8", "--run-now", "--json",
    ])
    assert rc_fail == 1
    jobs = long_job.list_long_jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == "failed"
    jid = jobs[0]["id"]

    from app.longform import thumbnails

    monkeypatch.setattr(
        planner,
        "plan_compilation",
        lambda **kw: planner.CompilationPlan(
            format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",),
            theme="", title="Retry Success", description="Desc", tags=("vlog",), language="hi",
            target_seconds=480.0,
            cold_open=planner.SegmentPlan("s.mp4", "u", "s1", "@souravjvlogs", "Sourav Joshi", "V", 10.0, 20.0, 90.0, "M1", "hi"),
            segments=(planner.SegmentPlan("s.mp4", "u", "s1", "@souravjvlogs", "Sourav Joshi", "V", 50.0, 120.0, 90.0, "M1", "hi"),),
        ),
    )

    def _fake_render(plan, out_path, **kw):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"mp4")
        return SimpleNamespace(output_path=str(out_path), duration=480.0, chapters=[(0.0, "Intro")], chapter_text="0:00 Intro", timestamps_for_thumbs=[10.0])

    monkeypatch.setattr(long_render, "render_compilation", _fake_render)
    monkeypatch.setattr(thumbnails, "generate_thumbnails_with_prompt", lambda *a, **kw: ([], ""))

    rc_ok = compile_cli.main(["--retry", jid, "--run-now", "--json"])
    assert rc_ok == 0
    with session_scope() as db:
        job_row = db.get(LongJob, jid)
        assert job_row.status == "done"
        assert db.get(Video, job_row.video_id).status == VideoStatus.PENDING


def test_r1_t2_target_minutes_clamping_and_dangling_caption_cleanup(write_channels):
    """R1-T2-06: Standard long-form target_minutes clamps to 6..10 and strips #Shorts / dangling words."""
    write_channels([_indianvlogs_entry()])
    jid_low = long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs", target_minutes=2)
    jid_high = long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs", target_minutes=99)
    with session_scope() as db:
        assert db.get(LongJob, jid_low).target_minutes == 6
        assert db.get(LongJob, jid_high).target_minutes == 10

    cap = long_render._short_caption("Crazy Road Trip for #Shorts...")
    assert "#shorts" not in cap.lower()
    assert "..." not in cap
    assert not cap.lower().endswith(" for")
    ch_title = planner._clean_chapter_title("Final Challenge #Shorts...", "Fallback Title")
    assert "#shorts" not in ch_title.lower()
    assert "..." not in ch_title


# ==================== R2 BOUNDARIES ====================


def test_r2_t2_empty_transcript_graceful_degradation(tmp_path: Path, monkeypatch):
    """R2-T2-01: Empty transcript words degrades cleanly without IndexError."""
    src_mp4 = tmp_path / "silent.mp4"
    src_mp4.write_bytes(b"mp4")
    write_transcript_sidecar(src_mp4, [], language="hi", engine="sarvam")

    monkeypatch.setattr(clip_job, "require_tools", lambda: None)
    monkeypatch.setattr(clip_job, "probe", lambda p: MediaInfo(duration=60.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(clip_job, "analyse", lambda p, info: _dummy_signals(60.0))
    monkeypatch.setattr(clip_job.subtitles, "already_captioned", lambda p, info: False)
    monkeypatch.setattr(clip_job.sfx, "trending", lambda: {})
    monkeypatch.setattr(clip_job, "pick_visual", lambda *a, **kw: [])

    src = ingest.SourceVideo(path=str(src_mp4), title="Silent", author="@a", source="youtube", source_id="sil1")
    res = clip_job.clip_source(src, count=1, auto_queue=False)
    assert res["ok"] is False
    assert "no usable moments" in res["error"]
    assert edit.first_sentence_text(Moment(0.0, 20.0), []) == ""


@pytest.mark.slow
@requires_ffmpeg
def test_r2_t2_all_dark_and_blurry_opening_frames_handled(tmp_path: Path, monkeypatch):
    """R2-T2-02: Uniformly dark or blurry video handles frame selection without crash."""
    monkeypatch.setattr(settings, "clip_layout", "framed")
    dark_mp4 = make_synthetic_mp4(tmp_path / "all_dark.mp4", duration_sec=8.0, width=1280, height=720, dark_all=True)
    out_mp4 = tmp_path / "dark_short.mp4"
    info = clip_job.probe(str(dark_mp4))
    sig = _dummy_signals(info.duration)
    clip_render.render_moment(str(dark_mp4), info, sig, Moment(0.0, 8.0, score=80.0, title="Dark"), [], str(out_mp4), captions=False)
    probed = probe_media(out_mp4)
    assert int(probed["video"]["width"]) == 1080
    assert int(probed["video"]["height"]) == 1920

    blur_mp4 = make_synthetic_mp4(tmp_path / "all_blur.mp4", duration_sec=8.0, width=1280, height=720, blur_all=True)
    blur_out = tmp_path / "blur_short.mp4"
    blur_info = clip_job.probe(str(blur_mp4))
    clip_render.render_moment(str(blur_mp4), blur_info, _dummy_signals(blur_info.duration), Moment(0.0, 8.0, score=80.0, title="Blur"), [], str(blur_out), captions=False)
    probed_blur = probe_media(blur_out)
    assert int(probed_blur["video"]["width"]) == 1080
    assert int(probed_blur["video"]["height"]) == 1920


def test_r2_t2_off_language_visual_clip_blocked_on_locked_channel(tmp_path: Path, write_channels, monkeypatch):
    """R2-T2-03: Unknown-language visual clip is blocked when target_languages=['hi'] even if clip_rendered=True."""
    write_channels([_indianvlogs_entry()])
    monkeypatch.setattr(settings, "target_languages", '["hi"]')
    monkeypatch.setattr(settings, "allow_unknown_language", False)

    mp4 = tmp_path / "vis.mp4"
    mp4.write_bytes(b"mp4")
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        v = Video(
            source="youtube",
            source_id="eOeC1hU__is",
            source_url="https://youtube.com/watch?v=eOeC1hU__is",
            title="Pointing Out the Cliffs From the Boat",
            language="",
            transformed_path=str(mp4),
            duration_sec=22,
            status=VideoStatus.TRANSFORMED,
            channel_id=ch.id,
            meta={"clip_rendered": True, "clip_kind": "visual", "clip_score": 58.0},
        )
        db.add(v)
        db.flush()
        reason = pipeline.clip_gate_reason(v)
        assert reason is not None
        passed = autopilot._screen(db, [v], datetime.now(timezone.utc))
        assert passed == []
        assert v.status == VideoStatus.REJECTED


def test_r2_t2_overlong_and_spammy_or_blocked_title_rejected_or_sanitized(monkeypatch):
    """R2-T2-04: Overlong/HTML/refusal AI titles are sanitized or rejected; blocked terms trigger safety gate."""
    monkeypatch.setattr(ai, "available", lambda: True)

    monkeypatch.setattr(ai, "ask_json", lambda *a, **kw: {"title": "Title: <script>alert(1)</script> " + "X" * 120 + " #Shorts #viral"})
    t1, s1 = titles.styled_title("curiosity", "Clean Fallback Title", "Line")
    assert s1 == "plain"
    assert len(t1) <= 70
    assert "<" not in t1 and "#" not in t1

    monkeypatch.setattr(ai, "ask_json", lambda *a, **kw: {"title": "I cannot fulfill this request as an AI"})
    t2, s2 = titles.styled_title("question", "Clean Fallback Title", "Line")
    assert s2 == "plain"
    assert "cannot fulfill" not in t2.lower()

    v = Video(
        source="youtube", source_id="bad_term_1", source_url="u",
        title="Win big on 1xbet today", language="hi",
        meta={"clip_rendered": True},
    )
    reason = pipeline.clip_gate_reason(v)
    assert reason is not None and "1xbet" in reason


def test_r2_t2_empty_edit_words_still_populates_clip_opening_line():
    """R2-T2-05: clean_vlog EditPlan(words=[]) keeps opening_line empty while first_sentence_text succeeds."""
    words = [
        Word("Bhai", 2.1, 2.4),
        Word("aaj", 2.5, 2.8),
        Word("hadh", 2.9, 3.2),
        Word("ho", 3.3, 3.5),
        Word("gayi.", 3.6, 4.0),
    ]
    m = Moment(2.0, 22.0, score=90.0, title="Hadh Ho Gayi", kind="speech")
    plan = edit.EditPlan(words=[], segments=[(2.0, 22.0)], beats=[], speed=1.08)
    assert edit.opening_line(m, words, plan) == ""
    first = edit.first_sentence_text(m, words)
    assert first == "Bhai aaj hadh ho gayi."
    assert len(first) <= 120


def test_r2_t2_speech_moment_exceeding_32s_is_clamped_or_split(monkeypatch):
    """R2-T2-06: Speech proposals > 32s are clamped/rejected when max_sec=32."""
    monkeypatch.setattr(ai, "available", lambda: True)
    words = [Word("word." if i % 9 == 8 else "word", round(i * 0.5, 2), round(i * 0.5 + 0.4, 2), 0.95) for i in range(90)]
    monkeypatch.setattr(
        ai,
        "ask_json",
        lambda *a, **kw: {"clips": [{"first": 1, "last": 10, "title": "Way Too Long", "hook": "Too long", "score": 95}]},
    )
    sig = _dummy_signals(50.0)
    picked = moments.pick_speech(words, sig, count=1, min_sec=15, max_sec=32, target_sec=22)
    assert picked
    for m in picked:
        assert m.duration <= 32.0 + moments._EDGE_PADDING_SEC


# ==================== R3 BOUNDARIES ====================


def test_r3_t2_invalid_ambient_preset_rejected(tmp_path: Path, auth_client, write_channels):
    """R3-T2-01: Invalid/traversal ambient presets raise ValueError and /api/long/build rejects invalid formats with 400."""
    from app.longform import ambient

    for bad_preset in ("slowed_reverb_pop", "", "../evil"):
        out = tmp_path / "bad_preset.mp4"
        with pytest.raises(ValueError):
            ambient.generate_ambient_video(bad_preset, target_minutes=1, output_path=str(out))
        assert not out.exists()

    write_channels([_ambient_entry()])
    resp = auth_client.post("/api/long/build", json={"channel_slug": "ambient", "format": "invalid_format"})
    assert resp.status_code == 400


def test_r3_t2_zero_negative_and_out_of_range_duration_clamped_or_rejected(tmp_path: Path, write_channels, monkeypatch):
    """R3-T2-02: Zero/negative/NaN durations raise ValueError in ambient and clamp safely in enqueue_long_job."""
    from app.longform import ambient

    monkeypatch.setattr(ambient, "run_ffmpeg", lambda *a, **kw: pytest.fail("run_ffmpeg must not be invoked for invalid durations"))
    for bad_min in (0, -10, float("nan")):
        out = tmp_path / "bad_dur.mp4"
        with pytest.raises(ValueError):
            ambient.generate_ambient_video("lofi_chill", target_minutes=bad_min, output_path=str(out))
        assert not out.exists()

    write_channels([_ambient_entry()])
    jid = long_job.enqueue_long_job("ambient", format_kind="lofi_chill", theme="lofi_chill", target_minutes=9999)
    with session_scope() as db:
        job_row = db.get(LongJob, jid)
        assert 1 <= job_row.target_minutes <= 180


def test_r3_t2_unlinked_credentials_never_invoke_youtube_upload(tmp_path: Path, write_channels, monkeypatch):
    """R3-T2-03: Even with long_auto_publish=True and TRANSFORMED video, unlinked Channel #3 never uploads."""
    write_channels([_ambient_entry(enabled=True, long_auto_publish=True)])
    monkeypatch.setattr(settings, "use_mock", False)
    monkeypatch.setattr(settings, "enable_publish", True)

    mp4 = tmp_path / "amb_unlinked.mp4"
    mp4.write_bytes(b"mp4")
    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        ch3.client_secrets = ""
        ch3.token_path = str(tmp_path / "missing_token.json")
        v = Video(
            source="longform", source_id="amb_unlinked_1", source_url="",
            title="Night Rain", transformed_path=str(mp4), duration_sec=1800,
            status=VideoStatus.TRANSFORMED, channel_id=ch3.id, meta={"long_form": True},
        )
        db.add(v)
        db.flush()
        vid = v.id

    upload_called = False

    def _fail_if_called(*a, **kw):
        nonlocal upload_called
        upload_called = True
        return {"ok": True}

    monkeypatch.setattr(pipeline, "publish_task", _fail_if_called)
    res = long_job.long_autopilot_job()
    assert not upload_called
    assert "ambient" in res
    with session_scope() as db:
        assert db.get(Video, vid).status == VideoStatus.TRANSFORMED


def test_r3_t2_paused_or_held_channel3_blocks_autopilot_but_keeps_supply_safe(tmp_path: Path, write_channels, monkeypatch):
    """R3-T2-04: Paused or held Channel #3 blocks autopilot and leaves transformed videos untouched."""
    sec = tmp_path / "sec.json"
    tok = tmp_path / "tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    write_channels([_ambient_entry(enabled=True, client_secrets=str(sec), token_path=str(tok))])
    monkeypatch.setattr(settings, "use_mock", False)

    mp4 = tmp_path / "amb_paused.mp4"
    mp4.write_bytes(b"mp4")
    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        v = Video(
            source="longform", source_id="amb_p1", source_url="", title="Rain",
            transformed_path=str(mp4), duration_sec=1800, status=VideoStatus.TRANSFORMED,
            channel_id=ch3.id, meta={"long_form": True},
        )
        db.add(v)
        ch3_id = ch3.id

    safety.pause("ambient")
    res_paused = long_job.long_autopilot_job()
    assert res_paused["ambient"]["skipped"] == "paused or held"
    safety.resume("ambient")

    now = datetime.now(timezone.utc)
    with session_scope() as db:
        safety.set_hold(db, now, "quota", db.get(Channel, ch3_id))
    res_held = long_job.long_autopilot_job(now=now)
    assert res_held["ambient"]["skipped"] == "paused or held"
    assert mp4.is_file()


def test_r3_t2_quota_zero_blocks_long_autopilot_and_long_supply_respects_cap_zero(tmp_path: Path, write_channels, monkeypatch):
    """R3-T2-05: Exhausted QuotaUsage blocks autopilot; long_uploads_per_day=0 stops long_supply_job."""
    sec = tmp_path / "sec.json"
    tok = tmp_path / "tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    write_channels([_ambient_entry(enabled=True, client_secrets=str(sec), token_path=str(tok))])
    monkeypatch.setattr(settings, "use_mock", False)

    mp4 = tmp_path / "amb_q.mp4"
    mp4.write_bytes(b"mp4")
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        ch3.youtube_channel_id = "UC_AMB_QUOTA"
        q = quota.get_or_create_today(db, ch3.id, now=now)
        q.uploads = q.cap
        db.add(Video(
            source="longform", source_id="amb_q1", source_url="", title="Focus",
            transformed_path=str(mp4), duration_sec=1800, status=VideoStatus.TRANSFORMED,
            channel_id=ch3.id, meta={"long_form": True},
        ))

    res = long_job.long_autopilot_job(now=now)
    assert res["ambient"]["skipped"] == "daily quota exhausted"

    write_channels([_ambient_entry(enabled=True, long_uploads_per_day=0)])
    sup = long_job.long_supply_job()
    assert sup["skipped"] == "no channels configured for long-form"
    with session_scope() as db:
        assert db.execute(select(LongJob)).scalars().all() == []


def test_r3_t2_shorts_autopilot_and_cleanup_ignore_channel3_longform(tmp_path: Path, write_channels):
    """R3-T2-06: Shorts queues ignore Channel #3 long-form rows and cleanup protects PENDING Channel #3 files."""
    write_channels([_ambient_entry()])
    old_ts = datetime.now(timezone.utc) - timedelta(days=30)
    pend_mp4 = tmp_path / "amb_pending.mp4"
    pub_mp4 = tmp_path / "amb_published.mp4"
    pend_mp4.write_bytes(b"pending-mp4")
    pub_mp4.write_bytes(b"published-mp4")

    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        db.add(Video(
            source="longform", source_id="amb_pend", source_url="", title="Pending Ambient",
            local_path=str(pend_mp4), transformed_path=str(pend_mp4),
            status=VideoStatus.PENDING, scraped_at=old_ts, channel_id=ch3.id,
            meta={"long_form": True, "clip_rendered": False},
        ))
        db.add(Video(
            source="longform", source_id="amb_pub", source_url="", title="Published Ambient",
            local_path=str(pub_mp4), transformed_path=str(pub_mp4),
            status=VideoStatus.PUBLISHED, published_at=old_ts, youtube_video_id="YT_AMB_OLD",
            channel_id=ch3.id, meta={"long_form": True, "clip_rendered": False},
        ))
        assert autopilot.ready_clips(db) == []
        assert approval.awaiting_review(db) == []

    approval.prune_stale_shelf()
    scheduler.cleanup_job(dry_run=False)
    assert pend_mp4.is_file()
    assert not pub_mp4.exists()


# ==================== R4 BOUNDARIES ====================


def test_r4_t2_long_supply_respects_stock_cap_active_job_and_render_lock(tmp_path: Path, write_channels, monkeypatch):
    """R4-T2-01: Stock cap=2, active LongJob=1, and RENDER_LOCK busy guard."""
    write_channels([_indianvlogs_entry()])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)

    f1 = tmp_path / "s1.mp4"
    f2 = tmp_path / "s2.mp4"
    f1.write_bytes(b"1")
    f2.write_bytes(b"2")
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch_id = ch.id
        db.add(Video(source="longform", source_id="st1", source_url="", title="S1", transformed_path=str(f1), status=VideoStatus.PENDING, channel_id=ch_id, meta={"long_form": True}))
        db.add(Video(source="longform", source_id="st2", source_url="", title="S2", transformed_path=str(f2), status=VideoStatus.TRANSFORMED, channel_id=ch_id, meta={"long_form": True}))

    res_cap = long_job.long_supply_job()
    assert res_cap["indianvlogs"]["queued"] is False
    assert res_cap["indianvlogs"]["stock"] == 2

    with session_scope() as db:
        for row in db.execute(select(Video)).scalars().all():
            db.delete(row)
        db.add(LongJob(channel_id=ch_id, format="best_of", status="queued"))

    res_act = long_job.long_supply_job()
    assert res_act["indianvlogs"]["queued"] is False
    assert res_act["indianvlogs"]["active_jobs"] == 1

    assert clip_job.RENDER_LOCK.acquire(blocking=False)
    try:
        assert long_job.run_next_long_job() == {"busy": True}
    finally:
        clip_job.RENDER_LOCK.release()


def test_r4_t2_long_panel_rejects_empty_metadata_oversize_thumb_and_published_mutations(tmp_path: Path, auth_client, write_channels):
    """R4-T2-02: /api/long/* validates blank metadata (400), bad/oversize thumbs (400/413), and published immutability (409)."""
    write_channels([_indianvlogs_entry()])
    p1 = tmp_path / "p1.mp4"
    p2 = tmp_path / "p2.mp4"
    p1.write_bytes(b"mp4")
    p2.write_bytes(b"mp4")
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        v_pend = Video(source="longform", source_id="pend_mut", source_url="", title="Pend", transformed_path=str(p1), status=VideoStatus.PENDING, channel_id=ch.id, meta={"long_form": True})
        v_pub = Video(source="longform", source_id="pub_mut", source_url="", title="Pub", transformed_path=str(p2), status=VideoStatus.PUBLISHED, youtube_video_id="yt123", channel_id=ch.id, meta={"long_form": True})
        db.add_all([v_pend, v_pub])
        db.flush()
        pend_id, pub_id = v_pend.id, v_pub.id

    assert auth_client.post(f"/api/long/{pend_id}/metadata", json={"title": "   "}).status_code == 400
    assert auth_client.post(f"/api/long/{pend_id}/metadata", json={"description": "   "}).status_code == 400
    assert auth_client.post(f"/api/long/{pend_id}/thumbnail/upload", content=b"not-an-image").status_code == 400
    assert auth_client.post(f"/api/long/{pend_id}/thumbnail/upload", content=b"x" * (10 * 1024 * 1024 + 64)).status_code == 413

    assert auth_client.post(f"/api/long/{pub_id}/reject").status_code == 409
    assert auth_client.post(f"/api/long/{pub_id}/approve", json={}).status_code == 409
    with session_scope() as db:
        assert db.get(Video, pub_id).status == VideoStatus.PUBLISHED


def test_r4_t2_every_review_app_api_route_has_guard_and_zero_500_on_bad_ids(anon_client, auth_client):
    """R4-T2-03: Every non-root APIRoute enforces _guard, missing IDs return 404, and logout revokes access."""
    api_routes = [r for r in review_mod.review_app.routes if isinstance(r, APIRoute) and r.path != "/"]
    assert api_routes
    for route in api_routes:
        assert "_guard" in route.endpoint.__code__.co_names, f"Missing _guard in {route.path}"
        if "GET" in route.methods:
            test_path = route.path.replace("{video_id}", "missing").replace("{job_id}", "missing").replace("{variant_id}", "v1").replace("{slug}", "missing")
            assert anon_client.get(test_path).status_code == 401

    for url in ("/api/long/missing/hero", "/api/long/missing/thumb/v1"):
        assert auth_client.get(url).status_code == 404
    for url in ("/api/long/missing/approve", "/api/long/missing/reject", "/api/long/jobs/missing/retry"):
        assert auth_client.post(url, json={}).status_code == 404

    assert auth_client.post("/api/logout").status_code == 200
    assert auth_client.get("/api/long/videos").status_code == 401


def test_r4_t2_doctor_and_final_audit_offline_contracts_and_no_secret_leaks(tmp_path: Path, monkeypatch):
    """R4-T2-04: doctor & final_audit report boolean presence only and zero leaked secret values."""
    secrets_map = {
        "REVIEW_TOKEN": "tok_e2e_secret_111",
        "SARVAM_API_KEY": "sarvam_e2e_secret_222",
        "DEEPGRAM_API_KEY": "dg_e2e_secret_333",
        "TELEGRAM_BOT_TOKEN": "tg_e2e_secret_444",
    }
    for k, v in secrets_map.items():
        monkeypatch.setenv(k, v)

    doc = doctor.run_doctor(fix=False, root=tmp_path)
    doc_json = json.dumps(doc)
    assert isinstance(doc["env"]["review_token_set"], bool)
    assert all(isinstance(val, bool) for val in doc["env"]["optional_keys"].values())
    for val in secrets_map.values():
        assert val not in doc_json

    aud = final_audit.run_audit(live=False)
    aud_json = json.dumps(aud)
    assert aud["platforms_ok"] is True
    assert aud["surface_ok"] is True
    assert aud["surface_checks"]["review_routes_guarded"] is True
    for val in secrets_map.values():
        assert val not in aud_json


def test_r4_t2_scheduler_cleanup_protects_pending_longform_and_reclaims_expired_published(tmp_path: Path, write_channels):
    """R4-T2-05: cleanup_job preserves PENDING long-form MP4/hero/thumbs while reclaiming expired PUBLISHED assets."""
    write_channels([_indianvlogs_entry()])
    old_ts = datetime.now(timezone.utc) - timedelta(days=30)

    pend_dir = tmp_path / "pend_assets"
    pub_dir = tmp_path / "pub_assets"
    pend_dir.mkdir()
    pub_dir.mkdir()
    pend_mp4 = pend_dir / "pend.mp4"
    pend_hero = pend_dir / "hero_raw.jpg"
    pend_t1 = pend_dir / "thumb_v1.jpg"
    pub_mp4 = pub_dir / "pub.mp4"
    pub_hero = pub_dir / "hero_raw.jpg"
    pub_t1 = pub_dir / "thumb_v1.jpg"
    for p in (pend_mp4, pend_hero, pend_t1, pub_mp4, pub_hero, pub_t1):
        p.write_bytes(make_valid_jpeg_bytes() if p.suffix == ".jpg" else b"mp4-bytes")

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        db.add(Video(
            source="longform", source_id="cl_pend", source_url="", title="Keep Pending",
            local_path=str(pend_mp4), transformed_path=str(pend_mp4),
            status=VideoStatus.PENDING, scraped_at=old_ts, channel_id=ch.id,
            meta={"long_form": True, "hero_frame": str(pend_hero), "thumbnails": [{"id": "v1", "path": str(pend_t1)}]},
        ))
        db.add(Video(
            source="longform", source_id="cl_pub", source_url="", title="Expire Pub",
            local_path=str(pub_mp4), transformed_path=str(pub_mp4),
            status=VideoStatus.PUBLISHED, published_at=old_ts, youtube_video_id="YT_OLD_1", channel_id=ch.id,
            meta={"long_form": True, "hero_frame": str(pub_hero), "thumbnails": [{"id": "v1", "path": str(pub_t1)}]},
        ))

    scheduler.cleanup_job(dry_run=True)
    assert all(p.is_file() for p in (pend_mp4, pend_hero, pend_t1, pub_mp4, pub_hero, pub_t1))

    scheduler.cleanup_job(dry_run=False)
    assert all(p.is_file() for p in (pend_mp4, pend_hero, pend_t1))
    assert not any(p.exists() for p in (pub_mp4, pub_hero, pub_t1))


def test_r4_t2_long_autopilot_reserves_daily_cap_across_channel_timezone(tmp_path: Path, write_channels, monkeypatch):
    """R4-T2-06: long_autopilot_job computes daily cap in channel_zone(ch) (Asia/Kolkata midnight)."""
    sec = tmp_path / "sec.json"
    tok = tmp_path / "tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    write_channels([_indianvlogs_entry(timezone="Asia/Kolkata", long_uploads_per_day=1, client_secrets=str(sec), token_path=str(tok))])
    monkeypatch.setattr(settings, "use_mock", False)

    mp4 = tmp_path / "tz_long.mp4"
    mp4.write_bytes(b"mp4")
    # 19:00 UTC on May 1 == 00:30 IST on May 2
    pub_at = datetime(2026, 5, 1, 19, 0, 0, tzinfo=timezone.utc)
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        db.add(Video(
            source="longform", source_id="tz_pub_1", source_url="", title="Posted IST",
            status=VideoStatus.PUBLISHED, published_at=pub_at, youtube_video_id="YT_IST_1",
            channel_id=ch.id, meta={"long_form": True},
        ))
        v_ready = Video(
            source="longform", source_id="tz_ready_1", source_url="", title="Ready IST",
            transformed_path=str(mp4), status=VideoStatus.TRANSFORMED,
            channel_id=ch.id, meta={"long_form": True, "owner_approved": "2026-05-01T00:00:00Z"},
        )
        db.add(v_ready)
        db.flush()
        ready_id = v_ready.id

    pub_calls: list[str] = []
    monkeypatch.setattr(pipeline, "publish_task", lambda vid, meta=None: pub_calls.append(vid) or {"ok": True, "youtube_id": "YT_IST_2"})

    # 20:00 UTC May 1 == 01:30 IST May 2 (same IST calendar day -> blocked by daily_cap)
    same_ist_day = datetime(2026, 5, 1, 20, 0, 0, tzinfo=timezone.utc)
    res_same = long_job.long_autopilot_job(now=same_ist_day)
    assert res_same["indianvlogs"]["skipped"] == "daily_cap"
    assert pub_calls == []

    # 19:00 UTC May 2 == 00:30 IST May 3 (next IST calendar day -> allowed)
    next_ist_day = datetime(2026, 5, 2, 19, 0, 0, tzinfo=timezone.utc)
    long_job.long_autopilot_job(now=next_ist_day)
    assert pub_calls == [ready_id]


def test_r4_t2_e2e_conftest_refuses_non_memory_db_and_repo_paths(tmp_path: Path, monkeypatch):
    """R4-T2-07: tests/e2e/conftest.py fails closed when DB is non-memory, --confcutdir bypasses root conftest, or paths sit inside PROJECT_ROOT."""
    # 1) Fresh subprocess importing app.db BEFORE pytest.main (using tmp_path-only DATABASE_URL)
    forbidden_db1 = tmp_path / "should_never_exist.db"
    env1 = os.environ.copy()
    env1["DOOMSCROLLER_ENV_FILE"] = ""
    env1["DATABASE_URL"] = f"sqlite:///{forbidden_db1.as_posix()}"
    env1["CONTENT_DIR"] = str(tmp_path / "sub_content_1")
    env1["CHANNELS_FILE"] = str(tmp_path / "sub_channels_1.json")
    env1["DOOMSCROLLER_LOG_DIR"] = str(tmp_path / "sub_logs_1")
    env1["USE_MOCK"] = "true"
    basetemp1 = (tmp_path / "bt1").as_posix()

    proc1 = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app.db, pytest; "
                f"raise SystemExit(pytest.main(['--collect-only', '-q', '--basetemp={basetemp1}', "
                "'tests/e2e/test_tier1_features.py']))"
            ),
        ],
        cwd=str(PROJECT_ROOT),
        env=env1,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    out1 = (proc1.stdout or "") + "\n" + (proc1.stderr or "")
    assert proc1.returncode != 0, out1
    assert "Refusing to run E2E tests" in out1
    assert not forbidden_db1.exists()

    # 2) Fresh subprocess with --confcutdir=tests/e2e (bypasses tests/conftest.py; safe tmp_path-only env)
    forbidden_db2 = tmp_path / "should_never_exist2.db"
    env2 = os.environ.copy()
    env2["DOOMSCROLLER_ENV_FILE"] = str(tmp_path / "nonexistent.env")
    env2["DATABASE_URL"] = f"sqlite:///{forbidden_db2.as_posix()}"
    env2["CONTENT_DIR"] = str(tmp_path / "sub_content_2")
    env2["CHANNELS_FILE"] = str(tmp_path / "sub_channels_2.json")
    env2["DOOMSCROLLER_LOG_DIR"] = str(tmp_path / "sub_logs_2")
    env2["USE_MOCK"] = "true"
    basetemp2 = str(tmp_path / "bt2")

    proc2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--confcutdir=tests/e2e",
            "--collect-only",
            "-q",
            f"--basetemp={basetemp2}",
            "tests/e2e/test_tier1_features.py",
        ],
        cwd=str(PROJECT_ROOT),
        env=env2,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    out2 = (proc2.stdout or "") + "\n" + (proc2.stderr or "")
    assert proc2.returncode != 0, out2
    assert "Refusing to run E2E tests" in out2
    assert not forbidden_db2.exists()

    # 3) Direct _assert_e2e_isolated boundary checks (tmp_path / content_dir / channels_file / LOG_DIR / USE_MOCK)
    with pytest.raises(RuntimeError, match="tmp_path="):
        _assert_e2e_isolated(PROJECT_ROOT / "_bad_e2e_tmp")

    with monkeypatch.context() as mp:
        mp.setattr(settings, "content_dir", "_e2e_must_not_create_content")
        with pytest.raises(RuntimeError, match="content_dir="):
            _assert_e2e_isolated()
        assert not (PROJECT_ROOT / "_e2e_must_not_create_content").exists()

    with monkeypatch.context() as mp:
        mp.setattr(settings, "channels_file", "channels.json")
        with pytest.raises(RuntimeError, match="channels_file="):
            _assert_e2e_isolated()

    with monkeypatch.context() as mp:
        mp.setattr(logging_config, "LOG_DIR", Path("logs"))
        with pytest.raises(RuntimeError, match="LOG_DIR="):
            _assert_e2e_isolated()

    with monkeypatch.context() as mp:
        mp.setenv("USE_MOCK", "false")
        with pytest.raises(RuntimeError, match="USE_MOCK="):
            _assert_e2e_isolated()
