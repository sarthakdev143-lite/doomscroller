"""Speech-to-text through any OpenAI-compatible ``/audio/transcriptions`` endpoint (Groq's Whisper, say).

Optional and off by default (``CLIP_STT_PROVIDER=openai_compatible``): Deepgram stays the owner's
transcriber. Whisper's word list carries timings but no punctuation, and the clipper finds sentences by
their punctuation, so each word takes its punctuated spelling from the segment text it belongs to.
Uploads are capped by these services (25 MB on Groq's free tier), so audio goes up in windows of ten
minutes and each window's timings are moved back onto the source clock. Any failure returns None.

Whisper's word timings are gapless: a word before or after a pause stretches over it. On a real talk a speaker
who stopped to eat had "Oh" timed 5.5s before she said it, so its caption sat over the pause. So each word is
trimmed to the audio's speaking frames (the activity mask Sarvam's word placement uses), never moved outside its
own span: on that talk the mean onset error against Deepgram fell from 0.29s to 0.16s and the worst from 5.5s
to 1.3s.
"""
from __future__ import annotations

import logging
import math
import os
import re
import tempfile
from typing import Optional

import httpx

from app.config import settings
from app.logging_config import scrub_secrets

from . import metering
from .media import Interrupted, run_ffmpeg
from .transcribe import Transcript, Word

log = logging.getLogger(__name__)

_WINDOW_SEC = 600.0
_TIMEOUT = httpx.Timeout(300, connect=15)
_LANGUAGE_CODES = {"english": "en", "hindi": "hi", "urdu": "ur", "spanish": "es", "french": "fr", "german": "de",
                   "portuguese": "pt", "italian": "it", "japanese": "ja", "korean": "ko", "chinese": "zh",
                   "russian": "ru", "arabic": "ar", "bengali": "bn", "tamil": "ta", "telugu": "te", "marathi": "mr"}


def available() -> bool:
    provider = str(getattr(settings, "clip_stt_provider", "") or "").strip().lower().replace("-", "_")
    return (provider == "openai_compatible" and bool(str(settings.clip_stt_base_url or "").strip())
            and bool(str(settings.clip_stt_api_key or "").strip()))


def language_code(raw: object) -> str:
    text = str(raw or "").strip().lower()
    return _LANGUAGE_CODES.get(text, text if len(text) <= 3 else "")


def _norm(token: str) -> str:
    return re.sub(r"[^\w]", "", token).lower()


def punctuated_words(data: dict, offset: float) -> list[Word]:
    """Words with timings from ``words`` and spelling (punctuation, capitals) from ``segments``' text."""
    raw_words = data.get("words") if isinstance(data.get("words"), list) else []
    tokens: list[str] = []
    for segment in data.get("segments") if isinstance(data.get("segments"), list) else []:
        if isinstance(segment, dict):
            tokens += str(segment.get("text") or "").split()
    if not tokens:
        tokens = str(data.get("text") or "").split()
    out: list[Word] = []
    cursor = 0
    for item in raw_words:
        if not isinstance(item, dict):
            continue
        text = str(item.get("word") or "").strip()
        try:
            start, end = float(item.get("start")), float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if not text or not math.isfinite(start) or not math.isfinite(end) or end < start:
            continue
        # Find this word in the punctuated text, looking a few tokens ahead in case one was split.
        for ahead in range(cursor, min(len(tokens), cursor + 4)):
            if _norm(tokens[ahead]) == _norm(text) and _norm(text):
                text, cursor = tokens[ahead], ahead + 1
                break
        out.append(Word(text, round(start + offset, 3), round(end + offset, 3), 1.0))
    return out


_FRAME_SEC = 0.01  # sarvam.speech_frames judges activity per 10ms frame
_TRIM_MIN_SEC = 0.15  # only a pause this long inside a word's span is trimmed off it
_ONSET_LEAD_SEC = 0.08  # soft onsets (s, f, h) read as quiet: start a little before the first loud frame


def fit_to_speech(words: list[Word], active, offset: float = 0.0) -> list[Word]:
    """Trim each word's leading and trailing non-speech runs (``active``: per-10ms speech frames whose first frame
    is at ``offset`` seconds). A word only ever shrinks inside its own span, so order and text are untouched; a word
    with no speech frames is kept."""
    if active is None or not len(active):
        return list(words)
    out: list[Word] = []
    for word in words:
        a = max(0, int(round((word.start - offset) / _FRAME_SEC)))
        b = min(len(active), int(round((word.end - offset) / _FRAME_SEC)))
        start, end = word.start, word.end
        spoken = [i for i in range(a, b) if active[i]] if b > a else []
        if spoken:
            if (spoken[0] - a) * _FRAME_SEC >= _TRIM_MIN_SEC:
                start = max(word.start, offset + spoken[0] * _FRAME_SEC - _ONSET_LEAD_SEC)
            if (b - 1 - spoken[-1]) * _FRAME_SEC >= _TRIM_MIN_SEC:
                end = min(word.end, offset + (spoken[-1] + 1) * _FRAME_SEC + _ONSET_LEAD_SEC)
        end = max(end, min(word.end, start + 0.05))
        out.append(Word(word.text, round(start, 3), round(end, 3), word.confidence))
    return out


def _fitted(words: list[Word], piece: str, offset: float) -> list[Word]:
    """One window's words fitted to that window's own speech frames (a window at a time, so a 90-minute source
    never decodes into memory at once). Any failure keeps Whisper's timings: looser, never lost."""
    try:
        from .sarvam import speech_frames

        return fit_to_speech(words, speech_frames(piece), offset)
    except Interrupted:
        raise
    except Exception as exc:
        log.warning("whisper: could not fit word timings to the speech: %s", type(exc).__name__)
        return words


def _request(path: str) -> dict:
    base = str(settings.clip_stt_base_url or "").strip().rstrip("/")
    key = str(settings.clip_stt_api_key or "").strip()
    with open(path, "rb") as audio:
        response = httpx.post(
            f"{base}/audio/transcriptions",
            headers={"authorization": f"Bearer {key}"},
            data={"model": settings.clip_stt_model, "response_format": "verbose_json",
                  "timestamp_granularities[]": ["word", "segment"]},
            files={"file": (os.path.basename(path), audio, "audio/mpeg")},
            timeout=_TIMEOUT,
        )
    if response.status_code != 200:
        raise RuntimeError(f"transcription returned {response.status_code}: {scrub_secrets(response.text[:200])}")
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("transcription sent an unreadable reply")
    return body


def transcribe(audio_path: str, duration: float) -> Optional[Transcript]:
    """A transcript of an already-extracted audio file, or None on anything that goes wrong."""
    if not available() or not math.isfinite(duration) or duration <= 0:
        return None
    words: list[Word] = []
    languages: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="clipper-whisper-", ignore_cleanup_errors=True) as tmp:
            start = 0.0
            index = 0
            while start < duration - 0.05:
                length = min(_WINDOW_SEC, duration - start)
                piece = os.path.join(tmp, f"part_{index}.mp3")
                run_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", audio_path, "-c", "copy", piece])
                data = _request(piece)
                metering.record("stt", provider="openai_compatible", model=settings.clip_stt_model,
                                audio_seconds=round(length, 2))
                words += _fitted(punctuated_words(data, start), piece, start)
                if data.get("language"):
                    languages.append(language_code(data["language"]))
                start += length
                index += 1
    except Interrupted:
        raise
    except Exception as exc:  # optional transcriber: a failure means the caller's fallback
        log.warning("whisper: transcription failed: %s", scrub_secrets(str(exc))[:200])
        return None
    language = max(set(languages), key=languages.count) if languages else ""
    return Transcript(words=words, language=language, duration=duration, engine="whisper")
