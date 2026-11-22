"""Tier 4 E2E Tests: Realistic End-to-End Application Scenarios across R1, R2, R3, and R4."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app import review as review_mod, review_ui
from app.clipper import ai, edit, ingest, job as clip_job, moments
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.signals import Signals
from app.clipper.transcribe import Transcript, Word
from app.config import settings
from app.db import session_scope
from app.growth import approval, autopilot, channels, safety, supply, titles, watch
from app.longform import job as long_job, planner, voiceover
from app.models import Channel, GrowthEvent, LongJob, MetadataDraft, Video, VideoStatus
from app.winproc import background_subprocess_kwargs
from scripts import compile as compile_cli, doctor, final_audit
from tasks import pipeline
from tests.e2e.conftest import (
    detect_black_frames,
    detect_silences,
    detect_volume,
    make_sarvam_tts_payload,
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


def _default_entry(**overrides) -> dict:
    base = {
        "slug": "default",
        "name": "Default Channel",
        "niche": "challenge clips",
        "brand_name": "@E2EBrand",
        "uploads_per_day": 3,
        "long_uploads_per_day": 1,
        "long_auto_publish": False,
        "timezone": "UTC",
        "require_approval": True,
        "enabled": True,
        "feeds": ["https://www.youtube.com/@MrBeast"],
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


def _words(prefix: str, start: float, end: float, count: int = 30) -> list[Word]:
    step = (end - start) / max(1, count)
    return [
        Word(f"{prefix}{i}." if i == count - 1 else f"{prefix}{i}", round(start + i * step, 2), round(start + (i + 0.8) * step, 2), 0.95)
        for i in range(count)
    ]


@pytest.mark.slow
@requires_ffmpeg
def test_t4_01_hindi_souravjvlogs_longform_build_review_and_pending_safety(tmp_path: Path, auth_client, write_channels, monkeypatch):
    """T4-01: Realistic Hindi @souravjvlogs 16:9 build -> Review UI -> PENDING upload lock."""
    sec = tmp_path / "sec.json"
    tok = tmp_path / "tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    write_channels([_indianvlogs_entry(client_secrets=str(sec), token_path=str(tok))])
    monkeypatch.setattr(settings, "sarvam_api_key", "e2e-hi-key")
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(
        ai,
        "ask_json",
        lambda prompt, **kw: (
            {"indices": [0, 1], "titles": {"0": "Piyush Ka Surprise", "1": "Sabse Bada Twist"}}
            if '"indices"' in prompt
            else (
                {"hook": "अंत में जो हुआ किसी ने नहीं सोचा था!", "bridges": ["पहला मजेदार मोड़।", "और अब सबसे बड़ा क्लाइमेक्स!"], "outro": "सब्सक्राइब जरूर करें!"}
                if '"bridges"' in prompt
                else {"title": "Sourav Joshi Best Moments", "description": "Hindi vlog compilation", "tags": ["sourav joshi", "hindi vlog"]}
            )
        ),
    )

    src_mp4 = make_synthetic_mp4(tmp_path / "hi_source.mp4", duration_sec=65.0, width=1280, height=720, amplitude=0.7)
    words_dicts = [{"text": w.text, "start": w.start, "end": w.end, "confidence": 0.95} for w in _words("भाई", 2.0, 60.0, count=60)]
    write_transcript_sidecar(src_mp4, words_dicts, language="hi", engine="sarvam")

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        db.add(Video(
            source="longform", source_id="hi_rej_1", source_url="u", title="Old Rejected",
            status=VideoStatus.REJECTED, channel_id=ch.id,
            meta={"long_form": True, "long_format": "best_of", "long_targets_key": "@souravjvlogs", "part_number": 1, "long_segments": [{"source_id": "hi_src_1", "start": 2.0, "end": 12.0}]},
        ))

    monkeypatch.setattr(
        planner,
        "list_creator_videos",
        lambda url, limit=25: [{"id": "hi_src_1", "url": "https://youtube.com/watch?v=hi_src_1", "title": "Sourav Vlog", "duration": 240.0, "creator": "@souravjvlogs"}],
    )
    monkeypatch.setattr(
        ingest,
        "fetch",
        lambda url: ingest.SourceVideo(path=str(src_mp4), title="Sourav Vlog", author="@souravjvlogs", author_name="Sourav Joshi", url=url, source="youtube", source_id="hi_src_1"),
    )
    monkeypatch.setattr(moments, "pick_speech", lambda *a, **kw: [Moment(2.0, 12.0, score=88.0, title="Act 1", kind="speech"), Moment(15.0, 27.0, score=94.0, title="Act 2", kind="speech")])
    monkeypatch.setattr(
        voiceover.httpx,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: make_sarvam_tts_payload(2.0), text="ok"),
    )

    orig_plan = planner.plan_compilation
    monkeypatch.setattr(planner, "plan_compilation", lambda **kw: orig_plan(**{**kw, "min_total_sec": 15.0}))

    rc = compile_cli.main(["--channel", "indianvlogs", "--format", "best_of", "--creator", "@souravjvlogs", "--run-now", "--json"])
    assert rc == 0

    panel = auth_client.get("/api/long/videos").json()
    assert len(panel["pending"]) == 1
    card = panel["pending"][0]
    assert card["status"] == "pending"
    assert len(card["thumbnails"]) >= 3
    assert card["hero_url"]
    assert "#shorts" not in card["title"].lower()

    with session_scope() as db:
        v = db.get(Video, card["id"])
        out_mp4 = Path(v.transformed_path)
        assert v.status == VideoStatus.PENDING
        assert v.meta.get("part_number") == 1

    probed = probe_media(out_mp4)
    assert int(probed["video"]["width"]) == 1920
    assert int(probed["video"]["height"]) == 1080
    assert detect_black_frames(out_mp4, duration=0.2, pic_th=0.98) == []
    total_dur = float(probed["duration"])
    non_fade_silences = [(s, e) for s, e in detect_silences(out_mp4, noise_db=-45.0, min_dur=0.5) if s < max(0.0, total_dur - 0.8)]
    assert non_fade_silences == []
    assert detect_volume(out_mp4)["max_volume"] <= -0.5

    pub_calls: list[str] = []
    monkeypatch.setattr(pipeline, "publish_task", lambda vid, meta=None: pub_calls.append(vid) or {"ok": True})
    monkeypatch.setattr(settings, "use_mock", False)
    long_job.long_autopilot_job()
    assert pub_calls == []


@pytest.mark.slow
@requires_ffmpeg
def test_t4_02_english_mrbeast_longform_build_review_and_pending_safety(tmp_path: Path, auth_client, write_channels, monkeypatch):
    """T4-02: Realistic English @MrBeast 16:9 build -> en-IN/shubh TTS -> PENDING safety."""
    write_channels([_default_entry()])
    monkeypatch.setattr(settings, "sarvam_api_key", "e2e-en-key")
    monkeypatch.setattr(ai, "available", lambda: False)

    src_mp4 = make_synthetic_mp4(tmp_path / "en_source.mp4", duration_sec=65.0, width=1280, height=720, amplitude=0.35)
    words_dicts = [{"text": w.text, "start": w.start, "end": w.end, "confidence": 0.95} for w in _words("challenge", 2.0, 60.0, count=60)]
    write_transcript_sidecar(src_mp4, words_dicts, language="en", engine="deepgram")

    monkeypatch.setattr(
        planner,
        "list_creator_videos",
        lambda url, limit=25: [{"id": "en_src_1", "url": "https://youtube.com/watch?v=en_src_1", "title": "MrBeast Challenge", "duration": 240.0, "creator": "@MrBeast"}],
    )
    monkeypatch.setattr(
        ingest,
        "fetch",
        lambda url: ingest.SourceVideo(path=str(src_mp4), title="MrBeast Challenge", author="@MrBeast", author_name="MrBeast", url=url, source="youtube", source_id="en_src_1"),
    )
    monkeypatch.setattr(moments, "pick_speech", lambda *a, **kw: [Moment(2.0, 12.0, score=88.0, title="Round One", kind="speech"), Moment(14.0, 26.0, score=95.0, title="Final Vault", kind="speech")])

    tts_payloads: list[dict] = []

    def _fake_post(url, *, headers=None, json=None, timeout=None):
        tts_payloads.append(json)
        return SimpleNamespace(status_code=200, json=lambda: make_sarvam_tts_payload(2.0), text="ok")

    monkeypatch.setattr(voiceover.httpx, "post", _fake_post)
    orig_plan = planner.plan_compilation
    monkeypatch.setattr(planner, "plan_compilation", lambda **kw: orig_plan(**{**kw, "min_total_sec": 15.0}))

    long_job.enqueue_long_job("default", format_kind="best_of", target="@MrBeast")
    res = long_job.run_next_long_job()
    assert res["ok"] is True

    assert tts_payloads
    assert all(p["target_language_code"] == "en-IN" and p["speaker"] == "shubh" for p in tts_payloads)
    assert any("Mr. Beast" in p["text"] for p in tts_payloads)

    with session_scope() as db:
        v = db.get(Video, res["video_id"])
        assert v.status == VideoStatus.PENDING
        out_mp4 = Path(v.transformed_path)

    probed = probe_media(out_mp4)
    assert int(probed["video"]["width"]) == 1920
    assert int(probed["video"]["height"]) == 1080
    assert detect_black_frames(out_mp4, duration=0.2, pic_th=0.98) == []
    assert detect_volume(out_mp4)["max_volume"] <= -0.5

    pub_calls: list[str] = []
    monkeypatch.setattr(pipeline, "publish_task", lambda vid, meta=None: pub_calls.append(vid) or {"ok": True})
    monkeypatch.setattr(settings, "use_mock", False)
    long_job.long_autopilot_job()
    assert pub_calls == []


def test_t4_03_indianvlogs_shorts_pipeline_metadata_language_lock_and_comment(tmp_path: Path, write_channels, monkeypatch):
    """T4-03: Full Shorts lifecycle blocks off-language visual clip and publishes Hindi clip with language lock + comment."""
    sec = tmp_path / "sec.json"
    tok = tmp_path / "tok.json"
    sec.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")
    write_channels([_indianvlogs_entry(client_secrets=str(sec), token_path=str(tok))])
    monkeypatch.setattr(settings, "target_languages", '["hi"]')
    monkeypatch.setattr(settings, "allow_unknown_language", False)
    monkeypatch.setattr(settings, "youtube_auto_comment", True)
    monkeypatch.setattr(settings, "use_mock", False)
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(
        ai,
        "ask_json",
        lambda prompt, **kw: (
            {"comment": "Aapka favourite part kaunsa tha?"}
            if '{"comment"' in prompt
            else {"title": "Piyush ne aaj hadh kar di!", "description": "Funny vlog moment", "tags": ["hindi", "vlog"]}
        ),
    )

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch_id = ch.id

    hi_mp4 = tmp_path / "src_hi.mp4"
    hi_mp4.write_bytes(b"mp4")
    hi_words = _words("भाई", 2.0, 24.0, count=22)
    write_transcript_sidecar(hi_mp4, [{"text": w.text, "start": w.start, "end": w.end, "confidence": 0.95} for w in hi_words], language="hi", engine="sarvam")

    monkeypatch.setattr(clip_job, "require_tools", lambda: None)
    monkeypatch.setattr(clip_job, "probe", lambda p: MediaInfo(duration=60.0, width=1920, height=1080, fps=30.0, has_audio=True))
    monkeypatch.setattr(clip_job, "analyse", lambda p, info: _dummy_signals(60.0))
    monkeypatch.setattr(clip_job.subtitles, "already_captioned", lambda p, info: False)
    monkeypatch.setattr(clip_job.sfx, "trending", lambda: {})
    monkeypatch.setattr(clip_job.music, "pick", lambda k: None)
    monkeypatch.setattr(clip_job, "build_clean_vlog_edit", lambda m, p, speed=1.08, **kw: edit.EditPlan(words=[], segments=[(m.start, m.end)], beats=[], speed=speed))
    monkeypatch.setattr(clip_job, "pick_speech", lambda *a, **kw: [Moment(2.0, 24.0, score=88.0, title="Bhai Twist", hook="Aage Dekho", kind="speech", ai_title=True)])
    monkeypatch.setattr(
        clip_job,
        "render_moment",
        lambda *a, **kw: (Path(a[5]).parent.mkdir(parents=True, exist_ok=True), Path(a[5]).write_bytes(b"short")) and SimpleNamespace(duration=22.0, tail=0.0, music=None, has_sfx=False),
    )

    src = ingest.SourceVideo(path=str(hi_mp4), title="Hindi Vlog", author="@souravjvlogs", source="youtube", source_id="hi_s_1")
    out = clip_job.clip_source(src, count=1, auto_queue=False, channel_id=ch_id)
    assert out["ok"] is True
    hi_vid = out["clips"][0]["id"]

    # Seed off-language visual clip in TRANSFORMED -> must be rejected by _screen
    en_mp4 = tmp_path / "en_vis.mp4"
    en_mp4.write_bytes(b"mp4")
    with session_scope() as db:
        bad = Video(
            source="youtube", source_id="off_lang_vis", source_url="u",
            title="Cliffs From Boat", language="", transformed_path=str(en_mp4), duration_sec=20,
            status=VideoStatus.TRANSFORMED, channel_id=ch_id,
            meta={"clip_rendered": True, "clip_kind": "visual", "clip_score": 55.0},
        )
        db.add(bad)
        db.flush()
        bad_id = bad.id

    styled, style_used = titles.styled_title("curiosity", "Bhai Twist", "भाई0 भाई1")
    assert style_used == "curiosity" and len(styled) <= 70
    approval.approve(hi_vid, title=styled)

    uploads: list[dict] = []
    comments: list[str] = []

    class _Pub:
        def check_ready(self):
            return None

        def upload(self, **kw):
            uploads.append(kw)
            return SimpleNamespace(video_id="YT_SHORT_HI_1")

        def post_comment(self, yt_id, text):
            comments.append(text)
            return "CMT_HI_1"

    pub_inst = _Pub()
    monkeypatch.setattr(autopilot, "_publisher", lambda ch=None: pub_inst)
    monkeypatch.setattr(pipeline, "_publisher_for", lambda cid: pub_inst)
    # 18:00 UTC == 23:30 IST -> all daily windows due
    autopilot.autopilot_job(now=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc))

    with session_scope() as db:
        assert db.get(Video, bad_id).status == VideoStatus.REJECTED
        hi_row = db.get(Video, hi_vid)
        assert hi_row.duration_sec <= 32
        assert hi_row.meta.get("clip_opening_line")
        assert hi_row.status == VideoStatus.PUBLISHED
        assert hi_row.meta.get("auto_comment_id") == "CMT_HI_1"

    assert len(uploads) == 1
    assert uploads[0]["default_language"] == "hi"
    assert uploads[0]["default_audio_language"] == "hi"
    assert len(comments) == 1


def test_t4_04_channel3_3_per_day_supply_review_and_paused_block(tmp_path: Path, auth_client, write_channels, monkeypatch):
    """T4-04: Channel #3 3/day procedural supply -> Control Panel review/approve -> unlinked/paused block."""
    from app.longform import ambient

    write_channels([_ambient_entry(enabled=True, long_uploads_per_day=3, long_auto_publish=False)])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    monkeypatch.setattr(long_job, "_STOCK_CAP", 5)
    monkeypatch.setattr(settings, "use_mock", False)

    def _fake_amb(preset, target_minutes, output_path, **kw):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"ambient-mp4")
        tp = Path(output_path).parent / "thumb.jpg"
        tp.write_bytes(make_valid_jpeg_bytes())
        return ambient.AmbientResult(
            output_path=str(output_path), duration=3600.0, preset=preset,
            width=1920, height=1080, fps=30,
            title=f"{preset} Study Mix", description="Claim-free focus",
            tags=("lofi", "ambient"), chapters=((0.0, "Intro"),), chapter_text="0:00 Intro",
            thumbnails=({"id": "v1", "label": "V1", "path": str(tp)},),
            hero_frame=str(tp), thumbnail_prompt="rain",
            language="en", theme="", part_number=1,
        )

    monkeypatch.setattr(ambient, "generate_ambient_video", _fake_amb)

    for _ in range(3):
        res = long_job.long_supply_job()
        assert res["ambient"]["queued"] is True
        assert long_job.run_next_long_job()["ok"] is True

    ch_resp = auth_client.get("/api/channels").json()
    amb_info = next(c for c in ch_resp["channels"] if c["slug"] == "ambient")
    assert not amb_info.get("supply_gap")

    panel = auth_client.get("/api/long/videos").json()
    assert len(panel["pending"]) == 3

    first_id = panel["pending"][0]["id"]
    assert auth_client.post(f"/api/long/{first_id}/approve", json={}).json()["status"] == "transformed"

    pub_calls: list[str] = []
    monkeypatch.setattr(pipeline, "publish_task", lambda vid, meta=None: pub_calls.append(vid) or {"ok": True})
    long_job.long_autopilot_job()
    assert pub_calls == []
    assert supply.supply_job().get("ambient", {}).get("starved") is None
    assert watch.watch_job()["feeds"] == 0


def test_t4_05_control_panel_auth_js_and_offline_health_gates(tmp_path: Path, anon_client, auth_client):
    """T4-05: Control Panel _guard audit, inline JS node --check, lazy imports, doctor & final_audit."""
    api_routes = [r for r in review_mod.review_app.routes if isinstance(r, APIRoute) and r.path != "/"]
    assert all("_guard" in r.endpoint.__code__.co_names for r in api_routes)
    assert anon_client.get("/api/queue").status_code == 401
    assert auth_client.get("/api/queue").status_code == 200

    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", review_ui.PAGE, flags=re.DOTALL | re.IGNORECASE)
    node = shutil.which("node")
    assert node and scripts
    for idx, body in enumerate(scripts):
        f = tmp_path / f"t4_js_{idx}.js"
        f.write_text(body, encoding="utf-8")
        proc = subprocess.run(
            [node, "--check", str(f)],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        assert proc.returncode == 0, proc.stderr

    imp_proc = subprocess.run(
        [sys.executable, "-c", "import sys, app.main, app.review, app.scheduler, app.longform.ambient; assert 'numpy' not in sys.modules and 'cv2' not in sys.modules"],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert imp_proc.returncode == 0, imp_proc.stderr

    shots_proc = subprocess.run(
        [sys.executable, "-c", "import sys, app.clipper.shots; assert 'cv2' not in sys.modules"],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert shots_proc.returncode == 0, shots_proc.stderr

    doc = doctor.run_doctor(fix=False, root=tmp_path)
    assert isinstance(doc["env"]["review_token_set"], bool)
    aud = final_audit.run_audit(live=False)
    assert aud["all_ok"] is True


@pytest.mark.slow
@requires_ffmpeg
def test_t4_06_channel3_procedural_audio_visual_continuity_inspection(tmp_path: Path):
    """T4-06: Direct media QA on procedural lofi_chill & rain_focus 1080p outputs (continuity, stereo, motion)."""
    from PIL import Image, ImageChops
    from app.longform import ambient

    for preset in ("lofi_chill", "rain_focus"):
        out_mp4 = tmp_path / f"{preset}_qa.mp4"
        ambient.generate_ambient_video(preset, target_minutes=1, output_path=str(out_mp4))
        probed = probe_media(out_mp4)
        assert probed["duration"] >= 58.0
        assert int(probed["video"]["width"]) == 1920
        assert int(probed["video"]["height"]) == 1080
        assert int(probed["audio"]["channels"]) == 2
        assert int(probed["audio"]["sample_rate"]) == 48_000
        assert detect_black_frames(out_mp4, duration=0.2, pic_th=0.98) == []
        assert detect_silences(out_mp4, noise_db=-40.0, min_dur=2.0) == []
        assert detect_volume(out_mp4)["max_volume"] <= -0.5

        frames = []
        for idx, ts in enumerate(("0.5", "30.0", "57.0")):
            img_p = tmp_path / f"{preset}_f{idx}.png"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-ss", ts, "-i", str(out_mp4), "-frames:v", "1", str(img_p)],
                stdin=subprocess.DEVNULL, check=True,
                **background_subprocess_kwargs(low_priority=True),
            )
            frames.append(Image.open(img_p).convert("RGB"))
        diff_01 = ImageChops.difference(frames[0], frames[1]).getbbox()
        diff_12 = ImageChops.difference(frames[1], frames[2]).getbbox()
        assert diff_01 is not None or diff_12 is not None, f"{preset} visuals appear completely frozen"


def test_t4_07_cross_track_db_and_pipeline_isolation(tmp_path: Path, write_channels, monkeypatch):
    """T4-07: Shorts, Classic Scraped, R1 Long-Form, and R3 Procedural Ambient stay strictly isolated."""
    write_channels([_indianvlogs_entry(), _ambient_entry()])
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)

    files = {}
    for name in ("short.mp4", "classic.mp4", "long_r1.mp4", "long_r3.mp4"):
        p = tmp_path / name
        p.write_bytes(b"mp4")
        files[name] = str(p)

    with session_scope() as db:
        ch_in = channels.channel_by_slug(db, "indianvlogs")
        ch_amb = channels.channel_by_slug(db, "ambient")
        v_short = Video(
            source="youtube", source_id="tr_short", source_url="u", title="Short",
            transformed_path=files["short.mp4"], status=VideoStatus.PENDING, channel_id=ch_in.id,
            meta={"clip_rendered": True, "long_form": False},
        )
        v_short_ready = Video(
            source="youtube", source_id="tr_short_ok", source_url="u", title="Short Ready",
            transformed_path=files["short.mp4"], status=VideoStatus.TRANSFORMED, channel_id=ch_in.id,
            meta={"clip_rendered": True, "long_form": False},
        )
        v_classic = Video(
            source="tiktok", source_id="tr_classic", source_url="u", title="Classic",
            local_path=files["classic.mp4"], status=VideoStatus.PENDING,
            meta={"clip_rendered": False, "long_form": False},
        )
        v_r1 = Video(
            source="longform", source_id="tr_r1", source_url="u", title="R1 Long",
            transformed_path=files["long_r1.mp4"], status=VideoStatus.PENDING, channel_id=ch_in.id,
            meta={"long_form": True, "clip_rendered": False},
        )
        v_r3 = Video(
            source="longform", source_id="tr_r3", source_url="u", title="R3 Ambient",
            transformed_path=files["long_r3.mp4"], status=VideoStatus.PENDING, channel_id=ch_amb.id,
            meta={"long_form": True, "clip_rendered": False, "mode": "procedural_ambient"},
        )
        db.add_all([v_short, v_short_ready, v_classic, v_r1, v_r3])
        db.flush()

        rev_ids = {v.id for v in approval.awaiting_review(db)}
        scr_ids = {v.id for v in approval.awaiting_scraped(db)}
        ready_ids = {v.id for v in autopilot.ready_clips(db)}
        assert rev_ids == {v_short.id}
        assert scr_ids == {v_classic.id}
        assert ready_ids == {v_short_ready.id}

    panel = long_job.long_panel_data()
    panel_titles = {item["title"] for item in panel["pending"]}
    assert panel_titles == {"R1 Long", "R3 Ambient"}
