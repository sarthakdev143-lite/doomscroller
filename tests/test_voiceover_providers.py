"""Tests for multi-provider ElevenLabs eleven_v4 + Cartesia sonic-3 + Sarvam bulbul:v3 TTS,
SHA-256 disk caching, secret scrubbing, and longform cost/supply guards.
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels, notify
from app.longform import job as long_job, voiceover
from app.models import Channel, LongJob, Video


def _fake_wav_bytes(duration_sec: float = 0.8, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x01\x00" * int(rate * duration_sec))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def isolated_tts_env(monkeypatch, tmp_path):
    init_db()
    with session_scope() as db:
        for tbl in (LongJob, Video, Channel):
            db.execute(delete(tbl))
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {"slug": "default", "niche": "challenge", "long_uploads_per_day": 1, "feeds": ["https://www.youtube.com/@MrBeast"]},
                {"slug": "indianvlogs", "niche": "hindi", "long_uploads_per_day": 1, "feeds": ["https://www.youtube.com/@souravjvlogs"]},
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    monkeypatch.setattr(settings, "eleven_labs_api_key", "")
    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    monkeypatch.setattr(settings, "longform_tts_provider", "auto")
    monkeypatch.setattr(settings, "longform_auto_supply", True)
    monkeypatch.setattr(settings, "ntfy_topic", "test-topic")
    voiceover._last_quota_alert_at = 0.0


def test_voiceover_import_does_not_pull_numpy_or_cv2():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, app.longform.voiceover; "
                "assert 'numpy' not in sys.modules, 'numpy imported eagerly'; "
                "assert 'cv2' not in sys.modules, 'cv2 imported eagerly'"
            ),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_en_primary_eleven_v4_and_failover_to_cartesia_then_sarvam(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "eleven_labs_api_key", "el_secret_123")
    monkeypatch.setattr(settings, "cartesia_api_key", "ca_secret_456")
    monkeypatch.setattr(settings, "sarvam_api_key", "sa_secret_789")
    monkeypatch.setattr(voiceover, "run_ffmpeg", lambda args, cwd=None: Path(args[-1]).write_bytes(_fake_wav_bytes(1.0)))

    calls: list[tuple[str, dict]] = []

    def _fake_post(url, *, headers=None, json=None, timeout=None):
        calls.append((url, json or {}))
        if "api.elevenlabs.io" in url:
            return SimpleNamespace(status_code=429, text="el quota exceeded el_secret_123")
        if "api.cartesia.ai" in url:
            return SimpleNamespace(status_code=200, content=_fake_wav_bytes(1.0), text="")
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(voiceover.httpx, "post", _fake_post)
    dur = voiceover.synthesize_wav("MrBeast just dropped a wild challenge!", tmp_path / "en_failover.wav", language="en")
    assert dur is not None and dur >= 0.4
    assert len(calls) == 2
    assert "api.elevenlabs.io/v1/text-to-speech/" in calls[0][0]
    assert calls[0][1]["model_id"] == "eleven_v4"
    assert calls[1][0] == "https://api.cartesia.ai/tts/bytes"
    assert calls[1][1]["model_id"] == "sonic-3"
    assert calls[1][1]["language"] == "en"

    # Now fail BOTH ElevenLabs & Cartesia -> Sarvam bulbul:v3 takes over for new text.
    calls.clear()

    def _fake_post_3(url, *, headers=None, json=None, timeout=None):
        calls.append((url, json or {}))
        if "api.elevenlabs.io" in url or "api.cartesia.ai" in url:
            return SimpleNamespace(status_code=402, text="credits exhausted")
        if "api.sarvam.ai" in url:
            b64 = base64.b64encode(_fake_wav_bytes(1.1)).decode("ascii")
            return SimpleNamespace(status_code=200, json=lambda: {"audios": [b64]}, text="ok")
        raise AssertionError(url)

    monkeypatch.setattr(voiceover.httpx, "post", _fake_post_3)
    dur2 = voiceover.synthesize_wav("Another brand new English line.", tmp_path / "en_sarvam.wav", language="en")
    assert dur2 is not None and dur2 >= 0.4
    assert [c[0].split("/")[2] for c in calls] == ["api.elevenlabs.io", "api.cartesia.ai", "api.sarvam.ai"]


def test_hindi_prefers_sarvam_then_cartesia_then_eleven_v4_and_caches_sha256(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "el_hi_secret")
    monkeypatch.setattr(settings, "cartesia_api_key", "ca_hi_secret")
    monkeypatch.setattr(settings, "sarvam_api_key", "sa_hi_secret")
    monkeypatch.setattr(voiceover, "run_ffmpeg", lambda args, cwd=None: Path(args[-1]).write_bytes(_fake_wav_bytes(1.3)))

    calls: list[tuple[str, dict]] = []

    def _fake_hi_post(url, *, headers=None, json=None, timeout=None):
        calls.append((url, json or {}))
        if "api.sarvam.ai" in url:
            return SimpleNamespace(status_code=503, text="sarvam temporarily down")
        if "api.cartesia.ai" in url:
            return SimpleNamespace(status_code=200, content=_fake_wav_bytes(1.3), text="")
        raise AssertionError("Should not reach paid ElevenLabs when Cartesia succeeds for Hindi")

    monkeypatch.setattr(voiceover.httpx, "post", _fake_hi_post)
    out1 = tmp_path / "hi_1.wav"
    out2 = tmp_path / "hi_2.wav"
    d1 = voiceover.synthesize_wav("भाई ये मोमेंट सबसे कमाल था!", out1, language="hi")
    assert d1 is not None and d1 >= 0.4
    assert len(calls) == 2
    assert "api.sarvam.ai" in calls[0][0]
    assert "api.cartesia.ai" in calls[1][0]
    assert calls[1][1]["language"] == "hi"

    # Second identical call hits SHA-256 disk cache without hitting any HTTP API!
    d2 = voiceover.synthesize_wav("भाई ये मोमेंट सबसे कमाल था!", out2, language="hi")
    assert d2 == pytest.approx(d1, rel=1e-3)
    assert len(calls) == 2
    assert out2.is_file() and out2.stat().st_size == out1.stat().st_size


def test_all_providers_quota_exhausted_scrubs_all_secrets_and_notifies_once(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "SECRET_EL_999")
    monkeypatch.setattr(settings, "cartesia_api_key", "SECRET_CA_888")
    monkeypatch.setattr(settings, "sarvam_api_key", "SECRET_SA_777")

    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "send", lambda text, *, title="", link="": alerts.append((title, text)) or True)
    monkeypatch.setattr(
        voiceover.httpx,
        "post",
        lambda url, **kw: SimpleNamespace(
            status_code=402,
            text=f"quota failed SECRET_EL_999 SECRET_CA_888 SECRET_SA_777 token={settings.review_token}",
        ),
    )

    with pytest.raises(voiceover.SarvamTTSQuotaError) as exc_info:
        voiceover.synthesize_wav("Test quota scrub", tmp_path / "q.wav", language="en")

    msg = str(exc_info.value)
    for secret in ("SECRET_EL_999", "SECRET_CA_888", "SECRET_SA_777"):
        assert secret not in msg
        assert len(alerts) == 1 and secret not in alerts[0][1]
    assert alerts[0][0] == "TTS Voiceover Quota Alert"


def test_longform_auto_supply_flag_blocks_unattended_compilation_build_only(monkeypatch):
    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    monkeypatch.setattr(settings, "longform_auto_supply", False)

    res = long_job.long_supply_job()
    assert res["default"]["skipped"] == "auto-supply disabled"
    assert res["indianvlogs"]["skipped"] == "auto-supply disabled"

    # Manual build trigger still works cleanly when requested by owner.
    jid = long_job.enqueue_long_job("default", format_kind="best_of", target="@MrBeast")
    assert jid


def test_tts_status_and_panel_payload_report_chains_without_leaking_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "eleven_labs_api_key", "TOP_SECRET_EL")
    monkeypatch.setattr(settings, "cartesia_api_key", "TOP_SECRET_CA")
    monkeypatch.setattr(settings, "sarvam_api_key", "TOP_SECRET_SA")
    monkeypatch.setattr(settings, "longform_auto_supply", False)

    st = voiceover.tts_status()
    assert st["en_chain"] == ["elevenlabs", "cartesia", "sarvam"]
    assert st["hi_chain"] == ["sarvam", "cartesia", "elevenlabs"]
    assert st["elevenlabs_model"] == "eleven_v4"
    assert st["auto_supply"] is False
    panel = long_job.long_panel_data()
    assert panel["tts_status"] == st
    blob = json.dumps(panel)
    for sec in ("TOP_SECRET_EL", "TOP_SECRET_CA", "TOP_SECRET_SA"):
        assert sec not in blob

