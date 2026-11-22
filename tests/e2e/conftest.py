"""Shared E2E fixtures, full-DB isolation, TestClient helpers, and ffmpeg/ffprobe verification helpers."""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any, Callable

import pytest

from app import db as _db_mod
from app import logging_config
from app.config import PROJECT_ROOT, settings


def _resolve_under_root(raw: str | Path, root: Path) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _assert_e2e_isolated(tmp_path: Path | None = None) -> None:
    """Fail closed if tests/e2e is not strictly isolated from live DB, .env, content/, channels.json, or logs/."""
    root = PROJECT_ROOT.resolve()

    if os.environ.get("DOOMSCROLLER_ENV_FILE") != "":
        raise RuntimeError(
            f"Refusing to run E2E tests: DOOMSCROLLER_ENV_FILE={os.environ.get('DOOMSCROLLER_ENV_FILE')!r} "
            "(must be '' set by tests/conftest.py before importing app.*)"
        )
    if (os.environ.get("DATABASE_URL") or "").strip().lower() != "sqlite:///:memory:":
        raise RuntimeError(
            f"Refusing to run E2E tests: DATABASE_URL={os.environ.get('DATABASE_URL')!r} "
            "(must be 'sqlite:///:memory:')"
        )
    if os.environ.get("USE_MOCK", "").strip().lower() != "true":
        raise RuntimeError(
            f"Refusing to run E2E tests: USE_MOCK={os.environ.get('USE_MOCK')!r} (must be 'true')"
        )

    if settings.db_url.strip().lower() != "sqlite:///:memory:":
        raise RuntimeError(
            f"Refusing to run E2E tests: settings.db_url={settings.db_url!r} (must be 'sqlite:///:memory:')"
        )

    url = _db_mod.engine.url
    if url.drivername != "sqlite" or url.database != ":memory:":
        raise RuntimeError(
            f"Refusing to run E2E tests: app.db.engine.url={url!r} is NOT in-memory SQLite! "
            "Likely cause: app.* was imported before tests/conftest.py."
        )
    if _db_mod.SessionLocal.kw.get("bind") is not _db_mod.engine:
        raise RuntimeError(
            "Refusing to run E2E tests: app.db.SessionLocal is not bound to the :memory: engine."
        )

    # Check raw content_dir and channels_file BEFORE accessing settings.content_path (which calls mkdir).
    raw_content_val = str(settings.content_dir or "").strip()
    raw_content = _resolve_under_root(raw_content_val or ".", root)
    if not raw_content_val or raw_content.is_relative_to(root):
        raise RuntimeError(
            f"Refusing to run E2E tests: content_dir={raw_content} is inside PROJECT_ROOT={root}"
        )
    if settings.content_path.resolve().is_relative_to(root):
        raise RuntimeError(
            f"Refusing to run E2E tests: content_path={settings.content_path} is inside PROJECT_ROOT={root}"
        )

    raw_channels_val = str(settings.channels_file or "").strip()
    raw_channels = _resolve_under_root(raw_channels_val or "channels.json", root)
    if (
        not raw_channels_val
        or raw_channels.is_relative_to(root)
        or settings.channels_path.resolve().is_relative_to(root)
    ):
        raise RuntimeError(
            f"Refusing to run E2E tests: channels_file={raw_channels} is inside PROJECT_ROOT={root}"
        )

    raw_logs_val = str(logging_config.LOG_DIR or "").strip()
    raw_logs = _resolve_under_root(raw_logs_val or "logs", root)
    if not raw_logs_val or raw_logs.is_relative_to(root):
        raise RuntimeError(
            f"Refusing to run E2E tests: LOG_DIR={raw_logs} is inside PROJECT_ROOT={root}"
        )

    if tmp_path is not None and tmp_path.resolve().is_relative_to(root):
        raise RuntimeError(
            f"Refusing to run E2E tests: tmp_path={tmp_path.resolve()} is inside PROJECT_ROOT={root}"
        )


# Enforce immediately at conftest import time (cannot be bypassed by collection order or --confcutdir).
_assert_e2e_isolated()

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.clipper import job as clip_job
from app.clipper.media import content_id
from app.db import init_db, session_scope
from app.growth import channels, controls
from app.longform import voiceover
from app.models import (
    Channel,
    ClipJob,
    Creator,
    Feed,
    GrowthEvent,
    GrowthState,
    LongJob,
    MetadataDraft,
    QuotaUsage,
    TransformJob,
    Video,
    VideoSnapshot,
)
from app.review import review_app
from app.winproc import background_subprocess_kwargs

E2E_REVIEW_TOKEN = "e2e-review-secret"

_SILENCE_START_RE = re.compile(r"silence_start:\s*([-+]?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([-+]?\d+(?:\.\d+)?)")
_MEAN_VOL_RE = re.compile(r"mean_volume:\s*([-+]?\d+(?:\.\d+)?)\s*dB")
_MAX_VOL_RE = re.compile(r"max_volume:\s*([-+]?\d+(?:\.\d+)?)\s*dB")
_BLACK_START_RE = re.compile(r"black_start:\s*([-+]?\d+(?:\.\d+)?)")
_BLACK_END_RE = re.compile(r"black_end:\s*([-+]?\d+(?:\.\d+)?)")

requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH",
)


@pytest.fixture(autouse=True)
def _e2e_isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Truncate every DB table in FK-safe order and isolate settings/globals for each E2E test."""
    _assert_e2e_isolated(tmp_path)
    init_db()
    with session_scope() as db:
        for model in (
            MetadataDraft,
            VideoSnapshot,
            TransformJob,
            LongJob,
            ClipJob,
            GrowthEvent,
            GrowthState,
            QuotaUsage,
            Feed,
            Video,
            Creator,
            Channel,
        ):
            db.execute(delete(model))

    content_root = tmp_path / "content"
    content_root.mkdir(parents=True, exist_ok=True)
    log_root = tmp_path / "logs"
    log_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    monkeypatch.setattr(settings, "content_dir", str(content_root))
    monkeypatch.setattr(settings, "use_mock", True)
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "review_token", E2E_REVIEW_TOKEN)
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "ntfy_topic", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    monkeypatch.setattr(settings, "ffmpeg_threads", 2)
    monkeypatch.setattr(settings, "target_languages", "")
    monkeypatch.setattr(settings, "allow_unknown_language", False)
    monkeypatch.setattr(settings, "youtube_auto_comment", False)
    monkeypatch.setattr(settings, "brand_name", "@E2EBrand")
    monkeypatch.setattr(settings, "longform_voiceover", True)
    monkeypatch.setattr(settings, "clip_layout", "fill")
    monkeypatch.setattr(logging_config, "LOG_DIR", log_root)

    controls._status.clear()
    voiceover._last_quota_alert_at = 0.0

    try:
        yield
    finally:
        controls._status.clear()
        voiceover._last_quota_alert_at = 0.0
        if clip_job.RENDER_LOCK.locked():
            try:
                clip_job.RENDER_LOCK.release()
            except RuntimeError:
                pass


@pytest.fixture()
def anon_client() -> TestClient:
    """Unauthenticated Control Panel TestClient."""
    return TestClient(review_app)


@pytest.fixture()
def auth_client() -> TestClient:
    """Cookie-authenticated Control Panel TestClient."""
    client = TestClient(review_app)
    resp = client.get(f"/?key={E2E_REVIEW_TOKEN}", follow_redirects=False)
    assert resp.status_code == 303
    return client


@pytest.fixture()
def write_channels() -> Callable[[list[dict[str, Any]]], Path]:
    """Write channels.json in the isolated tmp_path and sync into SQLite."""
    def _write(entries: list[dict[str, Any]]) -> Path:
        path = settings.channels_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"channels": entries}, indent=2), encoding="utf-8")
        with session_scope() as db:
            channels.sync_channels(db)
        return path

    return _write


def make_wav_bytes_24k(
    duration_sec: float = 2.5,
    *,
    sample_rate: int = 24_000,
    freq_hz: float = 330.0,
    amplitude: float = 0.35,
) -> bytes:
    """Generate valid 16-bit PCM WAV bytes at 24 kHz using stdlib wave/math only."""
    n_frames = max(1, int(round(duration_sec * sample_rate)))
    peak = int(max(0.0, min(1.0, amplitude)) * 32767)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        two_pi_f = 2.0 * math.pi * freq_hz
        for i in range(n_frames):
            sample = int(peak * math.sin(two_pi_f * (i / sample_rate)))
            frames.extend(struct.pack("<h", sample))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def make_wav_24k(
    path: str | Path,
    duration_sec: float = 2.5,
    *,
    sample_rate: int = 24_000,
    freq_hz: float = 330.0,
    amplitude: float = 0.35,
) -> Path:
    """Write a valid 16-bit PCM WAV file at 24 kHz."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        make_wav_bytes_24k(
            duration_sec=duration_sec,
            sample_rate=sample_rate,
            freq_hz=freq_hz,
            amplitude=amplitude,
        )
    )
    return out


def make_sarvam_tts_payload(duration_sec: float = 2.5) -> dict[str, Any]:
    """Build a Sarvam AI bulbul:v3 JSON response containing base64-encoded 24kHz WAV."""
    raw = make_wav_bytes_24k(duration_sec=duration_sec)
    return {"audios": [base64.b64encode(raw).decode("ascii")]}


def make_valid_jpeg_bytes(width: int = 1280, height: int = 720, color: tuple[int, int, int] = (40, 110, 200)) -> bytes:
    """Create valid JPEG image bytes using PIL."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def write_transcript_sidecar(
    video_path: str | Path,
    words: list[dict[str, Any]],
    *,
    language: str = "hi",
    engine: str = "sarvam",
) -> Path:
    """Write a fingerprint-matched `.transcript.json` sidecar next to `video_path`."""
    vp = str(video_path)
    sidecar = Path(f"{vp}.transcript.json")
    body = {
        "source": {"content": content_id(vp)},
        "language": language,
        "engine": engine,
        "words": words,
    }
    sidecar.write_text(json.dumps(body), encoding="utf-8")
    return sidecar


def make_synthetic_mp4(
    path: str | Path,
    *,
    duration_sec: float = 10.0,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    freq_hz: float = 440.0,
    amplitude: float = 0.25,
    dark_intro_sec: float = 0.0,
    blur_all: bool = False,
    dark_all: bool = False,
) -> Path:
    """Synthesize a real playable MP4 via ffmpeg lavfi using FFMPEG_THREADS=2 & winproc flags."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    threads = str(max(1, int(getattr(settings, "ffmpeg_threads", 2) or 2)))
    if dark_all:
        v_src = f"color=c=black:s={width}x{height}:r={fps}:d={duration_sec:.3f}"
        v_inputs = ["-f", "lavfi", "-i", v_src]
        v_filter = "[0:v]format=yuv420p[v]"
    elif dark_intro_sec > 0.05 and dark_intro_sec < duration_sec - 0.2:
        bright_dur = duration_sec - dark_intro_sec
        v_inputs = [
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}:d={dark_intro_sec:.3f}",
            "-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:r={fps}:d={bright_dur:.3f}",
        ]
        v_filter = "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]"
    else:
        v_inputs = ["-f", "lavfi", "-i", f"testsrc2=s={width}x{height}:r={fps}:d={duration_sec:.3f}"]
        v_filter = "[0:v]boxblur=20:3,format=yuv420p[v]" if blur_all else "[0:v]format=yuv420p[v]"

    audio_idx = 2 if (not dark_all and not blur_all and dark_intro_sec > 0.05 and dark_intro_sec < duration_sec - 0.2) else 1
    a_src = f"sine=f={freq_hz}:r=48000:d={duration_sec:.3f}"
    a_filter = f"[{audio_idx}:a]volume={amplitude:.3f},aformat=sample_rates=48000:channel_layouts=stereo[a]"

    cmd = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
        "-filter_threads", threads, "-filter_complex_threads", threads,
        *v_inputs,
        "-f", "lavfi", "-i", a_src,
        "-filter_complex", f"{v_filter};{a_filter}",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-threads", threads,
        "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        **background_subprocess_kwargs(low_priority=True),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"synthetic mp4 generation failed ({proc.returncode}): {proc.stderr[-500:]}")
    return out


def probe_media(path: str | Path) -> dict[str, Any]:
    """Run ffprobe and return parsed format + streams summary."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        **background_subprocess_kwargs(low_priority=True),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed ({proc.returncode}): {proc.stderr[-400:]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    return {
        "duration": float(fmt.get("duration") or (v_stream or {}).get("duration") or 0.0),
        "video": v_stream,
        "audio": a_stream,
        "raw": data,
    }


def _run_ffmpeg_filter_log(path: str | Path, args: list[str]) -> str:
    threads = str(max(1, int(getattr(settings, "ffmpeg_threads", 2) or 2)))
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
        "-filter_threads", threads,
        "-i", str(path),
        *args,
        "-f", "null", "-",
    ]
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        **background_subprocess_kwargs(low_priority=True),
    )
    return proc.stderr or ""


def detect_silences(path: str | Path, *, noise_db: float = -34.0, min_dur: float = 0.25) -> list[tuple[float, float]]:
    """Return list of (start, end) silent spans detected in `path`."""
    stderr = _run_ffmpeg_filter_log(path, ["-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}"])
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(stderr)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(stderr)]
    spans: list[tuple[float, float]] = []
    for idx, st in enumerate(starts):
        en = ends[idx] if idx < len(ends) else float("inf")
        spans.append((st, en))
    return spans


def detect_volume(path: str | Path) -> dict[str, float]:
    """Return `mean_volume` and `max_volume` in dBFS using ffmpeg volumedetect."""
    stderr = _run_ffmpeg_filter_log(path, ["-vn", "-af", "volumedetect"])
    mean_m = _MEAN_VOL_RE.search(stderr)
    max_m = _MAX_VOL_RE.search(stderr)
    return {
        "mean_volume": float(mean_m.group(1)) if mean_m else -99.0,
        "max_volume": float(max_m.group(1)) if max_m else -99.0,
    }


def detect_black_frames(path: str | Path, *, duration: float = 0.2, pic_th: float = 0.98) -> list[tuple[float, float]]:
    """Return list of (start, end) black frame intervals detected in `path`."""
    stderr = _run_ffmpeg_filter_log(path, ["-an", "-vf", f"blackdetect=d={duration}:pic_th={pic_th}"])
    starts = [float(m.group(1)) for m in _BLACK_START_RE.finditer(stderr)]
    ends = [float(m.group(1)) for m in _BLACK_END_RE.finditer(stderr)]
    return [(starts[i], ends[i] if i < len(ends) else float("inf")) for i in range(len(starts))]
