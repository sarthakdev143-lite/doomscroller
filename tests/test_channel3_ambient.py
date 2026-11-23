"""Tests for Channel #3 procedural Lofi/Ambient/Rain/Focus engine & feedless channel isolation."""
from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import delete, select

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels, scout, supply, watch
from app.models import Channel, ClipJob, Feed, GrowthEvent, GrowthState, LongJob, MetadataDraft, QuotaUsage, Video


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    init_db()
    with session_scope() as db:
        for tbl in (LongJob, Feed, Channel, ClipJob, GrowthEvent, GrowthState, QuotaUsage, MetadataDraft, Video):
            db.execute(delete(tbl))
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    monkeypatch.setattr(settings, "use_mock", True)


def test_lazy_numpy_invariant_with_ambient_imports():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, app.main, app.review, app.scheduler, app.longform.job, app.longform.ambient as a; "
                "assert not hasattr(app.review, 'ClipJob'); "
                "assert 'numpy' not in sys.modules, 'numpy imported eagerly'; "
                "assert 'cv2' not in sys.modules, 'cv2 imported eagerly'; "
                "assert len(a.list_presets()) == 4"
            ),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_ambient_presets_complete_and_valid():
    from app.longform import ambient

    expected = {"lofi_chill", "rain_focus", "night_ambient", "deep_study"}
    assert set(ambient.AMBIENT_PRESETS) == expected
    assert {p["key"] for p in ambient.list_presets()} == expected

    for key, preset in ambient.AMBIENT_PRESETS.items():
        assert preset.bpm > 0
        assert len(preset.chords_midi) >= 4
        assert len(preset.bass_midi) == len(preset.chords_midi)
        assert len(preset.tags) == 15
        low_tags = [t.lower() for t in preset.tags]
        assert "shorts" not in low_tags and "viral" not in low_tags, f"{key} has forbidden tag"
        assert preset.title_templates


def test_synthesize_cycle_wav_is_stereo_non_silent_unclipped_and_seamless(tmp_path):
    from app.longform import ambient

    for key in ("rain_focus", "lofi_chill"):
        wav_path = tmp_path / f"{key}.wav"
        ambient._synthesize_raw_loop_wav(key, wav_path, cycle_sec=4.0, seed=f"test-{key}")
        assert wav_path.is_file()
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getnchannels() == 2
            assert wf.getframerate() == 48000
            assert wf.getsampwidth() == 2
            nframes = wf.getnframes()
            assert nframes == 4 * 48000
            raw = wf.readframes(nframes)
        samples = struct.unpack(f"<{nframes * 2}h", raw)
        floats = [s / 32768.0 for s in samples]
        rms = math.sqrt(sum(x * x for x in floats) / len(floats))
        peak = max(abs(x) for x in floats)
        assert rms > 0.01, f"{key} audio too quiet: rms={rms}"
        assert peak < 0.99, f"{key} audio clipped: peak={peak}"
        # Seam continuity: tail energy must not dip to zero and wrap jump must be smooth
        tail_rms = math.sqrt(sum(x * x for x in floats[-4800:]) / 4800)
        head_rms = math.sqrt(sum(x * x for x in floats[:4800]) / 4800)
        assert tail_rms > 0.005 and head_rms > 0.005
        seam_jump = max(abs(floats[0] - floats[-2]), abs(floats[1] - floats[-1]))
        assert seam_jump < 0.20, f"{key} seam jump too sharp: {seam_jump}"


def test_build_ambient_metadata_is_claim_free_and_no_shorts():
    from app.longform import ambient

    for key in ambient.AMBIENT_PRESETS:
        for dur in (90.0, 2700.0):
            meta = ambient.build_ambient_metadata(
                key,
                duration_sec=dur,
                brand_name="@MidnightRainLofi",
                seed=f"seed-{key}-{dur}",
                include_chapters=True,
            )
            assert len(meta.title) <= 100
            assert len(meta.description.encode("utf-8")) <= 5000
            assert "#shorts" not in meta.title.lower()
            assert "#shorts" not in meta.description.lower()
            assert "shorts" not in [t.lower() for t in meta.tags]
            assert "viral" not in [t.lower() for t in meta.tags]
            assert "0:00 " in meta.chapter_text
            assert "100% original" in meta.description.lower()
            assert len(meta.chapters) >= 2

        no_ch = ambient.build_ambient_metadata(key, duration_sec=1800.0, include_chapters=False)
        assert "Chapters:" not in no_ch.description
        assert "0:00 " in no_ch.chapter_text


def test_generate_ambient_thumbnails_produces_3_distinct_1280x720_jpegs(tmp_path):
    from app.longform import ambient

    variants, prompt, hero_path = ambient.generate_ambient_thumbnails(
        "night_ambient",
        out_dir=tmp_path / "thumbs",
        title="Midnight Starfield Study Mix [1 Hour]",
        duration_sec=3600.0,
        brand_name="@MidnightRainLofi",
        seed="thumb-test",
    )
    assert Path(hero_path).is_file()
    with Image.open(hero_path) as him:
        assert him.size == (1280, 720)
    assert len(variants) >= 3
    blobs: set[bytes] = set()
    for v in variants[:3]:
        p = Path(v["path"])
        assert p.is_file()
        raw = p.read_bytes()
        assert len(raw) < 2_000_000
        blobs.add(raw)
        with Image.open(p) as im:
            assert im.format == "JPEG"
            assert im.size == (1280, 720)
    assert len(blobs) == 3
    assert "ambient" in prompt.lower() or "night" in prompt.lower()


def test_generate_ambient_video_rejects_bad_preset_and_uses_run_ffmpeg(tmp_path, monkeypatch):
    from app.clipper.media import MediaInfo
    from app.longform import ambient

    with pytest.raises(ValueError, match="preset"):
        ambient.generate_ambient_video("not_a_real_preset", str(tmp_path / "bad.mp4"), duration_sec=10.0)
    for bad_min in (0, -5, float("nan")):
        with pytest.raises(ValueError):
            ambient.generate_ambient_video("lofi_chill", str(tmp_path / "bad_dur.mp4"), target_minutes=bad_min)

    calls: list[list[str]] = []
    reclaimed: list[bool] = []

    def fake_ffmpeg(args, **kwargs):
        calls.append(list(args))
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix == ".wav" and "-i" in args:
            shutil.copyfile(args[args.index("-i") + 1], out)
        else:
            out.write_bytes(b"FAKE_MEDIA_" * 200)

    monkeypatch.setattr(ambient, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(ambient, "probe", lambda p: MediaInfo(65.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(ambient, "reclaim_memory", lambda: reclaimed.append(True))

    out_65 = tmp_path / "out_65.mp4"
    res = ambient.generate_ambient_video(
        "lofi_chill",
        str(out_65),
        duration_sec=65.0,
        brand_name="@MidnightRainLofi",
        theme="Tokyo Rooftop",
        part_number=2,
        thumbs_dir=tmp_path / "thumbs65",
    )
    assert Path(res.output_path).is_file()
    assert res.width == 1920 and res.height == 1080 and res.fps == 30
    assert res.preset == "lofi_chill" and res.part_number == 2 and res.theme == "Tokyo Rooftop"
    assert len(res.thumbnails) >= 3 and Path(res.hero_frame).is_file()
    assert "0:00 " in res.chapter_text
    assert len(calls) == 3  # normalize audio + 16s loop_v + stream_loop mux
    assert "-stream_loop" in calls[-1] and "-c:v" in calls[-1] and "copy" in calls[-1]
    assert reclaimed

    calls.clear()
    out_10 = tmp_path / "out_10.mp4"
    ambient.generate_ambient_video("deep_study", str(out_10), duration_sec=10.0)
    assert len(calls) == 2  # normalize audio + direct 1-pass <=16s render
    assert "libx264" in calls[-1] and "copy" not in calls[-1]


def test_generate_ambient_video_cleans_up_partial_tmp_mp4_on_failure(tmp_path, monkeypatch):
    from app.longform import ambient

    def fake_ffmpeg(args, **kwargs):
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix == ".wav" and "-i" in args:
            shutil.copyfile(args[args.index("-i") + 1], out)
        else:
            out.write_bytes(b"PARTIAL_TMP_MP4" * 50)

    monkeypatch.setattr(ambient, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(ambient, "probe", lambda p: (_ for _ in ()).throw(RuntimeError("simulated probe failure")))

    out_mp4 = tmp_path / "fail_out.mp4"
    partial_mp4 = tmp_path / "fail_out.tmp.mp4"
    with pytest.raises(RuntimeError, match="simulated probe failure"):
        ambient.generate_ambient_video("lofi_chill", str(out_mp4), duration_sec=10.0)
    assert not out_mp4.exists()
    assert not partial_mp4.exists()


def test_normalized_loop_wav_seam_continuity_across_all_presets(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    import numpy as np
    from app.longform import ambient

    for key in ambient.AMBIENT_PRESETS:
        raw_wav = tmp_path / f"{key}_raw.wav"
        norm_wav = tmp_path / f"{key}_norm.wav"
        ambient._synthesize_raw_loop_wav(key, raw_wav, cycle_sec=6.0, seed=f"seam-{key}")
        ambient._normalize_loop_wav(raw_wav, norm_wav)
        assert norm_wav.is_file()

        with wave.open(str(norm_wav), "rb") as wf:
            assert wf.getnchannels() == 2
            assert wf.getframerate() == 48000
            assert wf.getsampwidth() == 2
            nframes = wf.getnframes()
            assert nframes == 6 * 48000
            arr = np.frombuffer(wf.readframes(nframes), dtype="<i2").reshape(-1, 2).astype(np.float64) / 32768.0

        assert np.all(np.isfinite(arr))
        assert float(np.max(np.abs(arr))) <= 0.91
        diffs = np.max(np.abs(np.diff(arr, axis=0)), axis=1)
        p999_d = float(np.percentile(diffs, 99.9))
        seam_jump = float(np.max(np.abs(arr[0] - arr[-1])))
        head_jump = float(np.max(np.abs(arr[1] - arr[0])))
        assert seam_jump <= 1.25 * p999_d, f"{key} normalized loop seam jump {seam_jump:.5f} > 1.25*p999_d ({p999_d:.5f})"
        assert head_jump <= 1.25 * p999_d, f"{key} normalized loop head jump {head_jump:.5f} > 1.25*p999_d ({p999_d:.5f})"


def test_channels_meta_roundtrip_export_import_clone(tmp_path):
    cfg = Path(settings.channels_file)
    cfg.write_text(
        json.dumps({
            "channels": [
                {
                    "slug": "ambient",
                    "name": "Midnight Rain & Lofi",
                    "niche": "lofi ambient rain focus music",
                    "brand_name": "@MidnightRainLofi",
                    "uploads_per_day": 0,
                    "long_uploads_per_day": 3,
                    "long_auto_publish": False,
                    "enabled": False,
                    "feeds": [],
                    "meta": {
                        "mode": "procedural_ambient",
                        "presets": ["lofi_chill", "rain_focus"],
                        "target_minutes": 30,
                        "generate_while_paused": True,
                    },
                },
                {
                    "slug": "tech",
                    "name": "Tech",
                    "uploads_per_day": 2,
                    "feeds": ["https://www.youtube.com/@mkbhd"],
                },
            ]
        }),
        encoding="utf-8",
    )
    with session_scope() as db:
        channels.sync_channels(db)
        amb = channels.channel_by_slug(db, "ambient")
        assert amb is not None and channels.is_procedural(amb) is True
        assert amb.meta["mode"] == "procedural_ambient"

        snap = channels.export_channels_snapshot(db)
        by_slug = {c["slug"]: c for c in snap["channels"]}
        assert "meta" not in by_slug["tech"]
        assert by_slug["ambient"]["meta"]["mode"] == "procedural_ambient"

        cloned = channels.clone_channel(db, "ambient", "ambient-2", name="Ambient Copy")
        assert channels.is_procedural(cloned) is True
        assert cloned.meta["presets"] == ["lofi_chill", "rain_focus"]

    with session_scope() as db:
        with pytest.raises(channels.ChannelsConfigError, match="meta"):
            channels.import_channels_snapshot(db, {
                "version": 1,
                "channels": [{"slug": "badmeta", "meta": "not-a-dict"}],
            })


def test_procedural_channel_exempt_from_shorts_supply_watch_scout_and_next_step(tmp_path, monkeypatch):
    cs = tmp_path / "amb_cs.json"
    tok = tmp_path / "amb_tok.json"
    cs.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")

    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {
                    "slug": "ambient",
                    "name": "Midnight Rain & Lofi",
                    "niche": "lofi ambient rain focus music",
                    "brand_name": "@MidnightRainLofi",
                    "client_secrets": str(cs),
                    "token_path": str(tok),
                    "uploads_per_day": 0,
                    "long_uploads_per_day": 3,
                    "long_auto_publish": False,
                    "enabled": True,
                    "feeds": [],
                    "meta": {"mode": "procedural_ambient", "target_minutes": 30, "generate_while_paused": True},
                }
            ]
        }),
        encoding="utf-8",
    )

    with session_scope() as db:
        channels.sync_channels(db)
        amb = channels.channel_by_slug(db, "ambient")
        assert amb is not None
        assert channels.next_step(db, amb) == ""

    sup = supply.supply_job()
    assert sup.get("skipped") == "no shorts channels" or sup.get("ambient", {}).get("skipped")
    with session_scope() as db:
        assert db.execute(select(GrowthEvent).where(GrowthEvent.kind == "starved")).scalars().all() == []
        assert db.get(GrowthState, "ambient:supply_gap") is None

    wat = watch.watch_job()
    assert wat["feeds"] == 0 and wat["queued"] == 0

    searched: list[str] = []
    monkeypatch.setattr(scout, "_search", lambda q: searched.append(q) or [])
    monkeypatch.setattr(scout.ai, "available", lambda: True)
    sct = scout.scout_job()
    assert searched == []
    assert sct.get("skipped") or sct.get("ambient", {}).get("skipped")


@pytest.mark.slow
def test_real_ffmpeg_ambient_video_20s_audio_loudness_and_visual_continuity(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not installed")
    from app.longform import ambient

    out_mp4 = tmp_path / "real_20s.mp4"
    thumbs_dir = tmp_path / "thumbs"
    res = ambient.generate_ambient_video(
        "rain_focus",
        str(out_mp4),
        duration_sec=20.0,
        brand_name="@MidnightRainLofi",
        thumbs_dir=thumbs_dir,
    )
    assert out_mp4.is_file() and out_mp4.stat().st_size > 10_000
    assert len(res.thumbnails) >= 3

    probe_proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of", "json", str(out_mp4),
        ],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
    )
    pdata = json.loads(probe_proc.stdout)
    dur = float(pdata["format"]["duration"])
    assert 19.5 <= dur <= 21.0
    vstream = next(s for s in pdata["streams"] if s["codec_type"] == "video")
    astream = next(s for s in pdata["streams"] if s["codec_type"] == "audio")
    assert (vstream["width"], vstream["height"]) == (1920, 1080)
    assert vstream["r_frame_rate"] == "30/1"
    assert int(astream["sample_rate"]) == 48000 and int(astream["channels"]) == 2

    vol_proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(out_mp4), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
    )
    m_max = re.search(r"max_volume:\s*([-\d.]+)\s*dB", vol_proc.stderr)
    m_mean = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", vol_proc.stderr)
    assert m_max and m_mean
    assert float(m_max.group(1)) <= -0.8
    assert float(m_mean.group(1)) > -32.0

    sil_proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(out_mp4), "-af", "silencedetect=noise=-45dB:d=0.5", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
    )
    for m in re.finditer(r"silence_start:\s*([-\d.]+)", sil_proc.stderr):
        st = float(m.group(1))
        assert st < 0.5 or st > 19.0, f"unexpected mid-video silence at {st}s"

    blk_proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(out_mp4), "-vf", "blackdetect=d=0.25:pix_th=0.10", "-an", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
    )
    assert "black_start:" not in blk_proc.stderr

    lufs_proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(out_mp4), "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
    )
    i_matches = re.findall(r"\bI:\s*([-\d.]+)\s*LUFS", lufs_proc.stderr)
    assert i_matches, f"ebur128 I missing: {lufs_proc.stderr[-400:]}"
    integrated_lufs = float(i_matches[-1])
    assert -16.0 <= integrated_lufs <= -14.0, f"integrated loudness out of range: {integrated_lufs}"

    import numpy as np

    pcm_wav = tmp_path / "decoded_20s.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(out_mp4), "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(pcm_wav)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
    )
    with wave.open(str(pcm_wav), "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").reshape(-1, 2).astype(np.float64) / 32768.0
    diffs = np.max(np.abs(np.diff(pcm, axis=0)), axis=1)
    p999_d = float(np.percentile(diffs, 99.9))
    lo = max(0, int(15.9 * 48000))
    hi = min(len(diffs), int(16.1 * 48000))
    seam_win_max = float(np.max(diffs[lo:hi]))
    assert seam_win_max <= 1.5 * p999_d, f"16.0s MP4 loop seam spike {seam_win_max:.5f} > 1.5*p999_d ({p999_d:.5f})"


def test_procedural_and_zero_shorts_channels_skip_shorts_warnings_and_autopilot_churn(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from app.growth import approval, autopilot, notify, report

    cs = tmp_path / "amb_cs.json"
    tok = tmp_path / "amb_tok.json"
    cs.write_text("{}", encoding="utf-8")
    tok.write_text("{}", encoding="utf-8")

    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {
                    "slug": "ambient",
                    "name": "Midnight Rain & Lofi",
                    "niche": "lofi ambient rain focus music",
                    "brand_name": "@MidnightRainLofi",
                    "client_secrets": str(cs),
                    "token_path": str(tok),
                    "uploads_per_day": 0,
                    "long_uploads_per_day": 3,
                    "long_auto_publish": False,
                    "enabled": True,
                    "feeds": [],
                    "meta": {"mode": "procedural_ambient", "target_minutes": 30, "generate_while_paused": True},
                }
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "enable_publish", True)

    with session_scope() as db:
        channels.sync_channels(db)
        amb = channels.channel_by_slug(db, "ambient")
        assert amb is not None

    overview = approval.channels_overview()
    assert overview[0]["attention"] == ""

    doc = report.doctor(live=False)
    amb_row = next(r for r in doc["channels"] if r["slug"] == "ambient")
    feeds_check = next(c for c in amb_row["checks"] if c["name"] == "feeds")
    assert feeds_check["ok"] is True
    assert "none set for this channel" not in feeds_check["detail"]
    assert not any(c["name"] == "stock" for c in amb_row["checks"])

    rep_single = report.build_report(channel_slug="ambient")
    rep_fleet = report.build_report()
    assert not any("No finished clips in stock" in w or "Add a creator link" in w for w in rep_single["warnings"])
    assert not any("No finished clips in stock" in w or "Add a creator link" in w for w in rep_fleet["warnings"])

    ap_out = autopilot.autopilot_job(now=datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc))
    assert ap_out["ambient"]["skipped"] == "procedural channel"
    with session_scope() as db:
        assert db.get(GrowthState, "ambient:plan") is None
        assert db.execute(select(GrowthEvent)).scalars().all() == []

    # notify.send_long_ready formats ambient preset labels cleanly
    monkeypatch.setattr(settings, "ntfy_topic", "ops-alerts")
    sent_bodies: list[str] = []
    monkeypatch.setattr(notify, "send", lambda text, *, title="", link="": sent_bodies.append(text) or True)
    for preset, expected in (
        ("lofi_chill", "Lofi Chill"),
        ("rain_focus", "Rain Focus"),
        ("night_ambient", "Night Ambient"),
        ("deep_study", "Deep Study"),
    ):
        assert notify.send_long_ready("Midnight Study", channel_name="Midnight Rain & Lofi", duration_sec=1800, format_kind=preset, prompt="")
        assert expected in sent_bodies[-1]
        assert preset.upper() not in sent_bodies[-1]


def test_channels_entry_problem_validates_meta_target_languages():
    assert channels._entry_problem({"slug": "default", "meta": {"target_languages": ["en"]}}, set()) is None
    assert channels._entry_problem({"slug": "indianvlogs", "meta": {"target_languages": ["hi"]}}, set()) is None
    for bad in ("hi", [], [""], [123]):
        err = channels._entry_problem({"slug": "bad", "meta": {"target_languages": bad}}, set())
        assert err is not None and "meta.target_languages" in err


def test_portrait_mode_renders_16_9_thumbnails_and_passes_portrait_result(tmp_path, monkeypatch):
    import shutil
    from app.clipper.media import MediaInfo
    from app.longform import ambient

    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    pdir = ambient.ambient_portraits_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    portrait_file = pdir / "lofi_chill.jpg"
    im = Image.new("RGB", (1400, 800), (34, 24, 22))
    im.save(portrait_file, "JPEG", quality=92)

    listed = ambient.list_ambient_portraits()
    assert len(listed) == 1 and listed[0]["preset"] == "lofi_chill"

    variants, prompt, hero = ambient.generate_ambient_thumbnails(
        "lofi_chill",
        out_dir=tmp_path / "pthumbs",
        title="i wish i could meet you one last time [30 Min]",
        duration_sec=1800.0,
        brand_name="@AfterMidnightPOV",
        portrait_path=portrait_file,
    )
    assert Path(hero).is_file() and len(variants) >= 3
    assert "night" in prompt.lower()
    blobs = {Path(v["path"]).read_bytes() for v in variants[:3]}
    assert len(blobs) == 3

    calls: list[list[str]] = []

    def fake_ffmpeg(args, **kwargs):
        calls.append(list(args))
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix == ".wav" and "-i" in args:
            shutil.copyfile(args[args.index("-i") + 1], out)
        else:
            out.write_bytes(b"PORTRAIT_MP4_" * 100)

    monkeypatch.setattr(ambient, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(ambient, "probe", lambda p: MediaInfo(65.0, 1920, 1080, 30.0, True))
    res = ambient.generate_ambient_video(
        "lofi_chill",
        str(tmp_path / "portrait_65.mp4"),
        duration_sec=65.0,
        thumbs_dir=tmp_path / "p65_thumbs",
    )
    assert res.portrait_path == str(portrait_file)
    assert len(calls) == 3


def test_curated_music_track_stitcher_builds_crossfaded_mix_and_real_chapters(tmp_path, monkeypatch):
    from app.longform import ambient

    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    tdir = ambient.ambient_music_dir() / "lofi_chill"
    tdir.mkdir(parents=True, exist_ok=True)
    t1 = tdir / "01_stay_a_little_longer.mp3"
    t2 = tdir / "02_rain_on_glass.mp3"
    t1.write_bytes(b"ID3" * 20)
    t2.write_bytes(b"ID3" * 20)

    tracks = ambient.list_ambient_tracks("lofi_chill")
    assert len(tracks) == 2

    ff_calls: list[list[str]] = []
    monkeypatch.setattr(ambient, "_probe_audio_duration", lambda p: 120.0)
    monkeypatch.setattr(ambient, "run_ffmpeg", lambda args, **kw: ff_calls.append(list(args)))

    chapters = ambient._build_curated_mix_wav(tracks, tmp_path / "mix.wav", 300.0, seed="mix-seed")
    assert len(ff_calls) == 1
    cmd = " ".join(ff_calls[0])
    assert "acrossfade" in cmd and "loudnorm" in cmd and "alimiter" in cmd
    titles = [lbl for _, lbl in chapters]
    assert any("stay a little longer" in t for t in titles)
    assert any("rain on glass" in t for t in titles)
    assert chapters[0][0] == 0.0


def test_steady_multi_scene_dissolve_and_rgb24_screen_overlay_and_credits(tmp_path, monkeypatch):
    import shutil
    from app.clipper.media import MediaInfo
    from app.longform import ambient

    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    pdir = ambient.ambient_portraits_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    for fn in ("lofi_chill.jpg", "lofi_chill_2.jpg"):
        Image.new("RGB", (1376, 768), (24, 16, 20)).save(pdir / fn, "JPEG")

    ovdir = ambient.ambient_overlays_dir()
    ovdir.mkdir(parents=True, exist_ok=True)
    (ovdir / "warm_gold_dust.mp4").write_bytes(b"OVERLAY_MP4")

    mdir = ambient.ambient_music_dir() / "lofi_chill"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "Midnight Track.mp3").write_bytes(b"ID3" * 20)
    sib_dir = ambient.ambient_music_dir() / "rain_focus"
    sib_dir.mkdir(parents=True, exist_ok=True)
    (sib_dir / "Sibling Track.mp3").write_bytes(b"ID3" * 20)
    (ambient.ambient_music_dir() / "credits.json").write_text(
        json.dumps({
            "Midnight Track.mp3": {"credit_text": "Midnight Track by Test Artist | CC BY 4.0"},
            "Sibling Track.mp3": {"credit_text": "Sibling Track by Sibling Artist | CC BY-SA 3.0"},
        }),
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_ffmpeg(args, **kwargs):
        calls.append(list(args))
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix == ".wav" and "-i" in args:
            shutil.copyfile(args[args.index("-i") + 1], out)
        else:
            out.write_bytes(b"MP4_" * 50)

    monkeypatch.setattr(ambient, "_probe_audio_duration", lambda p: 180.0)
    monkeypatch.setattr(ambient, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(ambient, "probe", lambda p: MediaInfo(120.0, 1920, 1080, 30.0, True))

    res = ambient.generate_ambient_video("lofi_chill", str(tmp_path / "multi.mp4"), duration_sec=120.0)
    assert len(calls) == 3
    loop_cmd = " ".join(calls[1])
    assert "sin(" not in loop_cmd and "cos(" not in loop_cmd
    assert "xfade=transition=fade:duration=3.0:offset=21.0" in loop_cmd
    assert "xfade=transition=fade:duration=3.0:offset=45.0" in loop_cmd
    assert "format=rgb24" in loop_cmd and "blend=all_mode=screen" in loop_cmd
    assert res.chapters[0][1] == "Midnight Track"
    assert "0:00 Midnight Track" in res.chapter_text
    assert "Music Credits:\nMidnight Track by Test Artist | CC BY 4.0\nSibling Track by Sibling Artist | CC BY-SA 3.0" in res.description

