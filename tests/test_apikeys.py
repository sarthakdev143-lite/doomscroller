"""Backup API keys in one setting ("key1,key2"): parsing, failover on 401/403, and log scrubbing."""
from __future__ import annotations

import json
import logging

import pytest

from app import apikeys
from app.clipper import ai, transcribe as tr, whisper_stt
from app.config import settings
from app.logging_config import scrub_secrets

GOOD, BAD = "gsk_good_key_0123456789abcdef", "gsk_dead_key_0123456789abcdef"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    apikeys.reset()
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    yield
    apikeys.reset()


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _groq(monkeypatch, key):
    monkeypatch.setattr(settings, "clip_ai_backend", "api")
    monkeypatch.setattr(settings, "clip_ai_api_provider", "openai_compatible")
    monkeypatch.setattr(settings, "clip_ai_api_key", key)
    monkeypatch.setattr(settings, "clip_ai_api_model", "openai/gpt-oss-120b")
    monkeypatch.setattr(settings, "clip_ai_api_base_url", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "clip_ai_api_vision_model", "")
    monkeypatch.setattr(settings, "clip_ai_api_effort", "low")
    monkeypatch.setattr(ai, "opencode_path", lambda: pytest.fail("the api backend never runs a CLI"))
    monkeypatch.setattr(ai, "claude_path", lambda: pytest.fail("the api backend never runs a CLI"))


def _ok(text="hello"):
    return _Response(200, {"model": "openai/gpt-oss-120b", "usage": {},
                           "choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


# ---------- parsing and state ----------

@pytest.mark.parametrize("raw, keys", [
    ("", []),
    ("k1", ["k1"]),
    ("k1,k2", ["k1", "k2"]),
    (" k1 ; k2 | k3 ", ["k1", "k2", "k3"]),
    ('"k1, k2"', ["k1", "k2"]),
    ("k1,k1,k2", ["k1", "k2"]),
    ("k1\nk2", ["k1", "k2"]),
])
def test_a_key_setting_reads_as_a_list(raw, keys):
    assert apikeys.parse(raw) == keys


def test_a_refused_key_goes_last_until_it_expires_or_works(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(apikeys.time, "monotonic", lambda: clock[0])
    apikeys.refuse("k1")
    assert apikeys.candidates("k1,k2") == ["k2", "k1"] and apikeys.first("k1,k2") == "k2"
    assert apikeys.candidates("k1") == ["k1"]  # one key behaves exactly as before
    clock[0] += apikeys._REFUSED_SEC + 1
    assert apikeys.candidates("k1,k2") == ["k1", "k2"]
    apikeys.refuse("k1")
    apikeys.accept("k1")
    assert apikeys.first("k1,k2") == "k1"


def test_labels_never_contain_the_key():
    assert apikeys.label("k2", "k1,k2,k3") == "key 2 of 3"


# ---------- AI over HTTP ----------

def test_a_dead_main_key_falls_over_to_the_backup_and_stays_there(monkeypatch, caplog):
    _groq(monkeypatch, f"{BAD},{GOOD}")
    used = []

    def post(url, json=None, headers=None, timeout=None):
        key = headers["authorization"].split()[-1]
        used.append(key)
        return _ok() if key == GOOD else _Response(401, {"error": {"message": f"Invalid API Key {key}"}})

    monkeypatch.setattr(ai.httpx, "post", post)
    with caplog.at_level(logging.WARNING):
        assert ai.ask("hi") == "hello"
    assert used == [BAD, GOOD]
    assert "key 1 of 2" in caplog.text and BAD not in caplog.text and GOOD not in caplog.text
    used.clear()
    assert ai.ask("again") == "hello"
    assert used == [GOOD]  # the refused key is not retried on every call


def test_one_refused_key_still_fails_once_as_before(monkeypatch):
    _groq(monkeypatch, BAD)
    calls = []
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: calls.append(1) or _Response(401, {"error": {"message": "no"}}))
    assert ai.ask("hi") is None
    assert len(calls) == 1


def test_a_rate_limit_never_switches_keys(monkeypatch):
    _groq(monkeypatch, f"{GOOD},{BAD}")
    used = []

    def post(url, json=None, headers=None, timeout=None):
        used.append(headers["authorization"].split()[-1])
        return _Response(429, {"error": {"message": "rate limit reached"}}) if len(used) == 1 else _ok()

    monkeypatch.setattr(ai.httpx, "post", post)
    assert ai.ask("hi") == "hello"
    assert used == [GOOD, GOOD]


def test_every_backup_key_is_scrubbed_from_ai_errors(monkeypatch, caplog):
    _groq(monkeypatch, f"{BAD},{GOOD}")
    monkeypatch.setattr(ai.httpx, "post",
                        lambda *a, **k: _Response(403, {"error": {"message": f"keys {BAD} and {GOOD} are blocked"}}))
    with caplog.at_level(logging.WARNING):
        assert ai.ask("hi") is None
    assert BAD not in caplog.text and GOOD not in caplog.text


# ---------- speech to text ----------

def test_deepgram_uses_the_backup_key_when_the_first_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deepgram_api_key", f"{BAD},{GOOD}")
    sent = []

    def post(url, params=None, headers=None, content=None, timeout=None):
        key = headers["Authorization"].split()[-1]
        sent.append(key)
        return (_Response(200, {"results": {"channels": []}}) if key == GOOD
                else _Response(401, {"err_code": "INVALID_AUTH"}))

    monkeypatch.setattr(tr.httpx, "post", post)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"mp3")
    assert tr._request(str(audio)) == {"results": {"channels": []}}
    assert sent == [BAD, GOOD]


def test_deepgram_with_every_key_refused_reports_the_refusal(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deepgram_api_key", f"{BAD},{GOOD}")
    monkeypatch.setattr(tr.httpx, "post", lambda *a, **k: _Response(401, {"err_code": "INVALID_AUTH"}))
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"mp3")
    with pytest.raises(tr.TranscribeError) as caught:
        tr._request(str(audio))
    assert "401" in str(caught.value)


def test_whisper_uses_the_backup_key_when_the_first_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "clip_stt_api_key", f"{BAD},{GOOD}")
    monkeypatch.setattr(settings, "clip_stt_base_url", "https://api.groq.com/openai/v1")
    sent = []

    def post(url, headers=None, data=None, files=None, timeout=None):
        key = headers["authorization"].split()[-1]
        sent.append(key)
        return _Response(200, {"text": "hi", "words": []}) if key == GOOD else _Response(401, {"error": "invalid"})

    monkeypatch.setattr(whisper_stt.httpx, "post", post)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"mp3")
    assert whisper_stt._request(str(audio)) == {"text": "hi", "words": []}
    assert sent == [BAD, GOOD]


# ---------- log scrubbing ----------

def test_the_log_scrubber_hides_each_backup_key(monkeypatch):
    monkeypatch.setattr(settings, "deepgram_api_key", f"{BAD},{GOOD}")
    monkeypatch.setattr(settings, "clip_ai_api_key", f"{GOOD};{BAD}")
    for line in (f"Token {GOOD} refused", f"used {BAD}", f"DEEPGRAM_API_KEY={BAD},{GOOD}",
                 f"CLIP_AI_API_KEY={GOOD},{BAD} in env", json.dumps({"clip_stt_api_key": f"{BAD},{GOOD}"})):
        cleaned = scrub_secrets(line)
        assert BAD not in cleaned and GOOD not in cleaned, cleaned


def test_an_unconfigured_env_line_with_two_keys_is_scrubbed_whole(monkeypatch):
    for attr in ("deepgram_api_key", "clip_ai_api_key", "clip_stt_api_key"):
        monkeypatch.setattr(settings, attr, "")
    cleaned = scrub_secrets("DEEPGRAM_API_KEY=aaaa1111bbbb2222,cccc3333dddd4444 loaded")
    assert "aaaa1111" not in cleaned and "cccc3333" not in cleaned and cleaned.endswith(" loaded")
