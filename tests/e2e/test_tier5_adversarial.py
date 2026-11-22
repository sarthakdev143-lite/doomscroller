"""Tier 5 E2E Tests: White-Box Adversarial Regression Suite for Fixes (#1-#8)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import scheduler
from app.clipper import ai, edit, ingest, job as clip_job, moments, render as clip_render, transcribe
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.clipper.transcribe import Word
from app.config import settings
from app.db import session_scope
from app.growth import channels
from app.longform import ambient, job as long_job, planner, render as long_render, thumbnails, voiceover
from app.models import LongJob, Video, VideoStatus
from app.publishers import MockPublisher
from tasks import pipeline
from tests.e2e.conftest import make_valid_jpeg_bytes


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


def _default_entry(**overrides) -> dict:
    base = {
        "slug": "default",
        "name": "English Main",
        "niche": "viral challenges",
        "brand_name": "@DefaultBrand",
        "uploads_per_day": 3,
        "long_uploads_per_day": 1,
        "long_auto_publish": False,
        "timezone": "UTC",
        "require_approval": True,
        "enabled": True,
        "feeds": ["https://www.youtube.com/@MrBeast"],
        "meta": {"target_languages": ["en"]},
    }
    base.update(overrides)
    return base


def _ambient_entry(**overrides) -> dict:
    base = {
        "slug": "ambient",
        "name": "Midnight Rain & Lofi",
        "niche": "lofi ambient rain focus music",
        "brand_name": "@MidnightRainLofi",
        "uploads_per_day": 0,
        "long_uploads_per_day": 3,
        "long_auto_publish": False,
        "timezone": "UTC",
        "require_approval": True,
        "enabled": True,
        "meta": {
            "mode": "procedural_ambient",
            "presets": ["lofi_chill", "rain_focus", "night_ambient", "deep_study"],
            "target_minutes": 30,
            "generate_while_paused": True,
        },
        "feeds": [],
    }
    base.update(overrides)
    return base


def _dummy_signals(duration: float) -> Signals:
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


def test_t5_longform_auto_publish_override_and_default_mock_guard(
    tmp_path: Path, auth_client, write_channels, monkeypatch
):
    """T5-01: auto_publish=True cannot bypass long_auto_publish=False or enabled=False, and default blocks MockPublisher when use_mock=False."""
    sec = tmp_path / "sec.json"
    tok = tmp_path / "tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    write_channels([
        _default_entry(),
        _indianvlogs_entry(client_secrets=str(sec), token_path=str(tok), long_auto_publish=False, enabled=True),
        {
            "slug": "disabled_ch",
            "name": "Disabled Channel",
            "niche": "comedy",
            "uploads_per_day": 0,
            "long_uploads_per_day": 1,
            "long_auto_publish": True,
            "enabled": False,
            "feeds": ["https://www.youtube.com/@mkbhd"],
        },
    ])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    monkeypatch.setattr(long_job.notify, "send_long_ready", lambda *a, **kw: True)

    seg = planner.SegmentPlan("s.mp4", "u", "s1", "@souravjvlogs", "Sourav", "V", 10.0, 50.0, 90.0, "M1", "hi")
    monkeypatch.setattr(
        planner,
        "plan_compilation",
        lambda **kw: planner.CompilationPlan(
            format_kind="best_of",
            channel_slug=kw.get("channel_slug", "indianvlogs"),
            part_number=1,
            targets=("@souravjvlogs",),
            theme="",
            title="Compilation Build",
            description="Highlights",
            tags=("vlog",),
            language="hi",
            target_seconds=480.0,
            cold_open=seg,
            segments=(seg,),
        ),
    )

    def _fake_render(plan, out_path, **kw):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"MP4" * 200)
        return SimpleNamespace(
            output_path=str(out_path),
            duration=400.0,
            chapters=((0.0, "Intro"),),
            chapter_text="0:00 Intro",
            timestamps_for_thumbs=(15.0,),
        )

    monkeypatch.setattr(long_render, "render_compilation", _fake_render)
    monkeypatch.setattr(thumbnails, "generate_thumbnails_with_prompt", lambda *a, **kw: ([], ""))

    # 1. POST /api/long/build with auto_publish=True on indianvlogs (long_auto_publish=False)
    r1 = auth_client.post(
        "/api/long/build",
        json={"channel_slug": "indianvlogs", "format": "best_of", "target": "@souravjvlogs", "auto_publish": True},
    )
    assert r1.status_code == 200
    jid1 = r1.json()["job_id"]

    # 2. POST /api/long/build with auto_publish=True on disabled channel (even with long_auto_publish=True)
    r2 = auth_client.post(
        "/api/long/build",
        json={"channel_slug": "disabled_ch", "format": "best_of", "target": "@mkbhd", "auto_publish": True},
    )
    assert r2.status_code == 200
    jid2 = r2.json()["job_id"]

    with session_scope() as db:
        assert db.get(LongJob, jid1).auto_publish is False
        assert db.get(LongJob, jid2).auto_publish is False

    res1 = long_job.run_next_long_job()
    res2 = long_job.run_next_long_job()
    assert res1["ok"] is True and res2["ok"] is True

    with session_scope() as db:
        v1 = db.get(Video, res1["video_id"])
        v2 = db.get(Video, res2["video_id"])
        assert v1.status == VideoStatus.PENDING
        assert v2.status == VideoStatus.PENDING

    # 3. Verify autopilot skips unapproved TRANSFORMED row on long_auto_publish=False channel
    with session_scope() as db:
        v1 = db.get(Video, res1["video_id"])
        v1.status = VideoStatus.TRANSFORMED
        v1.transformed_at = datetime.now(timezone.utc)

        # Also plant an owner-approved TRANSFORMED video on default channel
        def_ch = channels.channel_by_slug(db, "default")
        def_mp4 = tmp_path / "def_long.mp4"
        def_mp4.write_bytes(b"MP4" * 200)
        v_def = Video(
            source="longform",
            source_id="def_guard_1",
            source_url="",
            title="Default Approved Long",
            transformed_path=str(def_mp4),
            duration_sec=400,
            status=VideoStatus.TRANSFORMED,
            channel_id=def_ch.id,
            meta={"long_form": True, "owner_approved": "2026-05-01T00:00:00Z"},
        )
        db.add(v_def)
        db.flush()
        v_def_id = v_def.id

    # 4. With use_mock=False and empty youtube_client_secrets, default must NEVER fall back to MockPublisher
    monkeypatch.setattr(settings, "use_mock", False)
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "youtube_client_secrets", "")
    monkeypatch.setattr(settings, "youtube_token_path", "")
    monkeypatch.setattr(
        MockPublisher,
        "upload",
        lambda *a, **kw: pytest.fail("MockPublisher.upload must not be called when use_mock=False"),
    )

    ap = long_job.long_autopilot_job()
    assert ap["indianvlogs"]["skipped"] == "none_ready"
    assert ap["default"]["skipped"] == "credentials missing"
    with session_scope() as db:
        assert db.get(Video, res1["video_id"]).status == VideoStatus.TRANSFORMED
        assert db.get(Video, v_def_id).status == VideoStatus.TRANSFORMED
        assert db.get(Video, v_def_id).youtube_video_id is None


def test_t5_default_channel_null_id_enforces_target_languages_and_blocks_foreign_rendered(
    write_channels, monkeypatch
):
    """T5-02: channel_id=None resolves default channel's meta.target_languages and blocks foreign rendered clips on ['en']."""
    write_channels([_default_entry(meta={"target_languages": ["en"]})])
    monkeypatch.setattr(settings, "target_languages", "")
    monkeypatch.setattr(settings, "allow_unknown_language", False)

    with session_scope() as db:
        def_ch = channels.channel_by_slug(db, "default")
        v_null_es_explicit = Video(
            source="youtube",
            source_id="t5_null_es",
            source_url="u",
            title="Clip",
            language="es",
            channel_id=None,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech"},
        )
        v_null_es_rendered = Video(
            source="youtube",
            source_id="t5_null_es_txt",
            source_url="u",
            title="Hola amigos reto imposible",
            language="",
            channel_id=None,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech"},
        )
        v_def_es_rendered = Video(
            source="youtube",
            source_id="t5_def_es_txt",
            source_url="u",
            title="Hola amigos reto imposible",
            language="",
            channel_id=def_ch.id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech"},
        )
        v_null_hi_rendered = Video(
            source="youtube",
            source_id="t5_null_hi_txt",
            source_url="u",
            title="Aaj crazy trip hai",
            language="",
            channel_id=None,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech"},
        )
        v_null_en_ok = Video(
            source="youtube",
            source_id="t5_null_en_ok",
            source_url="u",
            title="Unbelievable stunt in real life",
            language="",
            channel_id=None,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech"},
        )
        v_null_num_ok = Video(
            source="youtube",
            source_id="t5_null_num_ok",
            source_url="u",
            title="12345",
            language="",
            channel_id=None,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "visual"},
        )
        db.add_all([
            v_null_es_explicit,
            v_null_es_rendered,
            v_def_es_rendered,
            v_null_hi_rendered,
            v_null_en_ok,
            v_null_num_ok,
        ])
        db.flush()

        assert pipeline._channel_languages(v_null_es_explicit) == ["en"]
        assert pipeline.clip_gate_reason(v_null_es_explicit) == "language: es"
        assert pipeline.clip_gate_reason(v_null_es_rendered) == "language: es"
        assert pipeline.clip_gate_reason(v_def_es_rendered) == "language: es"
        assert pipeline.clip_gate_reason(v_null_hi_rendered) == "language: hi"
        assert pipeline.clip_gate_reason(v_null_en_ok) is None
        assert pipeline.clip_gate_reason(v_null_num_ok) is None


def test_t5_ambient_ai_thumbnail_v4_unpacked_and_selected(
    tmp_path: Path, write_channels, monkeypatch
):
    """T5-03: generate_ambient_thumbnails unpacks (label, path) from generate_ai_thumbnail and selects v4."""
    write_channels([_ambient_entry()])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    monkeypatch.setattr(long_job.notify, "send_long_ready", lambda *a, **kw: True)

    def _fake_ai_thumb(hero_path: str, prompt: str, dest: Path):
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(make_valid_jpeg_bytes())
        return ("ChatGPT AI", str(out))

    monkeypatch.setattr(thumbnails, "generate_ai_thumbnail", _fake_ai_thumb)

    tdir = tmp_path / "amb_thumbs"
    variants, _prompt, _hero = ambient.generate_ambient_thumbnails(
        "lofi_chill",
        out_dir=tdir,
        title="Midnight Rain Lofi",
        duration_sec=1800.0,
        brand_name="@MidnightRainLofi",
    )
    ids = [v["id"] for v in variants]
    assert ids == ["v1", "v2", "v3", "v4"]
    v4 = next(v for v in variants if v["id"] == "v4")
    assert v4["label"] == "ChatGPT AI"
    assert Path(v4["path"]).is_file()

    def _fake_gen_amb(preset, output_path, **kw):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MP4" * 200)
        th_dir = Path(kw["thumbs_dir"])
        th_vars, th_prompt, hero_p = ambient.generate_ambient_thumbnails(
            preset,
            out_dir=th_dir,
            title="Midnight Rain Lofi",
            duration_sec=60.0,
            brand_name=kw.get("brand_name") or "@MidnightRainLofi",
        )
        return ambient.AmbientResult(
            output_path=str(out),
            preset=preset,
            duration=60.0,
            width=1920,
            height=1080,
            fps=30,
            title="Midnight Rain Lofi",
            description="100% original procedural audio.",
            tags=("lofi", "ambient"),
            chapters=((0.0, "Intro"),),
            chapter_text="0:00 Intro",
            thumbnails=tuple(th_vars),
            hero_frame=str(hero_p),
            thumbnail_prompt=th_prompt,
            language="en",
            theme="",
            part_number=1,
        )

    monkeypatch.setattr(ambient, "generate_ambient_video", _fake_gen_amb)
    long_job.enqueue_long_job("ambient", format_kind="lofi_chill", target_minutes=1)
    res = long_job.run_next_long_job()
    assert res["ok"] is True

    with session_scope() as db:
        row = db.get(Video, res["video_id"])
        assert row is not None
        assert any(t["id"] == "v4" and t["label"] == "ChatGPT AI" for t in row.meta["thumbnails"])
        assert row.meta["selected_thumbnail_id"] == "v4"
        assert Path(row.meta["selected_thumbnail"]).is_file()


def test_t5_render_compilation_uses_highest_score_climax_when_chronology_reordered(
    tmp_path: Path, monkeypatch
):
    """T5-04: render_compilation selects highest-scoring segment as climax for cold_open and end_tail."""
    src_a = tmp_path / "src_a.mp4"
    src_b = tmp_path / "src_b.mp4"
    src_a.write_bytes(b"MP4A")
    src_b.write_bytes(b"MP4B")

    s0 = planner.SegmentPlan(str(src_a), "https://youtu.be/a", "a", "@One", "One", "Vid A", 10.0, 50.0, 85.0, "Opener", "en")
    s1_climax = planner.SegmentPlan(str(src_b), "https://youtu.be/b", "b", "@One", "One", "Vid B", 100.0, 150.0, 99.0, "Real Climax", "en")
    s2_lower = planner.SegmentPlan(str(src_a), "https://youtu.be/a", "a", "@One", "One", "Vid A", 200.0, 250.0, 60.0, "Later Lower", "en")
    co = planner.pick_cold_open([s0, s1_climax, s2_lower], climax=s1_climax)

    plan = planner.CompilationPlan(
        format_kind="best_of",
        channel_slug="default",
        part_number=1,
        targets=("@One",),
        theme="",
        title="Best of One",
        description="Desc",
        tags=("vlog",),
        language="en",
        target_seconds=180.0,
        cold_open=co,
        segments=(s0, s1_climax, s2_lower),
    )

    seen_cold: list[tuple[planner.SegmentPlan, planner.SegmentPlan]] = []
    seen_tail: list[tuple[Path, float]] = []

    monkeypatch.setattr(long_render, "probe", lambda p: MediaInfo(300.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(long_render, "_probe_duration", lambda p, f: f)
    monkeypatch.setattr(
        long_render,
        "run_ffmpeg",
        lambda args, **kw: (Path(args[-1]).parent.mkdir(parents=True, exist_ok=True), Path(args[-1]).write_bytes(b"X" * 2048)),
    )
    monkeypatch.setattr(
        voiceover,
        "generate_commentary_script",
        lambda pl: {"hook": "Wait for the finale!", "bridges": ["B1", "B2", "B3"], "outro": "Subscribe!"},
    )
    monkeypatch.setattr(voiceover, "synthesize_wav", lambda text, out_wav, **kw: (Path(out_wav).write_bytes(b"RIFF"), 2.0)[1])
    monkeypatch.setattr(
        voiceover,
        "render_narrator_bridge",
        lambda wav, bg, out, td, **kw: (Path(out).write_bytes(b"X" * 2048), 2.5)[1],
    )

    def _spy_cold(cold_open, climax, hook_wav, hook_dur, out_path, tmp_dir, **kw):
        seen_cold.append((cold_open, climax))
        Path(out_path).write_bytes(b"X" * 2048)
        return 5.0

    def _spy_tail(src, out_path, tmp_dir, *, seek_sec=0.0, **kw):
        seen_tail.append((Path(src), float(seek_sec)))
        Path(out_path).write_bytes(b"X" * 2048)
        return 6.0

    monkeypatch.setattr(long_render, "_render_cold_open_with_hook", _spy_cold)
    monkeypatch.setattr(long_render, "_render_end_tail", _spy_tail)

    res = long_render.render_compilation(
        plan,
        str(tmp_path / "out_reordered.mp4"),
        brand_name="@Brand",
        include_end_screen=True,
        include_voiceover=True,
    )
    assert Path(res.output_path).is_file()
    assert len(seen_cold) == 1
    assert seen_cold[0][1] == s1_climax
    assert seen_cold[0][1].source_path == str(src_b)
    assert len(seen_tail) == 1
    assert seen_tail[0][0] == src_b
    assert seen_tail[0][1] >= s1_climax.start


def test_t5_long_panel_data_visible_past_120_newer_shorts(tmp_path: Path, write_channels):
    """T5-05: long_panel_data filters long-form rows in SQL before limit(120) so >120 newer Shorts never hide them."""
    write_channels([_indianvlogs_entry()])
    mp4 = tmp_path / "long_vid.mp4"
    mp4.write_bytes(b"MP4")
    base_ts = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        long_rows = [
            Video(
                source="longform",
                source_id="lp_pend",
                source_url="",
                title="Long Pending",
                transformed_path=str(mp4),
                status=VideoStatus.PENDING,
                channel_id=ch.id,
                scraped_at=base_ts,
                meta={"long_form": True},
            ),
            Video(
                source="import",
                source_id="lp_trans",
                source_url="",
                title="Long Transformed",
                transformed_path=str(mp4),
                status=VideoStatus.TRANSFORMED,
                channel_id=ch.id,
                scraped_at=base_ts + timedelta(seconds=1),
                meta={"long_form": True},
            ),
            Video(
                source="longform",
                source_id="lp_pub",
                source_url="",
                title="Long Published",
                transformed_path=str(mp4),
                status=VideoStatus.PUBLISHED,
                youtube_video_id="YT_LP_PUB",
                channel_id=ch.id,
                scraped_at=base_ts + timedelta(seconds=2),
                meta={"long_form": True},
            ),
        ]
        db.add_all(long_rows)
        shorts = [
            Video(
                source="youtube",
                source_id=f"short_newer_{i}",
                source_url="",
                title=f"Short {i}",
                status=VideoStatus.PENDING,
                channel_id=ch.id,
                scraped_at=base_ts + timedelta(minutes=10 + i),
                meta={"clip_rendered": True},
            )
            for i in range(130)
        ]
        db.add_all(shorts)

    panel = long_job.long_panel_data()
    assert [x["title"] for x in panel["pending"]] == ["Long Pending"]
    assert [x["title"] for x in panel["ready"]] == ["Long Transformed"]
    assert [x["title"] for x in panel["posted"]] == ["Long Published"]


def test_t5_clean_vlog_opening_frame_skip_respects_first_spoken_word(tmp_path: Path, monkeypatch):
    """T5-06: clean_vlog passes speech words to render_moment so opening-speech protection blocks clipping active words."""
    src_mp4 = tmp_path / "hi_vlog.mp4"
    src_mp4.write_bytes(b"0" * 20_000)

    # 1. Direct render_moment check with clean_vlog EditPlan(words=[], segments=[(10.0, 28.0)])
    monkeypatch.setattr(clip_render, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"0" * 2048))
    monkeypatch.setattr(clip_render, "probe", lambda p: MediaInfo(18.0, 1080, 1920, 30.0, True))
    monkeypatch.setattr(settings, "clip_loop_ending", False)

    seen_skips: list[tuple[float, float]] = []

    def _spy_offset(source, start, end, *, max_skip=1.5, min_remain=4.0):
        seen_skips.append((start, max_skip))
        return 0.75

    monkeypatch.setattr(clip_render, "_bright_sharp_opening_offset", _spy_offset)

    # Case A: Hindi word starts right at 10.1s via edit.opening_words (words=[] when captions=False) -> must NOT clip opening speech
    words_early = [Word("अरे", 10.1, 10.4), Word("भाई", 10.5, 10.9), Word("रुको।", 11.0, 11.4)]
    ed_early = edit.EditPlan(words=[], segments=[(10.0, 28.0)], speed=1.0, opening_words=list(words_early))
    plan_early = clip_render.render_moment(
        str(src_mp4),
        MediaInfo(40.0, 1920, 1080, 30.0, True),
        _dummy_signals(40.0),
        Moment(10.0, 28.0, kind="speech", title="Bhai Twist", hook="WAIT"),
        [],
        str(tmp_path / "out_early.mp4"),
        edit=ed_early,
        captions=False,
    )
    assert seen_skips == []
    assert plan_early.segments[0][0] == pytest.approx(10.0, abs=0.05)

    # Case B: First word starts at 11.5s via edit.opening_words -> allowed_skip = 1.44 >= 0.5 -> dark frames up to 0.75s skipped
    words_late = [Word("भाई", 11.5, 11.9), Word("देखो।", 12.0, 12.5)]
    ed_late = edit.EditPlan(words=[], segments=[(10.0, 28.0)], speed=1.0, opening_words=list(words_late))
    plan_late = clip_render.render_moment(
        str(src_mp4),
        MediaInfo(40.0, 1920, 1080, 30.0, True),
        _dummy_signals(40.0),
        Moment(10.0, 28.0, kind="speech", title="Bhai Twist", hook="WAIT"),
        [],
        str(tmp_path / "out_late.mp4"),
        edit=ed_late,
        captions=False,
    )
    assert len(seen_skips) == 1 and seen_skips[0][1] == pytest.approx(1.44, abs=0.02)
    assert plan_late.segments[0][0] == pytest.approx(10.75, abs=0.05)

    # 2. End-to-end clip_job.clip_source path for clean_vlog (already_captioned=True -> timed=False)
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(clip_job, "require_tools", lambda: None)
    monkeypatch.setattr(clip_job, "probe", lambda p: MediaInfo(40.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(clip_job, "analyse", lambda p, i: _dummy_signals(40.0))
    monkeypatch.setattr(
        clip_job,
        "transcribe",
        lambda p, i: transcribe.Transcript(words=words_early, language="hi", duration=40.0, engine="sarvam"),
    )
    monkeypatch.setattr(clip_job.subtitles, "already_captioned", lambda p, i: True)
    monkeypatch.setattr(clip_job, "_earlier_clips", lambda s: [])
    monkeypatch.setattr(
        clip_job,
        "pick_speech",
        lambda *a, **kw: [Moment(10.0, 28.0, kind="speech", title="Bhai Twist", hook="WAIT", reveal="End Tak Dekho", score=88.0)],
    )
    monkeypatch.setattr(clip_job, "build_clean_vlog_edit", lambda *a, **kw: edit.EditPlan(words=[], segments=[(10.0, 28.0)], speed=1.0))
    monkeypatch.setattr(clip_job.sfx, "trending", lambda: {})
    monkeypatch.setattr(clip_job.music, "pick", lambda *a: None)
    monkeypatch.setattr(clip_job, "_store_audio_qa", lambda *a: None)
    monkeypatch.setattr(pipeline, "generate_metadata_for_video", lambda *a: None)

    captured_render_calls: list[tuple[list[Word], list[Word], bool, float]] = []
    real_render = clip_render.render_moment
    seen_skips.clear()

    def _spy_render(source, info, signals, moment, w, out_p, edit=None, captions=True, **kw):
        pl = real_render(source, info, signals, moment, w, out_p, edit=edit, captions=captions, **kw)
        captured_render_calls.append((list(w), list(edit.opening_words) if edit else [], bool(captions), pl.segments[0][0]))
        return pl

    monkeypatch.setattr(clip_job, "render_moment", _spy_render)

    res = clip_job.clip_source(ingest.SourceVideo(path=str(src_mp4), title="Hindi Vlog", source="local", source_id="hi_vlog_t5"))
    assert res["ok"] is True
    assert len(captured_render_calls) == 1
    passed_words, passed_opening_words, passed_captions, first_seg_start = captured_render_calls[0]
    assert passed_words == []
    assert len(passed_opening_words) == len(words_early)
    assert passed_captions is False
    assert first_seg_start == pytest.approx(10.0, abs=0.05)
    assert seen_skips == []


def test_t5_queue_filters_and_auto_approve_preserve_pending_longform(
    tmp_path: Path, auth_client, write_channels, monkeypatch
):
    """T5-08: apply_queue_filters (preview + filter_queue) and auto_approve_job preserve all PENDING long-form videos."""
    write_channels([_default_entry(), _indianvlogs_entry(), _ambient_entry()])
    monkeypatch.setattr(settings, "target_languages", '["en"]')
    monkeypatch.setattr(settings, "auto_approve", True)
    monkeypatch.setattr(settings, "auto_approve_veto_hours", 0)

    mp4 = tmp_path / "longform_pending.mp4"
    mp4.write_bytes(b"MP4" * 200)
    old_ts = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)

    with session_scope() as db:
        def_ch = channels.channel_by_slug(db, "default")
        ind_ch = channels.channel_by_slug(db, "indianvlogs")
        amb_ch = channels.channel_by_slug(db, "ambient")

        lf_hi = Video(
            source="longform",
            source_id="t5_lf_hi",
            source_url="",
            title="Hindi Long Compilation",
            language="hi",
            transformed_path=str(mp4),
            duration_sec=540,
            width=1920,
            height=1080,
            status=VideoStatus.PENDING,
            channel_id=ind_ch.id,
            scraped_at=old_ts,
            meta={"long_form": True, "clip_rendered": False},
        )
        lf_en = Video(
            source="longform",
            source_id="t5_lf_en",
            source_url="",
            title="English Long Compilation",
            language="en",
            transformed_path=str(mp4),
            duration_sec=540,
            width=1920,
            height=1080,
            status=VideoStatus.PENDING,
            channel_id=def_ch.id,
            scraped_at=old_ts,
            meta={"long_form": True, "clip_rendered": False},
        )
        lf_amb = Video(
            source="longform",
            source_id="t5_lf_amb",
            source_url="",
            title="Midnight Rain Lofi 30m",
            language="en",
            transformed_path=str(mp4),
            duration_sec=1800,
            width=1920,
            height=1080,
            status=VideoStatus.PENDING,
            channel_id=amb_ch.id,
            scraped_at=old_ts,
            meta={"long_form": True, "clip_rendered": False},
        )
        bad_short_filter = Video(
            source="youtube",
            source_id="t5_bad_short_filter",
            source_url="https://example.com/bad1",
            title="Spanish off-focus short",
            language="es",
            duration_sec=25,
            width=1080,
            height=1920,
            status=VideoStatus.PENDING,
            channel_id=def_ch.id,
            scraped_at=old_ts,
            meta={},
        )
        bad_short_auto = Video(
            source="youtube",
            source_id="t5_bad_short_auto",
            source_url="https://example.com/bad2",
            title="French off-focus short",
            language="fr",
            duration_sec=25,
            width=1080,
            height=1920,
            status=VideoStatus.PENDING,
            channel_id=def_ch.id,
            scraped_at=old_ts,
            meta={},
        )
        db.add_all([lf_hi, lf_en, lf_amb, bad_short_filter, bad_short_auto])
        db.flush()
        lf_ids = [lf_hi.id, lf_en.id, lf_amb.id]
        bad1_id, bad2_id = bad_short_filter.id, bad_short_auto.id

    # 1. GET /api/controls/preview (dry_run=True) must only flag the 2 off-focus Shorts, keeping all 3 long-form rows
    prev = auth_client.get("/api/controls/preview")
    assert prev.status_code == 200
    fprev = prev.json()["filters"]
    assert fprev["checked"] == 5
    assert fprev["kept"] == 3
    assert fprev["rejected"] == 2
    assert fprev["dry_run"] is True

    # 2. auto_approve_job() must skip all 3 long-form rows without rejecting Hindi or auto-approving English
    auto_res = scheduler.auto_approve_job()
    assert auto_res["approved"] == 0
    assert auto_res["reasons"].get("long-form awaits review") == 3
    assert auto_res["reasons"].get("language") == 2

    with session_scope() as db:
        for vid in lf_ids:
            row = db.get(Video, vid)
            assert row.status == VideoStatus.PENDING
        assert db.get(Video, bad1_id).status == VideoStatus.REJECTED
        assert db.get(Video, bad2_id).status == VideoStatus.REJECTED

        # Add one more invalid Short to verify POST /api/controls/filter_queue still rejects invalid Shorts
        # while preserving all 3 PENDING long-form rows
        def_ch = channels.channel_by_slug(db, "default")
        bad_short_3 = Video(
            source="youtube",
            source_id="t5_bad_short_3",
            source_url="https://example.com/bad3",
            title="German off-focus short",
            language="de",
            duration_sec=25,
            width=1080,
            height=1920,
            status=VideoStatus.PENDING,
            channel_id=def_ch.id,
            scraped_at=old_ts,
            meta={},
        )
        db.add(bad_short_3)
        db.flush()
        bad3_id = bad_short_3.id

    # 3. POST /api/controls/filter_queue ("Re-apply safety & language filters")
    fresp = auth_client.post("/api/controls/filter_queue")
    assert fresp.status_code == 200
    assert fresp.json()["state"] == "done"

    with session_scope() as db:
        for vid in lf_ids:
            row = db.get(Video, vid)
            assert row.status == VideoStatus.PENDING
        assert db.get(Video, bad3_id).status == VideoStatus.REJECTED
