"""The Hindi (Sarvam) transcription path: window stitching, the speech gate, and fail-safe fallback."""
from types import SimpleNamespace

import pytest

from app.clipper import sarvam, transcribe
from app.clipper.transcribe import Transcript, Word
from app.config import settings


def _payload(*chunks):
    """A Sarvam reply with chunk-level timestamps: each chunk is (text, start, end) within its window."""
    return {
        "transcript": " ".join(c[0] for c in chunks),
        "language_code": "hi-IN",
        "timestamps": {
            "words": [c[0] for c in chunks],
            "start_time_seconds": [c[1] for c in chunks],
            "end_time_seconds": [c[2] for c in chunks],
        },
    }


def test_split_chunk_spreads_words_evenly():
    words = sarvam._split_chunk("एक दो तीन चार", 10.0, 14.0)
    assert [w.text for w in words] == ["एक", "दो", "तीन", "चार"]
    assert words[0].start == 10.0 and abs(words[-1].end - 14.0) < 1e-6
    assert abs((words[1].start - words[0].start) - 1.0) < 1e-6  # 4s over 4 words


def test_words_from_offsets_onto_the_source_clock():
    words = sarvam._words_from(_payload(("नमस्ते", 1.0, 2.0)), offset=25.0, seg_len=25.0)
    assert words[0].text == "नमस्ते"
    assert words[0].start == 26.0 and words[0].end == 27.0  # 25s window offset + 1s


def test_words_from_falls_back_to_the_whole_window_without_timestamps():
    words = sarvam._words_from({"transcript": "बिना टाइम"}, offset=0.0, seg_len=10.0)
    assert [w.text for w in words] == ["बिना", "टाइम"]
    assert words[0].start == 0.0 and abs(words[-1].end - 10.0) < 1e-6


def test_transcribe_stitches_windows_with_running_offset(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_test")
    monkeypatch.setattr(sarvam, "_segment", lambda *a, **k: None)  # no ffmpeg
    calls = []
    replies = iter([_payload(("पहला", 0.0, 5.0)), _payload(("दूसरा", 0.0, 5.0))])

    def fake_call(path, language_code):
        calls.append(language_code)
        return next(replies)

    monkeypatch.setattr(sarvam, "_call", fake_call)
    t = sarvam.transcribe("/tmp/audio.wav", duration=50.0, language_code="hi-IN")
    assert t is not None and t.engine == "sarvam" and t.language == "hi"
    assert [w.text for w in t.words] == ["पहला", "दूसरा"]
    assert t.words[0].start == 0.0        # first window
    assert t.words[1].start == 25.0       # second window offset by one 25s chunk
    assert calls == ["hi-IN", "hi-IN"]    # two windows for 50s


def test_transcribe_is_fail_safe(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_test")
    monkeypatch.setattr(sarvam, "_segment", lambda *a, **k: None)

    def boom(path, language_code):
        raise RuntimeError("Sarvam returned 429: quota")

    monkeypatch.setattr(sarvam, "_call", boom)
    assert sarvam.transcribe("/tmp/a.wav", duration=10.0) is None  # never raises


def test_transcribe_needs_a_key(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "")
    assert sarvam.transcribe("/tmp/a.wav", duration=10.0) is None


def test_hindi_transcript_is_speech_but_not_english():
    t = Transcript(words=[Word("नमस्ते", 0.0, 1.0)], language="hi", duration=5.0, engine="sarvam")
    assert t.is_hindi_speech is True
    assert t.is_english_speech is False
    assert t.spoken_language == "hi"
    empty = Transcript(words=[], language="hi", duration=5.0, engine="sarvam")
    assert empty.is_hindi_speech is False


def _deepgram_payload(language, *words):
    return {"results": {"channels": [{
        "detected_language": language,
        "alternatives": [{"words": [
            {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "confidence": 0.9}
            for i, w in enumerate(words)]}],
    }]}}


def test_deepgram_hindi_is_rerouted_through_sarvam(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 64)
    monkeypatch.setattr(settings, "deepgram_api_key", "dg_test")
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_test")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(transcribe, "_request", lambda path: _deepgram_payload("hi", "kuch", "bhi"))
    hindi = Transcript(words=[Word("नमस्ते", 0.0, 1.0)], language="hi", duration=60.0, engine="sarvam")
    monkeypatch.setattr(sarvam, "transcribe", lambda *a, **k: hindi)
    info = SimpleNamespace(has_audio=True, duration=60.0)
    out = transcribe.transcribe(str(video), info)
    assert out is hindi and out.is_hindi_speech  # Deepgram's Hindi detection handed off to Sarvam


def test_devanagari_renders_when_the_font_is_installed():
    # The feature's point: with a Noto Devanagari font on the box, Hindi text draws instead of being
    # dropped. Skipped where no such font is installed (e.g. a CI image without fonts-noto-core).
    from app.transformers import fonts
    from app.transformers.ffmpeg_transformer import is_renderable

    if not fonts.has_devanagari_font():
        pytest.skip("no Devanagari font installed")
    assert is_renderable("भूमि ठाकुर की यात्रा")
    assert fonts.dominant_script("नमस्ते") == "devanagari"


def test_deepgram_english_stays_on_deepgram(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"y" * 64)
    monkeypatch.setattr(settings, "deepgram_api_key", "dg_test")
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_test")
    monkeypatch.setattr(transcribe, "run_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(transcribe, "_request",
                        lambda path: _deepgram_payload("en", *(["word"] * 60)))
    called = []
    monkeypatch.setattr(sarvam, "transcribe", lambda *a, **k: called.append(1))
    info = SimpleNamespace(has_audio=True, duration=60.0)
    out = transcribe.transcribe(str(video), info)
    assert out.engine == "deepgram" and not called  # English never touches Sarvam
