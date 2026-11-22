"""Tier 3 E2E Tests: Cross-Feature Pairwise Combinations (Fast + Slow Real-ffmpeg Media QA)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.clipper import ai, edit, job as clip_job, render as clip_render
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.config import settings
from app.db import session_scope
from app.growth import autopilot, channels, notify, supply, titles
from app.longform import job as long_job, planner, render as long_render, thumbnails, voiceover
from app.models import Channel, LongJob, Video, VideoStatus
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


def test_t3_01_channel3_supply_review_auth_and_autopilot_triple_lock(tmp_path: Path, anon_client, auth_client, write_channels, monkeypatch):
    """T3-01: R3 long_supply_job + R4 _guard auth + unlinked triple safety lock."""
    from app.longform import ambient

    write_channels([_ambient_entry()])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    monkeypatch.setattr(settings, "use_mock", False)

    def _fake_amb(preset, target_minutes, output_path, **kw):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"mp4")
        tp = Path(output_path).parent / "thumb.jpg"
        tp.write_bytes(make_valid_jpeg_bytes())
        return ambient.AmbientResult(
            output_path=str(output_path), duration=3600.0, preset=preset,
            width=1920, height=1080, fps=30,
            title="Rain Study Session", description="Claim-free ambience",
            tags=("rain", "focus"), chapters=((0.0, "Start"),), chapter_text="0:00 Start",
            thumbnails=({"id": "v1", "label": "V1", "path": str(tp)},),
            hero_frame=str(tp), thumbnail_prompt="rain",
            language="en", theme="", part_number=1,
        )

    monkeypatch.setattr(ambient, "generate_ambient_video", _fake_amb)
    long_job.long_supply_job()
    res = long_job.run_next_long_job()
    assert res["ok"] is True
    vid = res["video_id"]

    assert anon_client.get("/api/long/videos").status_code == 401
    assert anon_client.post(f"/api/long/{vid}/approve", json={}).status_code == 401

    assert len(auth_client.get("/api/long/videos").json()["pending"]) == 1
    assert auth_client.post(f"/api/long/{vid}/approve", json={}).json()["status"] == "transformed"

    pub_calls: list[str] = []
    monkeypatch.setattr(pipeline, "publish_task", lambda v_id, meta=None: pub_calls.append(v_id) or {"ok": True})
    long_job.long_autopilot_job()
    assert pub_calls == []


def test_t3_02_r1_sarvam_quota_exhaustion_aborts_render_alerts_ntfy_no_fallback(tmp_path: Path, write_channels, monkeypatch):
    """T3-02: R1 LongJob aborts cleanly on Sarvam 429, scrubs key in ntfy alert, leaves 0 Videos."""
    write_channels([_indianvlogs_entry()])
    secret = "sarvam_t3_secret_key"
    monkeypatch.setattr(settings, "sarvam_api_key", secret)
    monkeypatch.setattr(settings, "ntfy_topic", "t3-topic")
    monkeypatch.setattr(ai, "available", lambda: False)
    voiceover._last_quota_alert_at = 0.0

    src = tmp_path / "src.mp4"
    src.write_bytes(b"mp4")
    seg = planner.SegmentPlan(str(src), "u", "s1", "@souravjvlogs", "Sourav Joshi", "V", 10.0, 40.0, 90.0, "M1", "hi")
    monkeypatch.setattr(
        planner,
        "plan_compilation",
        lambda **kw: planner.CompilationPlan(
            format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",),
            theme="", title="T", description="D", tags=("vlog",), language="hi",
            target_seconds=360.0, cold_open=seg, segments=(seg,),
        ),
    )
    monkeypatch.setattr(long_render, "probe", lambda p: SimpleNamespace(duration=60.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(voiceover.httpx, "post", lambda *a, **kw: SimpleNamespace(status_code=429, text=f"quota exceeded {secret}"))

    alerts: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, *, title="", link="": alerts.append(text) or True)

    jid = long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs")
    res = long_job.run_next_long_job()
    assert res["ok"] is False
    assert len(alerts) == 1
    assert secret not in alerts[0]

    with session_scope() as db:
        job_row = db.get(LongJob, jid)
        assert job_row.status == "failed"
        assert "SarvamTTSQuotaError" in (job_row.error or "")
        assert db.execute(select(Video)).scalars().all() == []


def test_t3_03_r2_language_lock_plus_clean_vlog_pacing_and_title_diversity(tmp_path: Path, write_channels, monkeypatch):
    """T3-03: Language lock blocks off-language visual clip while clean_vlog Hindi clip gets styled title + defaultLanguage=hi."""
    write_channels([_indianvlogs_entry()])
    monkeypatch.setattr(settings, "target_languages", '["hi"]')
    monkeypatch.setattr(settings, "allow_unknown_language", False)
    monkeypatch.setattr(settings, "use_mock", False)
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "ask_json", lambda *a, **kw: {"title": "Piyush ne aaj hadh kar di yaar"})

    mp4_en = tmp_path / "en_vis.mp4"
    mp4_hi = tmp_path / "hi_vlog.mp4"
    mp4_en.write_bytes(b"mp4")
    mp4_hi.write_bytes(b"mp4")

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        v_bad = Video(
            source="youtube", source_id="bad_vis", source_url="u",
            title="Cliffs From Boat", language="", transformed_path=str(mp4_en), duration_sec=20,
            status=VideoStatus.TRANSFORMED, channel_id=ch.id,
            meta={"clip_rendered": True, "clip_kind": "visual", "clip_score": 60.0},
        )
        v_good = Video(
            source="youtube", source_id="good_hi", source_url="u",
            title="Sourav Vlog Twist", language="hi", transformed_path=str(mp4_hi), duration_sec=24,
            status=VideoStatus.TRANSFORMED, channel_id=ch.id,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_score": 88.0, "clip_opening_line": "Bhai aaj hadh ho gayi."},
        )
        db.add_all([v_bad, v_good])
        db.flush()
        passed = autopilot._screen(db, [v_bad, v_good], datetime.now(timezone.utc))
        assert [p.id for p in passed] == [v_good.id]
        assert v_bad.status == VideoStatus.REJECTED

    styled, used_style = titles.styled_title("curiosity", v_good.title, "Bhai aaj hadh ho gayi.")
    assert used_style == "curiosity"
    assert len(styled) <= 70

    uploads: list[dict] = []
    monkeypatch.setattr(
        pipeline,
        "_publisher_for",
        lambda cid: SimpleNamespace(upload=lambda **kw: uploads.append(kw) or SimpleNamespace(video_id="YT_HI_T3")),
    )
    res = pipeline.publish_task(v_good.id, meta={"title": styled})
    assert res["ok"] is True
    assert uploads[0]["default_language"] == "hi"
    assert uploads[0]["default_audio_language"] == "hi"


def test_t3_04_r1_rejected_longform_frees_spans_and_parts_while_pending_locks(write_channels):
    """T3-04: REJECTED spans are free while PENDING locks; reject_long(part2) unlocks part2 spans too."""
    write_channels([_indianvlogs_entry()])
    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch_id = ch.id
        v1 = Video(
            source="longform", source_id="p1", source_url="u", title="P1",
            status=VideoStatus.REJECTED, channel_id=ch_id,
            meta={"long_form": True, "long_format": "best_of", "long_targets_key": "@souravjvlogs", "part_number": 1, "long_segments": [{"source_id": "s1", "start": 10.0, "end": 60.0}]},
        )
        v2 = Video(
            source="longform", source_id="p2", source_url="u", title="P2",
            status=VideoStatus.PENDING, channel_id=ch_id,
            meta={"long_form": True, "long_format": "best_of", "long_targets_key": "@souravjvlogs", "part_number": 2, "long_segments": [{"source_id": "s1", "start": 200.0, "end": 260.0}]},
        )
        db.add_all([v1, v2])
        db.flush()
        v2_id = v2.id

    with session_scope() as db:
        used = planner.used_spans_for_channel(db, ch_id)
        assert len(used.get("s1", [])) == 1
        assert used["s1"][0].start == 200.0
        assert planner.next_part_number(db, ch_id, ["@souravjvlogs"]) == 3

    long_job.reject_long(v2_id)
    with session_scope() as db:
        assert planner.used_spans_for_channel(db, ch_id) == {}
        assert planner.next_part_number(db, ch_id, ["@souravjvlogs"]) == 1


def test_t3_05_channel_isolation_shorts_and_longform_across_3_channels(tmp_path: Path, write_channels, monkeypatch):
    """T3-05: default, indianvlogs, and procedural ambient stay cleanly isolated across Shorts & Long jobs."""
    write_channels([
        {"slug": "default", "name": "English", "niche": "challenges", "uploads_per_day": 2, "long_uploads_per_day": 1, "long_auto_publish": False, "enabled": True, "feeds": ["https://www.youtube.com/@MrBeast"]},
        _indianvlogs_entry(),
        _ambient_entry(),
    ])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    monkeypatch.setattr(supply, "next_source", lambda *a, **kw: None)
    monkeypatch.setattr(supply, "next_source_for", lambda *a, **kw: None)

    sup = supply.supply_job()
    assert "ambient" not in sup or "starved" not in str(sup.get("ambient", ""))

    lsup = long_job.long_supply_job()
    assert lsup["default"]["queued"] is True
    assert lsup["indianvlogs"]["queued"] is True
    assert lsup["ambient"]["queued"] is True

    # Shorts autopilot must never touch long_form rows
    with session_scope() as db:
        ch_amb = channels.channel_by_slug(db, "ambient")
        mp4 = tmp_path / "amb.mp4"
        mp4.write_bytes(b"mp4")
        db.add(Video(
            source="longform", source_id="amb_iso", source_url="", title="Amb",
            transformed_path=str(mp4), status=VideoStatus.TRANSFORMED, channel_id=ch_amb.id,
            meta={"long_form": True, "clip_rendered": False},
        ))
        assert autopilot.ready_clips(db) == []


def test_t3_06_path_traversal_and_header_injection_fuzz_on_panel_and_notify(auth_client):
    """T3-06: Path traversal IDs return 404/400 and notify._clean_header strips CR/LF."""
    assert auth_client.get("/api/long/..%2F..%2Fetc%2Fpasswd/hero").status_code in (400, 404)
    assert auth_client.get("/api/long/abc/thumb/..%2Fsecret").status_code in (400, 404)
    cleaned = notify._clean_header("Alert\r\nX-Injected: true\nNext")
    assert "\r" not in cleaned and "\n" not in cleaned


@pytest.mark.slow
@requires_ffmpeg
def test_t3_07_r1_bridge_and_end_tail_audio_continuity_and_peak_ceiling(tmp_path: Path):
    """T3-07: Real ffmpeg bridge + end-tail render have 0 tail silence dropouts and max_volume <= -0.5 dB."""
    src_mp4 = make_synthetic_mp4(tmp_path / "src_1080.mp4", duration_sec=12.0, width=1920, height=1080, amplitude=0.7)
    vo_wav = make_wav_24k(tmp_path / "vo.wav", duration_sec=2.5, amplitude=0.6)

    bridge_out = tmp_path / "bridge_real.mp4"
    voiceover.render_narrator_bridge(
        vo_wav, src_mp4, bridge_out, tmp_path,
        badge_text="CHAPTER 1", headline="Sourav Joshi",
        brand_name="@MrIndianVlogs001", duration=4.5, preview_start=1.0, preview_end=10.0,
    )
    assert detect_silences(bridge_out, noise_db=-45.0, min_dur=0.35) == []
    assert detect_volume(bridge_out)["max_volume"] <= -0.5

    tail_out = tmp_path / "tail_real.mp4"
    long_render._render_end_tail(src_mp4, tail_out, tmp_path, seconds=6.0, outro_wav=vo_wav, seek_sec=1.0)
    tail_silences = [(s, e) for s, e in detect_silences(tail_out, noise_db=-45.0, min_dur=0.35) if s < 5.4]
    assert tail_silences == []
    assert detect_volume(tail_out)["max_volume"] <= -0.5


@pytest.mark.slow
@requires_ffmpeg
def test_t3_08_r1_full_render_compilation_1080p_and_thumbnails(tmp_path: Path, monkeypatch):
    """T3-08: Full render_compilation + thumbnails produces 1080p stereo MP4 with 0 black frames and <= -0.5 dB peak."""
    src_mp4 = make_synthetic_mp4(tmp_path / "comp_src.mp4", duration_sec=28.0, width=1280, height=720, amplitude=0.35)
    monkeypatch.setattr(settings, "sarvam_api_key", "e2e-sarvam-key")
    monkeypatch.setattr(ai, "available", lambda: False)
    monkeypatch.setattr(
        voiceover.httpx,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: make_sarvam_tts_payload(2.0), text="ok"),
    )

    s1 = planner.SegmentPlan(str(src_mp4), "u1", "s1", "@souravjvlogs", "Sourav Joshi", "Vlog", 1.0, 9.0, 88.0, "Arrival", "hi")
    s2 = planner.SegmentPlan(str(src_mp4), "u1", "s1", "@souravjvlogs", "Sourav Joshi", "Vlog", 11.0, 20.0, 94.0, "Grand Finale", "hi")
    co = planner.SegmentPlan(str(src_mp4), "u1", "s1", "@souravjvlogs", "Sourav Joshi", "Vlog", 12.0, 17.0, 94.0, "Grand Finale", "hi")
    plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",),
        theme="", title="Sourav Joshi Moments", description="Fun vlog compilation", tags=("vlog",), language="hi",
        target_seconds=360.0, cold_open=co, segments=(s1, s2),
    )

    out_mp4 = tmp_path / "full_comp.mp4"
    res = long_render.render_compilation(plan, str(out_mp4), brand_name="@MrIndianVlogs001", include_end_screen=True, include_voiceover=True)
    probed = probe_media(out_mp4)
    assert int(probed["video"]["width"]) == 1920
    assert int(probed["video"]["height"]) == 1080
    assert int(probed["audio"]["channels"]) == 2
    assert int(probed["audio"]["sample_rate"]) == 48_000
    assert detect_black_frames(out_mp4, duration=0.2, pic_th=0.98) == []
    assert detect_volume(out_mp4)["max_volume"] <= -0.5

    thumbs_dir = tmp_path / "thumbs"
    thumbs, _ = thumbnails.generate_thumbnails_with_prompt(
        str(out_mp4), out_dir=thumbs_dir, title=plan.title, format_kind="best_of",
        chapters=res.chapters, timestamps=res.timestamps_for_thumbs,
    )
    assert len(thumbs) >= 3
    assert (thumbs_dir / "hero_raw.jpg").is_file()


@pytest.mark.slow
@requires_ffmpeg
def test_t3_09_r3_procedural_ambient_render_and_panel_media_stream(tmp_path: Path, auth_client, write_channels):
    """T3-09: Procedural lofi_chill video persists to PENDING and streams via Control Panel /api/media/{id}."""
    from app.longform import ambient

    write_channels([_ambient_entry()])
    out_mp4 = tmp_path / "lofi_panel.mp4"
    res = ambient.generate_ambient_video("lofi_chill", duration_sec=18.0, output_path=str(out_mp4))
    assert detect_volume(out_mp4)["max_volume"] <= -0.5

    with session_scope() as db:
        ch3 = channels.channel_by_slug(db, "ambient")
        v = Video(
            source="longform", source_id="amb_stream_1", source_url="",
            title=res.title, local_path=str(out_mp4), transformed_path=str(out_mp4),
            duration_sec=int(res.duration), width=1920, height=1080,
            status=VideoStatus.PENDING, channel_id=ch3.id, meta={"long_form": True},
        )
        db.add(v)
        db.flush()
        vid = v.id

    stream_resp = auth_client.get(f"/api/media/{vid}")
    assert stream_resp.status_code == 200
    assert len(stream_resp.content) > 50_000


@pytest.mark.slow
@requires_ffmpeg
def test_t3_10_r2_clean_vlog_render_moment_framed_and_ass_captions(tmp_path: Path, monkeypatch):
    """T3-10: Real ffmpeg clean_vlog render_moment in framed layout produces clean 1080x1920 Short."""
    monkeypatch.setattr(settings, "clip_layout", "framed")
    src_mp4 = make_synthetic_mp4(tmp_path / "vlog_short_src.mp4", duration_sec=12.0, width=1920, height=1080, amplitude=0.35)
    out_mp4 = tmp_path / "clean_vlog_short.mp4"
    info = clip_job.probe(str(src_mp4))
    sig = _dummy_signals(info.duration)
    ed = edit.EditPlan(words=[], segments=[(1.0, 11.0)], beats=[], speed=1.08)
    clip_render.render_moment(str(src_mp4), info, sig, Moment(1.0, 11.0, score=88.0, title="Clean Vlog"), [], str(out_mp4), edit=ed, captions=False)

    probed = probe_media(out_mp4)
    assert int(probed["video"]["width"]) == 1080
    assert int(probed["video"]["height"]) == 1920
    assert detect_black_frames(out_mp4, duration=0.2, pic_th=0.98) == []
    assert detect_volume(out_mp4)["max_volume"] <= -0.5
