"""The clipper's SaaS-facing parts: cost metering, the HTTP AI backends, cheaper STT routing, and the
standalone ``clip_to_directory`` seam. Nothing here touches a network, a CLI or real ffmpeg."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.clipper import ai, metering, memes, standalone, transcribe as tr, whisper_stt
from app.clipper.media import MediaInfo
from app.clipper.moments import Moment
from app.clipper.transcribe import Transcript, Word
from app.config import settings

INFO = MediaInfo(duration=600.0, width=1280, height=720, fps=30.0, has_audio=True)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)


class _Response:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


# ---------- metering ----------

def test_recording_without_a_meter_is_a_no_op():
    assert metering.active() is None
    metering.record("llm", provider="x")  # must not raise or leak into a later meter
    with metering.metered() as meter:
        assert meter.summary()["entries"] == 0


def test_a_meter_collects_every_kind_and_summarises():
    with metering.metered() as meter:
        metering.record("stt", provider="deepgram", model="nova-3", audio_seconds=30)
        metering.record("llm", provider="anthropic", model="m", input_tokens=10, output_tokens=5, images=1, ok=True)
        metering.record("llm", provider="anthropic", model="m", ok=False)
        with metering.timed("render", stage="clip", output_seconds=20.0):
            pass
        metering.record("download", source="broll", bytes=1000, seconds=1.0)
        metering.record("api", provider="pixabay", requests=1)
    summary = meter.summary()
    assert summary["stt_seconds"] == {"deepgram:nova-3": 30.0}
    assert summary["llm"]["anthropic:m"]["calls"] == 2 and summary["llm"]["anthropic:m"]["failed"] == 1
    assert summary["llm"]["anthropic:m"]["input_tokens"] == 10 and summary["render"]["output_seconds"] == 20.0
    assert summary["download"]["broll_bytes"] == 1000 and summary["api_requests"] == {"pixabay": 1.0}


def test_deepgram_spend_is_metered_with_its_audio_length(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deepgram_api_key", "k")
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    monkeypatch.setattr(tr, "run_ffmpeg", lambda args, **k: Path(args[-1]).write_bytes(b"mp3"))
    monkeypatch.setattr(tr, "_request", lambda path: {"results": {"channels": [{"detected_language": "en", "alternatives": [
        {"words": [{"word": "hi", "punctuated_word": "Hi.", "start": 0.1, "end": 0.3, "confidence": 0.99}]}]}]}})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    with metering.metered() as meter:
        tr.transcribe(str(video), MediaInfo(120.0, 1280, 720, 30.0, True))
    assert meter.summary()["stt_seconds"] == {f"deepgram:{settings.deepgram_model}": 120.0}


def test_broll_and_meme_requests_and_downloads_are_metered(monkeypatch, tmp_path):
    from app.clipper import broll

    class Stream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            yield b"x" * 300

    monkeypatch.setattr(broll.httpx, "stream", lambda *a, **k: Stream())
    monkeypatch.setattr(broll.httpx, "get", lambda *a, **k: _Response(200, {"hits": []}))
    monkeypatch.setattr(settings, "pixabay_api_key", "k")
    monkeypatch.setattr(settings, "pexels_api_key", "")
    with metering.metered() as meter:
        broll._download("https://cdn/x.mp4", tmp_path / "b" / "x.mp4")
        memes._download("https://cdn/m.mp4", tmp_path / "m" / "m.mp4")
        broll.search("rain")
    summary = meter.summary()
    assert summary["download"]["broll_bytes"] == 300 and summary["download"]["meme_bytes"] == 300
    assert summary["api_requests"] == {"pixabay": 1.0}


def test_each_cli_ai_call_is_metered(monkeypatch):
    monkeypatch.setattr(ai, "opencode_path", lambda: None)
    monkeypatch.setattr(ai, "claude_path", lambda: "claude")
    events = '{"type":"result","result":"ok","usage":{"input_tokens":12,"output_tokens":3,"cache_read_input_tokens":5}}'
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, events, ""))
    with metering.metered() as meter:
        assert ai.ask("hello") == "ok"
    llm = meter.summary()["llm"]
    assert llm[f"claude-cli:{settings.claude_model}"] == {"calls": 1, "failed": 0, "input_tokens": 17,
                                                          "output_tokens": 3, "images": 0}


# ---------- HTTP AI backends ----------

def _api(monkeypatch, provider="anthropic", **extra):
    monkeypatch.setattr(settings, "clip_ai_backend", "api")
    monkeypatch.setattr(settings, "clip_ai_api_provider", provider)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test-key")
    monkeypatch.setattr(settings, "clip_ai_api_key", extra.get("key", ""))
    monkeypatch.setattr(settings, "clip_ai_api_model", extra.get("model", ""))
    monkeypatch.setattr(settings, "clip_ai_api_base_url", extra.get("base", ""))
    monkeypatch.setattr(settings, "clip_ai_api_vision_model", extra.get("vision", ""))
    monkeypatch.setattr(ai, "opencode_path", lambda: pytest.fail("the api backend never runs a CLI"))
    monkeypatch.setattr(ai, "claude_path", lambda: pytest.fail("the api backend never runs a CLI"))


def test_the_owner_default_is_still_the_cli_backend():
    from app.config import Settings
    fresh = Settings()
    assert fresh.clip_ai_backend == "cli" and fresh.clip_stt_provider == "deepgram"


def test_the_anthropic_default_model_is_the_cheapest(monkeypatch):
    _api(monkeypatch)
    assert ai._api_model([]) == "claude-haiku-5-5"


def test_anthropic_request_shape_usage_and_images(monkeypatch, tmp_path):
    _api(monkeypatch, model="claude-opus-5-5")  # a fallback-capable model, to cover the beta header
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return _Response(200, {"model": "claude-opus-5-5", "stop_reason": "end_turn",
                               "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": '{"ok": true}'}],
                               "usage": {"input_tokens": 1200, "output_tokens": 40, "cache_read_input_tokens": 0}})

    monkeypatch.setattr(ai.httpx, "post", post)
    image = tmp_path / "sheet.jpg"
    image.write_bytes(b"\xff\xd8jpeg")
    with metering.metered() as meter:
        assert ai.ask_json("Judge these. Reply with JSON only.", files=[str(image)]) == {"ok": True}
    url, body, headers = sent[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant-test-key" and headers["anthropic-version"] == "2026-10-21"
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01" and body["fallbacks"] == "default"
    assert body["model"] == "claude-opus-5-5" and "temperature" not in body and "tools" not in body
    blocks = body["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/jpeg"
    assert blocks[-1]["type"] == "text" and blocks[-1]["text"].endswith("Judge these. Reply with JSON only.")
    assert blocks[-1]["text"].startswith("Answer with one JSON object only")
    llm = meter.summary()["llm"]["anthropic:claude-opus-5-5"]
    assert llm["input_tokens"] == 1200 and llm["output_tokens"] == 40 and llm["images"] == 1


def test_anthropic_refusal_rate_limit_and_errors(monkeypatch, caplog):
    _api(monkeypatch)
    replies = iter([_Response(429, {"error": {"message": "slow down"}}, {"retry-after": "1"}),
                    _Response(200, {"stop_reason": "refusal", "content": [], "usage": {}})])
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: next(replies))
    assert ai.ask("x") is None
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: _Response(401, {"error": {"message": "bad key sk-ant-test-key"}}))
    with caplog.at_level(logging.WARNING):
        assert ai.ask("x") is None
    assert "sk-ant-test-key" not in caplog.text and "401" in caplog.text

    def down(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(ai.httpx, "post", down)
    assert ai.ask("x") is None


def test_openai_compatible_provider_for_gemini_or_groq(monkeypatch, tmp_path):
    _api(monkeypatch, provider="openai_compatible", key="gk", model="gemini-2.5-flash",
         base="https://generativelanguage.googleapis.com/v1beta/openai", vision="gemini-2.5-flash")
    assert ai.available() and ai.can_see()
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return _Response(200, {"model": "gemini-2.5-flash", "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 50, "completion_tokens": 2}})

    monkeypatch.setattr(ai.httpx, "post", post)
    image = tmp_path / "f.png"
    image.write_bytes(b"png")
    with metering.metered() as meter:
        assert ai.ask("describe", files=[str(image)]) == "hello"
    url, body, headers = sent[0]
    assert url.endswith("/v1beta/openai/chat/completions") and headers["authorization"] == "Bearer gk"
    assert body["messages"][0] == {"role": "system", "content": ai._CLAUDE_SYSTEM}
    parts = body["messages"][1]["content"]
    assert parts[0]["type"] == "text" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert meter.summary()["llm"]["openai_compatible:gemini-2.5-flash"]["input_tokens"] == 50


def test_openai_compatible_needs_a_model_and_a_base_url(monkeypatch):
    _api(monkeypatch, provider="openai_compatible", key="gk")
    assert not ai.available()


# The free Groq setup, exactly as CONTRACT.md documents it (models checked against Groq's docs 2026-11-27:
# gpt-oss-120b and whisper-large-v3-turbo are production models; Groq lists no vision model).
GROQ_PRESET = {
    "CLIP_AI_BACKEND": "api",
    "CLIP_AI_API_PROVIDER": "openai_compatible",
    "CLIP_AI_API_BASE_URL": "https://api.groq.com/openai/v1",
    "CLIP_AI_API_KEY": "gsk_test",
    "CLIP_AI_API_MODEL": "openai/gpt-oss-120b",
    "CLIP_AI_API_EFFORT": "low",
    "CLIP_AI_API_MAX_TOKENS": "2048",
    "CLIP_STT_PROVIDER": "openai_compatible",
    "CLIP_STT_BASE_URL": "https://api.groq.com/openai/v1",
    "CLIP_STT_API_KEY": "gsk_test",
    "CLIP_STT_MODEL": "whisper-large-v3-turbo",
}


@pytest.fixture
def groq(monkeypatch):
    from app.config import Settings
    for name, value in GROQ_PRESET.items():
        monkeypatch.setenv(name, value)
    parsed = Settings(_env_file=None)
    for name in GROQ_PRESET:
        monkeypatch.setattr(settings, name.lower(), getattr(parsed, name.lower()))
    monkeypatch.setattr(settings, "clip_ai_api_vision_model", "")
    monkeypatch.setattr(ai, "opencode_path", lambda: pytest.fail("the api backend never runs a CLI"))
    monkeypatch.setattr(ai, "claude_path", lambda: pytest.fail("the api backend never runs a CLI"))
    return parsed


def test_the_groq_preset_parses_and_asks_gpt_oss_with_a_budget_that_fits_the_free_tier(groq, monkeypatch):
    from app.clipper import whisper_stt
    assert groq.clip_ai_api_max_tokens == 2048 and ai.available() and whisper_stt.available()
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, headers))
        return _Response(200, {"model": "openai/gpt-oss-120b", "usage": {"prompt_tokens": 9, "completion_tokens": 3},
                               "choices": [{"message": {"content": '{"ok": 1}'}, "finish_reason": "stop"}]})

    monkeypatch.setattr(ai.httpx, "post", post)
    assert ai.ask_json("Pick. JSON only.") == {"ok": 1}
    url, body, headers = sent[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions" and headers["authorization"] == "Bearer gsk_test"
    assert body["model"] == "openai/gpt-oss-120b" and body["max_tokens"] == 2048 and body["reasoning_effort"] == "low"
    assert isinstance(body["messages"][1]["content"], str)  # plain text, no image parts for a text model


def test_without_a_vision_model_pictures_are_never_sent_and_broll_turns_off_with_a_note(groq, monkeypatch, tmp_path):
    from app.clipper import edit, subtitles
    from app.clipper.edit import Cutaway, EditPlan
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: pytest.fail("no picture may reach a text-only model"))
    sheet = tmp_path / "sheet.jpg"
    sheet.write_bytes(b"\xff\xd8jpeg")
    assert not ai.can_see() and ai.ask_json("Judge.", files=[str(sheet)]) is None
    monkeypatch.setattr(edit.broll, "available", lambda: True)
    plan = EditPlan(words=[], segments=[(0.0, 30.0)], cutaways=[Cutaway(5.0, 2.0, "red car", subject="car")])
    notes = edit.attach_stock(plan)
    assert plan.cutaways == [] and "cannot look at pictures" in notes[0]
    monkeypatch.setattr(subtitles, "run_ffmpeg", lambda *a, **k: pytest.fail("no frames are pulled for nothing"))
    assert subtitles.already_captioned(str(sheet), INFO) is False


def test_without_vision_visual_moments_are_ranked_by_rules_not_dropped(groq, monkeypatch):
    from app.clipper import moments
    picked = [Moment(10.0, 30.0, kind="visual", score=40.0), Moment(60.0, 80.0, kind="visual", score=30.0)]
    monkeypatch.setattr(moments, "rank_windows", lambda *a, **k: list(picked))
    monkeypatch.setattr(moments, "_ai_rank_visual", lambda *a, **k: pytest.fail("no frames go to a text model"))
    signals = type("S", (), {"duration": 600.0})()
    # The seam asks for a 75 floor whenever an AI is configured; a text-only one must not empty the job.
    got = moments.pick_visual("v.mp4", INFO, signals, count=2, title="Talk", min_score=75.0)
    assert [m.start for m in got] == [10.0, 60.0]


def test_a_vision_model_turns_picture_checks_back_on(groq, monkeypatch):
    monkeypatch.setattr(settings, "clip_ai_api_vision_model", "a-vision-model")
    assert ai.can_see() and ai._api_model(["x.jpg"]) == "a-vision-model"


def test_groq_request_too_large_retries_once_with_a_reply_budget_that_fits(groq, monkeypatch):
    monkeypatch.setattr(settings, "clip_ai_api_max_tokens", 4096)
    budgets = []
    too_large = ("Request too large for model `openai/gpt-oss-120b` in organization `org_x` service tier "
                 "`on_demand` on tokens per minute (TPM): Limit 8000, Requested 8881, please reduce your message size")

    def post(url, json=None, headers=None, timeout=None):
        budgets.append(json["max_tokens"])
        if len(budgets) == 1:
            return _Response(413, {"error": {"message": too_large, "type": "tokens", "code": "rate_limit_exceeded"}})
        return _Response(200, {"choices": [{"message": {"content": "fits"}, "finish_reason": "stop"}], "usage": {}})

    monkeypatch.setattr(ai.httpx, "post", post)
    assert ai.ask("x") == "fits" and budgets == [4096, 4096 - 881 - 256]
    # A prompt that is itself over the limit is not retried into a uselessly small reply.
    budgets.clear()
    monkeypatch.setattr(settings, "clip_ai_api_max_tokens", 2048)
    monkeypatch.setattr(ai.httpx, "post", lambda url, json=None, **k: budgets.append(json["max_tokens"]) or
                        _Response(413, {"error": {"message": too_large.replace("8881", "9900")}}))
    assert ai.ask("x") is None and budgets == [2048]


def _thinking_post(bodies, *, answer='{"clips": []}'):
    """gpt-oss-120b at Groq's default effort: the whole reply budget goes on reasoning and nothing is answered."""
    def post(url, json=None, headers=None, timeout=None):
        bodies.append(dict(json))
        if json.get("reasoning_effort") == "low":
            return _Response(200, {"model": json["model"], "choices": [{"message": {"content": answer},
                                                                         "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 1394, "completion_tokens": 300,
                                             "completion_tokens_details": {"reasoning_tokens": 120}}})
        return _Response(200, {"model": json["model"], "choices": [{"message": {"content": '{"clips": [{"first": 5,'},
                                                                     "finish_reason": "length"}],
                               "usage": {"prompt_tokens": 1394, "completion_tokens": 2048,
                                         "completion_tokens_details": {"reasoning_tokens": 2046}}})
    return post


def test_a_reply_the_model_spent_thinking_is_asked_again_at_low_effort(groq, monkeypatch):
    monkeypatch.setattr(settings, "clip_ai_api_effort", "")  # the owner's .env.saas left it out
    monkeypatch.setattr(ai, "_low_effort_models", set())
    bodies = []
    monkeypatch.setattr(ai.httpx, "post", _thinking_post(bodies))
    with metering.metered() as meter:
        assert ai.ask_json("Pick the moments. JSON only.") == {"clips": []}
    assert [b.get("reasoning_effort") for b in bodies] == [None, "low"]
    llm = meter.summary()["llm"]["openai_compatible:openai/gpt-oss-120b"]
    assert llm["calls"] == 2 and llm["failed"] == 1 and llm["output_tokens"] == 2348  # the wasted call is still billed
    # Every later call to that model starts at low effort.
    bodies.clear()
    assert ai.ask_json("Again.") == {"clips": []} and [b.get("reasoning_effort") for b in bodies] == ["low"]


def test_an_effort_the_operator_chose_is_never_overridden_and_a_cut_off_reply_still_gets_the_strict_retry(
        groq, monkeypatch, caplog):
    monkeypatch.setattr(settings, "clip_ai_api_effort", "medium")
    monkeypatch.setattr(ai, "_low_effort_models", set())
    bodies = []
    monkeypatch.setattr(ai.httpx, "post", _thinking_post(bodies))
    with caplog.at_level(logging.WARNING, logger="app.clipper.ai"):
        assert ai.ask("Pick.") == '{"clips": [{"first": 5,'  # handed back, flagged, never silently dropped
    assert [b.get("reasoning_effort") for b in bodies] == ["medium"]
    assert any("cut off at its 2048-token reply budget" in r.getMessage() for r in caplog.records)
    # ask_json still makes its stricter second attempt, which a shorter reply can fit.
    bodies.clear()
    replies = iter([_Response(200, {"model": "openai/gpt-oss-120b", "usage": {"completion_tokens": 2048},
                                    "choices": [{"message": {"content": '{"clips": [{"first"'}, "finish_reason": "length"}]}),
                    _Response(200, {"model": "openai/gpt-oss-120b", "usage": {"completion_tokens": 400},
                                    "choices": [{"message": {"content": '{"clips": []}'}, "finish_reason": "stop"}]})])
    monkeypatch.setattr(ai.httpx, "post", lambda url, json=None, **k: bodies.append(json) or next(replies))
    assert ai.ask_json("Pick.") == {"clips": []} and len(bodies) == 2


def test_low_effort_is_remembered_only_once_the_model_accepts_it(groq, monkeypatch):
    monkeypatch.setattr(settings, "clip_ai_api_effort", "")
    monkeypatch.setattr(ai, "_low_effort_models", set())
    bodies = []
    thinking = _thinking_post([])

    def refuses_low(url, json=None, headers=None, timeout=None):
        bodies.append(dict(json))
        if json.get("reasoning_effort"):
            return _Response(400, {"error": {"message": "`reasoning_effort` is not supported with this model"}})
        return thinking(url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setattr(ai.httpx, "post", refuses_low)
    assert ai.ask("Pick.") is None and [b.get("reasoning_effort") for b in bodies] == [None, "low"]
    assert ai._low_effort_models == set()
    bodies.clear()
    ai.ask("Again.")
    assert bodies[0].get("reasoning_effort") is None  # later calls are not poisoned by the refused parameter


def test_transient_retries_are_not_used_up_by_one_time_adjustments(groq_vision, monkeypatch, tmp_path):
    """A refused picture count, then two 429s, then an answer: still answered (three transient retries are not needed)."""
    frames = _jpegs(tmp_path, 5)
    replies = iter([_Response(400, {"error": {"message": "Too many images provided. This model supports up to 3 images"}}),
                    _Response(429, {"error": {"message": "slow down"}}), _Response(429, {"error": {"message": "slow down"}}),
                    _Response(200, {"choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}],
                                    "usage": {}})])
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: next(replies))
    assert ai.ask_json("Look.", files=frames) == {"ok": True}


def _jpegs(tmp_path, count, size=(480, 270), name="frame"):
    from PIL import Image
    paths = []
    for i in range(count):
        path = tmp_path / f"{name}_{i}.jpg"
        Image.new("RGB", size, (20 * i % 255, 80, 160)).save(path, format="JPEG")
        paths.append(str(path))
    return paths


def _capped_vision_post(sent, cap=3, reply='{"captions": false}'):
    """Groq's qwen vision model: more than `cap` pictures in one request is a 400 naming the cap."""
    def post(url, json=None, headers=None, timeout=None):
        parts = json["messages"][1]["content"]
        images = [p for p in parts if isinstance(p, dict) and p.get("type") == "image_url"] if isinstance(parts, list) else []
        text = next((p["text"] for p in parts if isinstance(p, dict) and p.get("type") == "text"), "") \
            if isinstance(parts, list) else parts
        sent.append({"images": len(images), "text": text})
        if len(images) > cap:
            return _Response(400, {"error": {"message": f"Too many images provided.  This model supports up to {cap} images",
                                             "type": "invalid_request_error"}})
        return _Response(200, {"model": json["model"], "usage": {"prompt_tokens": 50, "completion_tokens": 5},
                               "choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})
    return post


@pytest.fixture
def groq_vision(groq, monkeypatch):
    monkeypatch.setattr(settings, "clip_ai_api_vision_model", "qwen/qwen3.8-27b")
    monkeypatch.setattr(ai, "_image_caps", {})
    return groq


def test_a_vision_model_capped_at_three_pictures_gets_numbered_sheets_instead_of_a_lost_call(groq_vision, monkeypatch,
                                                                                               tmp_path):
    from PIL import Image
    sent = []
    monkeypatch.setattr(ai.httpx, "post", _capped_vision_post(sent))
    frames = _jpegs(tmp_path, 8)
    with metering.metered() as meter:
        assert ai.ask_json("Any captions?", files=frames) == {"captions": False}
    # One refused request, then the eight frames as three numbered sheets, in order.
    assert [s["images"] for s in sent] == [8, 3]
    assert "1 to 8" in sent[1]["text"] and "top-left corner" in sent[1]["text"]
    llm = meter.summary()["llm"]["openai_compatible:qwen/qwen3.8-27b"]
    assert llm["calls"] == 1 and llm["failed"] == 0 and llm["images"] == 3
    # The cap is remembered for this endpoint and model: the next call never pays the refusal again.
    sent.clear()
    assert ai.ask_json("Rank these.", files=frames[:6]) == {"captions": False}
    assert [s["images"] for s in sent] == [3]
    # Requests within the cap are untouched (no sheets, no note).
    sent.clear()
    assert ai.ask_json("One picture.", files=frames[:1]) == {"captions": False}
    assert sent[0]["images"] == 1 and "Upload limit" not in sent[0]["text"]
    # The sheets keep every picture and label it with its number.
    sheets = ai._combine_images(frames, 3, str(tmp_path))
    assert len(sheets) == 3
    with Image.open(sheets[0]) as sheet:
        assert sheet.width >= 2 * 480 and sheet.height >= 2 * 270  # three frames in a 2x2 grid at full size


def test_the_caption_check_and_visual_ranking_never_exceed_a_vision_models_picture_cap(groq_vision, monkeypatch,
                                                                                         tmp_path):
    from app.clipper import moments, subtitles
    from PIL import Image
    sent = []

    def frame(args, **kwargs):
        Image.new("RGB", (480, 270), (200, 200, 200)).save(args[-1], format="JPEG")

    monkeypatch.setattr(subtitles, "run_ffmpeg", frame)
    monkeypatch.setattr(ai.httpx, "post", _capped_vision_post(sent, reply='{"captions": true}'))
    assert subtitles.already_captioned("talk.mp4", INFO) is True

    def strip(path, moment, out_path):
        Image.new("RGB", (1280, 180), (90, 90, 90)).save(out_path, format="JPEG")

    ranked = {"clips": [{"candidate": 2, "title": "Big reveal", "hook": "Watch this", "score": 90}]}
    monkeypatch.setattr(moments, "_frame_strip", strip)
    monkeypatch.setattr(ai.httpx, "post", _capped_vision_post(sent, reply=json.dumps(ranked)))
    candidates = [Moment(float(10 * i), float(10 * i + 20), kind="visual", score=50.0) for i in range(6)]
    assert moments._ai_rank_visual("talk.mp4", candidates, "Talk") == ranked
    answered = [s for s in sent if s["images"] <= 3]
    assert len(answered) == 2 and sum(1 for s in sent if s["images"] > 3) == 1  # only the very first request is refused
    assert "1 to 6" in answered[1]["text"]


# ---------- cheaper speech-to-text ----------

def _deepgram_reply(language="en", words=("Hello.",) * 60):
    return {"results": {"channels": [{"detected_language": language, "alternatives": [{"words": [
        {"word": w.strip("."), "punctuated_word": w, "start": i, "end": i + 0.4, "confidence": 0.99}
        for i, w in enumerate(words)]}]}]}}


def _stt(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deepgram_api_key", "k")
    monkeypatch.setattr(settings, "sarvam_api_key", "s")
    monkeypatch.setattr(tr, "run_ffmpeg", lambda args, **k: Path(args[-1]).write_bytes(b"mp3"))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    return video


def test_hindi_audio_skips_the_wasted_full_deepgram_pass(monkeypatch, tmp_path):
    from app.clipper import sarvam
    video = _stt(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(tr, "_request", lambda path: sent.append(Path(path).name) or _deepgram_reply("hi", ("नमस्ते.",) * 60))
    hindi = Transcript(words=[Word("नमस्ते", 0.0, 0.5)], language="hi", duration=600.0, engine="sarvam")
    monkeypatch.setattr(sarvam, "transcribe", lambda *a, **k: hindi)
    with metering.metered() as meter:
        got = tr.transcribe(str(video), INFO)
    assert got is hindi and sent == ["sample_0.mp3", "sample_1.mp3"]
    assert meter.summary()["stt_seconds"][f"deepgram:{settings.deepgram_model}"] == 40.0


def test_a_sample_that_is_not_clearly_hindi_speech_keeps_the_full_deepgram_pass(monkeypatch, tmp_path):
    """Music or one code-switched window must never send an English video to Sarvam."""
    from app.clipper import sarvam
    video = _stt(monkeypatch, tmp_path)
    replies = iter([_deepgram_reply("hi"), _deepgram_reply("hi", ("la",) * 3), _deepgram_reply("en", ("Hello.",) * 900)])
    sent = []
    monkeypatch.setattr(tr, "_request", lambda path: sent.append(Path(path).name) or next(replies))
    monkeypatch.setattr(sarvam, "transcribe", lambda *a, **k: pytest.fail("not two clear Hindi samples"))
    got = tr.transcribe(str(video), INFO)
    assert got.is_english_speech and sent == ["sample_0.mp3", "sample_1.mp3", "audio.mp3"]


def test_a_known_language_skips_the_sample_too(monkeypatch, tmp_path):
    from app.clipper import sarvam
    video = _stt(monkeypatch, tmp_path)
    monkeypatch.setattr(tr, "_request", lambda path: pytest.fail("Hindi was declared, so Deepgram is not asked"))
    hindi = Transcript(words=[Word("नमस्ते", 0.0, 0.5)], language="hi", duration=600.0, engine="sarvam")
    monkeypatch.setattr(sarvam, "transcribe", lambda *a, **k: hindi)
    assert tr.transcribe(str(video), INFO, language_hint="hi") is hindi


def test_english_audio_costs_a_sample_plus_one_full_pass_and_no_sarvam(monkeypatch, tmp_path):
    from app.clipper import sarvam
    video = _stt(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(tr, "_request", lambda path: sent.append(Path(path).name) or _deepgram_reply("en", ("Hello.",) * 900))
    monkeypatch.setattr(sarvam, "transcribe", lambda *a, **k: pytest.fail("English never goes to Sarvam"))
    got = tr.transcribe(str(video), INFO)
    assert got.is_english_speech and sent == ["sample_0.mp3", "audio.mp3"]


def test_a_refused_deepgram_sample_is_a_warning_that_names_the_reason(monkeypatch, tmp_path, caplog):
    """A key Deepgram refuses (here: two keys joined by a comma) used to log only "TranscribeError" at INFO."""
    from app.clipper import sarvam, whisper_stt as ws
    video = _stt(monkeypatch, tmp_path)
    monkeypatch.setattr(ws, "available", lambda: False)

    def refused(path):
        raise tr.TranscribeError('Deepgram returned 401: {"err_code":"INVALID_AUTH","err_msg":"Invalid credentials."}')

    monkeypatch.setattr(tr, "_request", refused)
    monkeypatch.setattr(sarvam, "transcribe", lambda *a, **k: pytest.fail("no Hindi was heard"))
    with caplog.at_level(logging.INFO, logger="app.clipper.transcribe"), pytest.raises(tr.TranscribeError):
        tr.transcribe(str(video), INFO)
    sample = [r for r in caplog.records if "from the sample" in r.getMessage()]
    assert sample and sample[0].levelno == logging.WARNING and "401" in sample[0].getMessage()


def test_a_failed_sarvam_still_falls_back_to_deepgram_once(monkeypatch, tmp_path):
    from app.clipper import sarvam
    video = _stt(monkeypatch, tmp_path)
    sent, asked = [], []
    monkeypatch.setattr(tr, "_request", lambda path: sent.append(Path(path).name) or _deepgram_reply("hi"))
    monkeypatch.setattr(sarvam, "transcribe", lambda *a, **k: asked.append(1) or None)
    tr.transcribe(str(video), INFO)
    assert sent == ["sample_0.mp3", "sample_1.mp3", "audio.mp3"] and len(asked) == 1


def _speech_wav(path, spans, seconds):
    """A 16kHz mono WAV with a voice-like tone inside ``spans`` and near-silence elsewhere."""
    import wave

    import numpy as np
    t = np.arange(int(16000 * seconds)) / 16000
    level = np.full_like(t, 0.0005)
    for a, b in spans:
        level[(t >= a) & (t < b)] = 0.3
    pcm = (np.sin(2 * np.pi * 180 * t) * level * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm.tobytes())
    return str(path)


def test_sarvam_windows_end_in_a_pause_and_words_land_on_speech(monkeypatch, tmp_path):
    from app.clipper import sarvam
    wav = _speech_wav(tmp_path / "a.wav", [(0.5, 18.0), (19.2, 40.0)], 40.0)
    active = sarvam.speech_frames(wav)
    assert active[100] and not active[1860] and active[2500]
    # The window ends in the 18.0-19.2s pause instead of cutting a word at 25s.
    assert sarvam._window_end(active, 0.0, 40.0) == pytest.approx(18.6, abs=0.05)
    assert sarvam._window_end(None, 0.0, 40.0) == 25.0
    # Sarvam's one chunk per window is spread over the speech, never into the pause.
    words = sarvam._split_chunk(" ".join(f"w{i}" for i in range(20)), 10.0, 25.0, active)
    assert all(not (18.0 <= w.start < 19.2) for w in words)
    assert all(w.end <= 18.05 or w.start >= 19.2 for w in words)  # the word before the pause ends with its sound
    assert words[0].start == pytest.approx(10.0, abs=0.02) and words[-1].end <= 25.0
    assert sarvam._split_chunk("a b", 10.0, 12.0) == sarvam._split_chunk("a b", 10.0, 12.0, None)  # old behaviour kept

    monkeypatch.setattr(settings, "sarvam_api_key", "sv")
    monkeypatch.setattr(sarvam, "_segment", lambda *a, **k: None)
    windows = []
    monkeypatch.setattr(sarvam, "_call", lambda path, code: windows.append(path) or
                        {"transcript": "एक दो", "timestamps": {"words": ["एक दो"], "start_time_seconds": [0.0],
                                                               "end_time_seconds": [5.0]}})
    starts = []
    real_words_from = sarvam._words_from
    monkeypatch.setattr(sarvam, "_words_from", lambda p, offset, seg_len, active=None:
                        starts.append((offset, seg_len)) or real_words_from(p, offset, seg_len, active))
    got = sarvam.transcribe(wav, 40.0)
    assert got is not None and starts[0] == (0.0, pytest.approx(18.6, abs=0.05))
    assert sum(length for _, length in starts) == pytest.approx(40.0)


def test_whisper_words_take_punctuation_from_their_segments():
    data = {"language": "english",
            "segments": [{"text": " Hello there. How are you?"}],
            "words": [{"word": "Hello", "start": 0.0, "end": 0.3}, {"word": "there", "start": 0.3, "end": 0.6},
                      {"word": "How", "start": 1.0, "end": 1.2}, {"word": "are", "start": 1.2, "end": 1.3},
                      {"word": "you", "start": 1.3, "end": 1.5}]}
    words = whisper_stt.punctuated_words(data, 600.0)
    assert [w.text for w in words] == ["Hello", "there.", "How", "are", "you?"]
    assert words[0].start == 600.0 and whisper_stt.language_code("english") == "en"


def test_whisper_is_used_when_configured_and_deepgram_is_not(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    monkeypatch.setattr(settings, "clip_stt_provider", "openai_compatible")
    monkeypatch.setattr(settings, "clip_stt_base_url", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "clip_stt_api_key", "gq")
    monkeypatch.setattr(tr, "run_ffmpeg", lambda args, **k: Path(args[-1]).write_bytes(b"mp3"))
    monkeypatch.setattr(whisper_stt, "run_ffmpeg", lambda args, **k: Path(args[-1]).write_bytes(b"mp3"))
    calls = []

    def post(url, headers=None, data=None, files=None, timeout=None):
        calls.append((url, headers, data))
        return _Response(200, {"language": "english", "segments": [{"text": "Hi."}],
                               "words": [{"word": "Hi", "start": 0.1, "end": 0.3}]})

    monkeypatch.setattr(whisper_stt.httpx, "post", post)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    with metering.metered() as meter:
        got = tr.transcribe(str(video), MediaInfo(900.0, 1280, 720, 30.0, True))
    assert got.engine == "whisper" and got.language == "en" and len(calls) == 2  # two ten-minute windows
    assert calls[0][0] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert calls[0][1] == {"authorization": "Bearer gq"} and calls[0][2]["response_format"] == "verbose_json"
    assert calls[1][2]["timestamp_granularities[]"] == ["word", "segment"]
    assert meter.summary()["stt_seconds"] == {"openai_compatible:whisper-large-v3-turbo": 900.0}
    assert got.words[-1].start == pytest.approx(600.1)


def _mask(seconds, *spans):
    import numpy as np
    active = np.zeros(int(seconds * 100), dtype=bool)
    for a, b in spans:
        active[int(a * 100):int(b * 100)] = True
    return active


def test_whisper_words_are_trimmed_to_the_speech_not_stretched_over_a_pause():
    """Measured on a real talk: Whisper timed "Oh" 5.5s before it was said, across a pause spent eating."""
    words = [Word("Australia.", 3.0, 4.0), Word("Oh", 4.0, 9.0), Word("they're", 9.0, 9.4), Word("hmm", 9.4, 9.9)]
    got = whisper_stt.fit_to_speech(words, _mask(12, (3.0, 3.8), (8.9, 9.4)))
    assert [w.text for w in got] == ["Australia.", "Oh", "they're", "hmm"]
    assert got[0].start == 3.0 and got[0].end == pytest.approx(3.88)  # the pause after it is no longer its own
    assert got[1].start == pytest.approx(8.82) and got[1].end == 9.0  # starts just before the sound, not 4.9s early
    assert (got[2].start, got[2].end) == (9.0, 9.4)  # already on speech: untouched
    assert (got[3].start, got[3].end) == (9.4, 9.9)  # no speech frames in its span: kept as Whisper had it
    # Short dips (under 0.15s) are syllables, not pauses; no mask means no change.
    assert whisper_stt.fit_to_speech([Word("go", 1.0, 1.5)], _mask(3, (1.1, 1.5)))[0].start == 1.0
    assert whisper_stt.fit_to_speech(words, None) == words
    # A later ten-minute window's mask starts at its own offset.
    later = whisper_stt.fit_to_speech([Word("Oh", 604.0, 609.0)], _mask(12, (8.9, 9.4)), offset=600.0)
    assert later[0].start == pytest.approx(608.82) and later[0].end == 609.0


def test_whisper_transcripts_are_fitted_to_the_audio_they_came_from(monkeypatch, tmp_path):
    from app.clipper import sarvam
    monkeypatch.setattr(settings, "clip_stt_provider", "openai_compatible")
    monkeypatch.setattr(settings, "clip_stt_base_url", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "clip_stt_api_key", "gq")
    monkeypatch.setattr(whisper_stt, "run_ffmpeg", lambda args, **k: Path(args[-1]).write_bytes(b"mp3"))
    monkeypatch.setattr(whisper_stt.httpx, "post", lambda *a, **k: _Response(200, {
        "language": "english", "segments": [{"text": "Well. Oh nice."}],
        "words": [{"word": "Well", "start": 0.2, "end": 0.6}, {"word": "Oh", "start": 0.6, "end": 5.0},
                  {"word": "nice", "start": 5.0, "end": 5.4}]}))
    seen = []
    monkeypatch.setattr(sarvam, "speech_frames", lambda path: seen.append(path) or _mask(6, (0.2, 0.6), (4.7, 5.4)))
    got = whisper_stt.transcribe(str(tmp_path / "audio.mp3"), 6.0)
    assert len(seen) == 1 and Path(seen[0]).name == "part_0.mp3"  # each upload window's own audio, never the whole file
    assert [(w.text, w.start) for w in got.words] == [("Well.", 0.2), ("Oh", pytest.approx(4.62)), ("nice.", 5.0)]
    # A mask that cannot be read leaves Whisper's timings as they were, never the transcript lost.
    monkeypatch.setattr(sarvam, "speech_frames", lambda path: (_ for _ in ()).throw(ValueError("bad audio")))
    assert whisper_stt.transcribe(str(tmp_path / "audio.mp3"), 6.0).words[1].start == 0.6


# ---------- the standalone seam ----------

class _Plan:
    def __init__(self, duration):
        self.duration = duration
        self.tail = 0.0
        self.music = ""


def _seam(monkeypatch, tmp_path, *, moments=None, fail=None):
    """Every stage stubbed: no ffmpeg, no network, no AI."""
    from app.clipper import produce
    from app.clipper.ingest import SourceVideo
    src = tmp_path / "upload.mp4"
    src.write_bytes(b"v")
    calls = {"produce": [], "stages": []}
    monkeypatch.setattr(standalone, "require_tools", lambda: None)
    monkeypatch.setattr(standalone, "fetch", lambda s: SourceVideo(path=str(src), title="Big Talk", author_name="Chan",
                                                                    source="local", source_id="abc"))
    monkeypatch.setattr(standalone, "probe", lambda p: INFO)
    monkeypatch.setattr(standalone, "transcribe", lambda p, i, **k: calls.setdefault("transcribe", k) and None)
    monkeypatch.setattr(standalone.subtitles, "already_captioned", lambda p, i: False)
    monkeypatch.setattr(standalone, "analyse", lambda p, i: object())
    monkeypatch.setattr(standalone, "pick_speech", lambda *a, **k: [])
    picked = moments if moments is not None else [
        Moment(10.0, 40.0, title="First", description="About one.", tags=("Money Talk", "tips"), score=88.0, kind="visual"),
        Moment(100.0, 130.0, title="Second", kind="visual")]
    monkeypatch.setattr(standalone, "pick_visual", lambda *a, **k: list(picked))

    def produce_clip(src_, info, signals, moment, words, out_path, **kw):
        if fail:
            raise fail
        calls["produce"].append({"out": out_path, **kw})
        Path(out_path).write_bytes(b"clip")
        return produce.Produced(plan=_Plan(28.5), edit=None, credit="Stock video from Pixabay: Ana.",
                                memes_used=["giphy-1"], meme_keys=[["giphy-1", "sha1:abc"]])

    monkeypatch.setattr(standalone, "produce_clip", produce_clip)
    monkeypatch.setattr(standalone, "make_thumbnail", lambda path, d: Path(path).with_suffix(".jpg"))

    def write_bundle(clip_path, **kw):
        calls.setdefault("bundle", []).append(kw)
        manifest = Path(clip_path).with_name(Path(clip_path).stem + "_manifest.json")
        manifest.write_text("{}", encoding="utf-8")
        return manifest

    monkeypatch.setattr(standalone, "write_bundle", write_bundle)
    monkeypatch.setattr(standalone, "resolve_sfx_pack",
                        lambda explicit, cache: Path(explicit) if explicit else tmp_path / "cc0")
    return calls


def test_clip_to_directory_returns_the_contract_shape_and_writes_only_under_out_dir(monkeypatch, tmp_path):
    calls = _seam(monkeypatch, tmp_path)
    before = (settings.content_dir, settings.clip_layout, settings.clip_memes, settings.brand_name)
    stages = []
    out = tmp_path / "job1"
    result = standalone.clip_to_directory("https://youtu.be/x", out, count=2, layout="fill", memes=True,
                                          recent_memes=["giphy-9"], on_stage=lambda s, p: stages.append((s, p)),
                                          watermark_text="made with clipper", brand_name=None)
    assert set(result) >= {"source", "clips", "meter"}
    assert result["source"] == {"title": "Big Talk", "duration_s": 600.0, "author": "Chan"}
    clip = result["clips"][0]
    assert set(clip) >= {"path", "thumbnail_path", "duration_s", "start_s", "end_s", "title", "description",
                         "hashtags", "score", "language", "memes_used"}
    assert Path(clip["path"]).parent == out.resolve() and clip["title"] == "First"
    assert clip["hashtags"] == ["#moneytalk", "#tips", "#shorts"] and clip["memes_used"] == ["giphy-1", "sha1:abc"]
    assert "About one." in clip["description"] and "Pixabay" in clip["description"]
    assert [s for s, _ in stages][0] == "fetching" and stages[-1] == ("done", 1.0)
    assert [p for _, p in stages] == sorted(p for _, p in stages)
    kw = calls["produce"][0]
    assert kw["brand"] == "" and kw["watermark"] == "made with clipper" and kw["max_sec"] == 60
    assert kw["mstock"].recent == {"giphy-9"}
    assert (settings.content_dir, settings.clip_layout, settings.clip_memes, settings.brand_name) == before
    assert calls["transcribe"] == {"sidecar": False, "language_hint": ""}


def test_stock_and_meme_caches_can_be_shared_across_jobs_while_the_rest_stays_private(monkeypatch, tmp_path):
    from app.clipper import broll
    seen = {}
    _seam(monkeypatch, tmp_path)
    original = standalone.produce_clip

    def spy(*a, **k):
        seen.update(stock=broll._search_cache("Pixabay", "rain"), meme=memes._search_cache("Giphy", "lol"),
                    content=settings.content_path)
        return original(*a, **k)

    monkeypatch.setattr(standalone, "produce_clip", spy)
    shared = tmp_path / "shared"
    standalone.clip_to_directory("x", tmp_path / "job", count=1, shared_cache_dir=shared)
    assert seen["stock"].parent == shared.resolve() / "broll" and seen["meme"].parent == shared.resolve() / "memes"
    assert seen["content"] == (tmp_path / "job").resolve() / ".cache"
    assert settings.clip_shared_cache_dir == ""
    standalone.clip_to_directory("x", tmp_path / "job2", count=1)
    assert seen["stock"].parent == (tmp_path / "job2").resolve() / ".cache" / "broll"


def test_options_reach_settings_only_during_the_run(monkeypatch, tmp_path):
    seen = {}
    calls = _seam(monkeypatch, tmp_path)
    original = standalone.produce_clip

    def spy(*a, **k):
        seen.update(layout=settings.clip_layout, broll=settings.clip_broll, sfx=settings.clip_sfx,
                    music=settings.clip_music, content=settings.content_dir, max_sec=settings.clip_max_sec)
        return original(*a, **k)

    monkeypatch.setattr(standalone, "produce_clip", spy)
    out = tmp_path / "job2"
    standalone.clip_to_directory("x", out, count=1, layout="nonsense", broll=False, sfx=False, music=True,
                                 max_clip_seconds=500, language="hi")
    assert seen == {"layout": "framed", "broll": False, "sfx": False, "music": True,
                    "content": str(out.resolve() / ".cache"), "max_sec": 90}
    assert calls["transcribe"]["language_hint"] == "hi"


def test_logo_and_custom_memes_reach_the_render(monkeypatch, tmp_path):
    from PIL import Image
    calls = _seam(monkeypatch, tmp_path)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (40, 20), (0, 0, 0, 255)).save(logo)
    mine = tmp_path / "my_reaction.gif"
    mine.write_bytes(b"GIF89a")
    standalone.clip_to_directory("x", tmp_path / "job3", count=1, logo_path=str(logo),
                                 custom_memes=[str(mine), str(tmp_path / "missing.mp4"), str(tmp_path / "notes.txt")])
    kw = calls["produce"][0]
    assert kw["logo"] == str(logo)
    custom = kw["mstock"].custom
    assert [c.link for c in custom] == [str(mine)] and custom[0].provider == memes.CUSTOM


def test_a_logo_that_is_not_an_image_is_dropped_once_with_a_warning(monkeypatch, tmp_path):
    calls = _seam(monkeypatch, tmp_path)
    bad = tmp_path / "logo.png"
    bad.write_bytes(b"<html>not a png</html>")
    result = standalone.clip_to_directory("x", tmp_path / "j", count=2, logo_path=str(bad))
    assert [c["logo"] for c in calls["produce"]] == ["", ""] and len(result["clips"]) == 2
    assert any("logo" in w for w in result["warnings"])


def test_a_logo_that_fails_in_the_render_costs_only_the_logo(monkeypatch, tmp_path):
    from app.clipper import produce
    from app.clipper.ingest import SourceVideo
    from app.clipper.media import MediaError
    seen = []

    def render_moment(*a, **kw):
        seen.append(kw.get("logo"))
        if kw.get("logo"):
            raise MediaError("logo will not decode")
        return _Plan(20.0)

    steps = produce.Steps(render_moment=render_moment)
    warnings = []
    made = produce.produce_clip(SourceVideo(path="s.mp4", title="t"), INFO, object(), Moment(1.0, 20.0, kind="visual"), [],
                                str(tmp_path / "c.mp4"), timed=True, language="", burned_in=False, stings={},
                                stock=None, mstock=memes.MemeSession(), warn=warnings.append, logo="logo.png", steps=steps)
    assert seen == ["logo.png", None] and made.plan.duration == 20.0 and "logo" in warnings[0]


def test_no_moments_and_a_failed_fetch_raise_clip_errors(monkeypatch, tmp_path):
    from app.clipper.ingest import IngestError
    _seam(monkeypatch, tmp_path, moments=[])
    with pytest.raises(standalone.ClipError) as no_moments:
        standalone.clip_to_directory("x", tmp_path / "j")
    assert no_moments.value.code == "no_moments"

    def broken(s):
        raise IngestError("source is not available: private video")

    monkeypatch.setattr(standalone, "fetch", broken)
    with pytest.raises(standalone.ClipError) as failed:
        standalone.clip_to_directory("x", tmp_path / "j")
    assert failed.value.code == "fetch_failed" and "private" in failed.value.message


def test_every_clip_failing_is_a_render_failed_error(monkeypatch, tmp_path):
    from app.clipper.media import MediaError
    _seam(monkeypatch, tmp_path, fail=MediaError("filter graph broke"))
    with pytest.raises(standalone.ClipError) as err:
        standalone.clip_to_directory("x", tmp_path / "j")
    assert err.value.code == "render_failed" and "filter graph broke" in err.value.message


def test_a_cancel_raised_from_on_stage_stops_the_run(monkeypatch, tmp_path):
    _seam(monkeypatch, tmp_path)

    class Cancel(Exception):
        pass

    def stop(stage, progress):
        if stage == "rendering":
            raise Cancel()

    with pytest.raises(Cancel):
        standalone.clip_to_directory("x", tmp_path / "j", on_stage=stop)


def test_the_meter_of_the_caller_is_used_and_returned(monkeypatch, tmp_path):
    _seam(monkeypatch, tmp_path)
    with metering.metered() as meter:
        metering.record("api", provider="giphy", requests=1)
        result = standalone.clip_to_directory("x", tmp_path / "j", count=1)
    assert result["meter"]["api_requests"] == {"giphy": 1.0}
    result = standalone.clip_to_directory("x", tmp_path / "k", count=1)
    assert result["meter"]["entries"] == 0


def test_the_seam_never_imports_the_database_or_the_growth_loop():
    code = ("import os, sys; os.environ['DOOMSCROLLER_ENV_FILE']='';"
            "import app.clipper.standalone;"
            "bad=[m for m in sys.modules if m.startswith(('app.db','app.models','app.growth','app.clipper.job','sqlalchemy'))];"
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL)
    assert out.returncode == 0 and out.stdout.strip() == "[]", out.stderr
