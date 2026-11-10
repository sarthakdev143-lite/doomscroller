"""Hindi (and other Indic) speech transcripts from Sarvam, for the clipper's speech path.

Deepgram (transcribe.py) captions English with word-level timing. Sarvam is the Indic captioner: its
REST speech-to-text is meant for audio under 30 seconds and returns only chunk-level timestamps (phrase
segments, not words), so this module cuts the audio into <=25s windows, calls Sarvam per window, offsets
each window's timestamps back onto the source clock, and splits each phrase chunk into evenly-spaced Words
so the rest of the clipper — which is built on word timings — works unchanged.

Fail-safe on purpose: anything that goes wrong (no key, a bad window, a network or API error, an
unreadable reply) returns None, and the caller then falls back to the no-speech (motion) path, exactly as
before Sarvam existed. The key travels in the `api-subscription-key` header, never in a URL, so ordinary
request logging cannot leak it.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

from .media import run_ffmpeg
from .transcribe import Transcript, Word

log = logging.getLogger(__name__)

_STT_URL = "https://api.sarvam.ai/speech-to-text"
_MODEL = "saaras:v4"
_CHUNK_SEC = 25.0  # the REST endpoint is intended for audio under 30s; stay under that
_TIMEOUT = httpx.Timeout(120, connect=30)


def _segment(audio_path: str, out_path: str, start: float, length: float) -> None:
    """Cut one <=25s mono 16kHz window out of the extracted audio."""
    run_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", audio_path,
                "-ac", "1", "-ar", "16000", out_path])


def _call(path: str, language_code: str) -> dict:
    with open(path, "rb") as audio:
        response = httpx.post(
            _STT_URL,
            headers={"api-subscription-key": settings.sarvam_api_key},
            data={"model": _MODEL, "language_code": language_code, "with_timestamps": "true"},
            files={"file": (path.rsplit("/", 1)[-1], audio, "audio/wav")},
            timeout=_TIMEOUT,
        )
    if response.status_code != 200:
        # The body carries no key; status plus a short excerpt is safe and tells the real cause (quota, etc.).
        raise RuntimeError(f"Sarvam returned {response.status_code}: {response.text[:200]}")
    return response.json()


def _split_chunk(text: str, start: float, end: float) -> list[Word]:
    """Turn one phrase chunk into evenly-spaced words. Sarvam gives chunk timings only, so word times are
    approximate within the chunk — good enough for caption display and never worse than no captions."""
    tokens = [t for t in (text or "").split() if t]
    if not tokens:
        return []
    span = max(end - start, 0.0)
    step = span / len(tokens) if tokens else 0.0
    return [Word(text=tok, start=start + i * step, end=start + (i + 1) * step, confidence=1.0)
            for i, tok in enumerate(tokens)]


def _words_from(payload: dict, offset: float, seg_len: float) -> list[Word]:
    """Map one window's response onto the source clock. Prefers the chunk timestamps; if they are missing
    or malformed, spreads the whole window transcript across the window's own span."""
    stamps = payload.get("timestamps") or {}
    chunks = stamps.get("words") or []
    starts = stamps.get("start_time_seconds") or []
    ends = stamps.get("end_time_seconds") or []
    words: list[Word] = []
    if chunks and len(chunks) == len(starts) == len(ends):
        for text, s, e in zip(chunks, starts, ends):
            try:
                words += _split_chunk(str(text), offset + float(s), offset + float(e))
            except (TypeError, ValueError):
                continue
    if not words:
        words = _split_chunk(str(payload.get("transcript") or ""), offset, offset + seg_len)
    return words


def transcribe(audio_path: str, duration: float, *, language_code: str = "hi-IN") -> Optional[Transcript]:
    """A Hindi transcript for an already-extracted audio file, or None on anything that goes wrong.

    `audio_path` is the mono 16kHz file the clipper already made for Deepgram; `duration` is the source
    length. Never raises — a None here means the caller keeps the no-speech path.
    """
    if not settings.sarvam_api_key or duration <= 0:
        return None
    import os
    import tempfile

    words: list[Word] = []
    try:
        with tempfile.TemporaryDirectory(prefix="clipper-sarvam-") as tmp:
            start = 0.0
            index = 0
            while start < duration - 0.05:
                seg_len = min(_CHUNK_SEC, duration - start)
                seg = os.path.join(tmp, f"seg_{index}.wav")
                _segment(audio_path, seg, start, seg_len)
                words += _words_from(_call(seg, language_code), start, seg_len)
                start += seg_len
                index += 1
    except Exception as exc:  # fail-safe: never break the clip job over the captioner
        log.warning("sarvam: Hindi transcription failed, using the no-speech path: %s", type(exc).__name__)
        return None

    words = [w for w in words if w.text]
    if not words:
        return None
    log.info("sarvam: %d words across %.0fs of Hindi audio", len(words), duration)
    return Transcript(words=words, language="hi", duration=duration, engine="sarvam")
