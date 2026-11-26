"""Speech-to-text through any OpenAI-compatible ``/audio/transcriptions`` endpoint (Groq's Whisper, say).

Optional and off by default (``CLIP_STT_PROVIDER=openai_compatible``): Deepgram stays the owner's
transcriber. Whisper's word list carries timings but no punctuation, and the clipper finds sentences by
their punctuation, so each word takes its punctuated spelling from the segment text it belongs to.
Uploads are capped by these services (25 MB on Groq's free tier), so audio goes up in windows of ten
minutes and each window's timings are moved back onto the source clock. Any failure returns None.
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
                words += punctuated_words(data, start)
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
