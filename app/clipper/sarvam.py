"""Hindi (and other Indic) speech transcripts from Sarvam, for the clipper's speech path.

Deepgram (transcribe.py) captions English with word-level timing. Sarvam is the Indic captioner: its
REST speech-to-text is meant for audio under 30 seconds and returns only chunk-level timestamps (phrase
segments, not words), so this module cuts the audio into <=25s windows, calls Sarvam per window, offsets
each window's timestamps back onto the source clock, and splits each phrase chunk into evenly-spaced Words
so the rest of the clipper — which is built on word timings — works unchanged.

On real Hindi audio Sarvam returns ONE timestamp chunk per request (the whole window), so the window
boundaries are the only real timing there is. Two things keep the words near the speech anyway: each
window ends in the longest pause between 15s and 25s (never mid-word), and a chunk's words are spread over
the frames where someone is actually speaking rather than evenly over the clock, so a pause no longer
drags every later word in the window early.

Fail-safe on purpose: anything that goes wrong (no key, a bad window, a network or API error, an
unreadable reply) returns None, and the caller then falls back to the no-speech (motion) path, exactly as
before Sarvam existed. The key travels in the `api-subscription-key` header, never in a URL, so ordinary
request logging cannot leak it.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
import time
import wave
from typing import Optional, Sequence

import numpy as np

import httpx

from app.config import settings
from app.logging_config import scrub_secrets

from . import metering
from .media import Interrupted, read_ffmpeg, run_ffmpeg
from .transcribe import Transcript, Word

log = logging.getLogger(__name__)

_STT_URL = "https://api.sarvam.ai/speech-to-text"
_MODEL = "saaras:v4"
_CHUNK_SEC = 25.0  # the REST endpoint is intended for audio under 30s; stay under that
_MIN_WINDOW_SEC = 15.0  # a window ends in a pause found after this much audio
_FRAME_SEC = 0.01  # speech activity is judged per 10ms frame
_MIN_PAUSE_FRAMES = 20  # a window boundary needs at least 0.2s of quiet
_TIMEOUT = httpx.Timeout(120, connect=30)
_MAX_RETRIES = 3
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _segment(audio_path: str, out_path: str, start: float, length: float) -> None:
    """Cut one <=25s mono 16kHz window out of the extracted audio."""
    run_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", audio_path,
                "-ac", "1", "-ar", "16000", out_path])


def _basename(path: str) -> str:
    """Final path segment on either OS. Uploads must never leak local directories,
    including when a Windows-style path reaches a POSIX host."""
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1] or "audio.wav"


def _call(path: str, language_code: str) -> dict:
    filename = _basename(path)
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            with open(path, "rb") as audio:
                response = httpx.post(
                    _STT_URL,
                    headers={"api-subscription-key": (settings.sarvam_api_key or "").strip()},
                    data={"model": _MODEL, "language_code": language_code, "with_timestamps": "true"},
                    files={"file": (filename, audio, "audio/wav")},
                    timeout=_TIMEOUT,
                )
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt + 1 < _MAX_RETRIES:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
        if response.status_code == 200:
            return response.json()
        if response.status_code in _RETRYABLE_STATUS and attempt + 1 < _MAX_RETRIES:
            time.sleep(0.5 * (attempt + 1))
            continue
        # The body carries no key; status plus a short excerpt is safe and tells the real cause (quota, etc.).
        raise RuntimeError(f"Sarvam returned {response.status_code}: {scrub_secrets(response.text[:200])}")
    assert last_exc is not None
    raise last_exc


def speech_frames(audio_path: str) -> Optional[np.ndarray]:
    """Per-10ms-frame speech activity of a 16-bit mono WAV, or None when the audio shows no clear
    contrast between talking and pauses (then words keep the even spread)."""
    try:
        with wave.open(str(audio_path), "rb") as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
                raise wave.Error("not 16-bit mono")
            rate = handle.getframerate()
            pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
    except (OSError, EOFError, wave.Error):
        # The clipper hands Sarvam the 16kHz MP3 it made for Deepgram: decode that instead.
        try:
            rate = 16000
            raw = read_ffmpeg(["-i", str(audio_path), "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"])
            pcm = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
        except Interrupted:
            raise
        except Exception:
            return None
    hop = max(1, int(rate * _FRAME_SEC))
    count = len(pcm) // hop
    if count < 50:
        return None
    rms = np.sqrt(np.mean(pcm[:count * hop].reshape(count, hop) ** 2, axis=1) + 1e-12)
    level = 20 * np.log10(rms)
    # The valley between the quiet and the loud frames (Otsu's split of the level histogram). A fixed
    # "floor + 10 dB" breaks on dense speech, where the floor percentile is itself speech.
    hist, edges = np.histogram(level, bins=120)
    centres = (edges[:-1] + edges[1:]) / 2
    below = np.cumsum(hist)[:-1].astype(np.float64)
    above = hist.sum() - below
    below_sum = np.cumsum(hist * centres)[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        quiet_mean = below_sum / below
        loud_mean = ((hist * centres).sum() - below_sum) / above
        spread = np.where((below > 0) & (above > 0), below * above * (loud_mean - quiet_mean) ** 2, -1.0)
    split = int(np.argmax(spread))
    if spread[split] <= 0 or loud_mean[split] - quiet_mean[split] < 8.0:
        return None  # no believable speech/pause contrast: keep the even spread
    active = level > edges[split + 1]
    if active.mean() < 0.15:
        return None
    # Close gaps under 120ms (between syllables) and drop blips under 80ms (clicks), as runs.
    for value, shortest in ((False, 12), (True, 8)):
        edges = np.flatnonzero(np.diff(np.concatenate(([not value], active == value, [not value])).astype(int)))
        for a, b in zip(edges[::2], edges[1::2]):
            if b - a < shortest and 0 < a and b < count:
                active[a:b] = not value
    return active


def _window_end(active: Optional[np.ndarray], start: float, duration: float) -> float:
    """Where a window starting at ``start`` ends: in the longest pause between 15s and 25s, else at 25s."""
    hard = min(duration, start + _CHUNK_SEC)
    if active is None or hard >= duration - 0.05:
        return hard
    lo, hi = int((start + _MIN_WINDOW_SEC) / _FRAME_SEC), min(len(active), int(hard / _FRAME_SEC))
    quiet = ~active[lo:hi]
    if not quiet.any():
        return hard
    edges = np.flatnonzero(np.diff(np.concatenate(([0], quiet.astype(int), [0]))))
    runs = list(zip(edges[::2], edges[1::2]))
    a, b = max(runs, key=lambda r: r[1] - r[0])
    if b - a < _MIN_PAUSE_FRAMES:
        return hard
    return round(float((lo + (a + b) / 2) * _FRAME_SEC), 3)


def _split_chunk(text: str, start: float, end: float, active: Optional[np.ndarray] = None) -> list[Word]:
    """Turn one phrase chunk into words. Sarvam gives chunk timings only, so word times are approximate:
    spread over the chunk's speaking frames when the audio's activity is known, else evenly."""
    if not math.isfinite(start) or not math.isfinite(end):
        return []
    tokens = [t for t in (text or "").split() if t]
    if not tokens:
        return []
    start = max(0.0, start)
    span = max(end - start, 0.0)
    if active is not None and span > 0:
        a, b = int(start / _FRAME_SEC), min(len(active), int(math.ceil(end / _FRAME_SEC)))
        spoken = np.flatnonzero(active[a:b]) if b > a else np.array([], dtype=int)
        if len(spoken) >= 50:  # half a second of speech: enough to place words by
            # Word i starts at the (i/n)th share of the speaking time and ends where the next begins.
            marks = [a + int(spoken[min(len(spoken) - 1, int(len(spoken) * i / len(tokens)))]) for i in range(len(tokens))]
            stops = marks[1:] + [a + int(spoken[-1]) + 1]
            words = []
            for tok, m, s in zip(tokens, marks, stops):
                # A word before a pause ends where the sound stops, not when the next word begins.
                silent = np.flatnonzero(~active[m:s]) if s > m else np.array([], dtype=int)
                last = m + int(silent[0]) if len(silent) and silent[0] > 0 else max(m + 1, s)
                words.append(Word(text=tok, start=m * _FRAME_SEC, end=last * _FRAME_SEC, confidence=1.0))
            return words
    step = span / len(tokens) if tokens else 0.0
    return [Word(text=tok, start=start + i * step, end=start + (i + 1) * step, confidence=1.0)
            for i, tok in enumerate(tokens)]


def _words_from(payload: dict, offset: float, seg_len: float, active: Optional[np.ndarray] = None) -> list[Word]:
    """Map one window's response onto the source clock. Prefers the chunk timestamps; if they are missing
    or malformed, spreads the whole window transcript across the window's own span."""
    if not isinstance(payload, dict) or not math.isfinite(offset) or not math.isfinite(seg_len) or seg_len < 0:
        return []
    win_start = max(0.0, offset)
    win_end = win_start + seg_len
    stamps = payload.get("timestamps") if isinstance(payload.get("timestamps"), dict) else {}
    chunks = stamps.get("words") or []
    starts = stamps.get("start_time_seconds") or []
    ends = stamps.get("end_time_seconds") or []
    words: list[Word] = []
    if (isinstance(chunks, list) and isinstance(starts, list) and isinstance(ends, list)
            and chunks and len(chunks) == len(starts) == len(ends)):
        for text, s, e in zip(chunks, starts, ends):
            try:
                s_val = float(s) if s is not None else 0.0
                e_val = float(e) if e is not None else s_val
            except (TypeError, ValueError):
                continue
            if not math.isfinite(s_val):
                s_val = 0.0
            clamped_s = min(win_end, max(win_start, win_start + s_val))
            clamped_e = clamped_s if not math.isfinite(e_val) else min(win_end, max(clamped_s, win_start + e_val))
            words += _split_chunk(str(text or ""), clamped_s, clamped_e, active)
    if not words:
        words = _split_chunk(str(payload.get("transcript") or ""), win_start, win_end, active)
    return words


def transcribe(audio_path: str, duration: float, *, language_code: str = "hi-IN") -> Optional[Transcript]:
    """A Hindi transcript for an already-extracted audio file, or None on anything that goes wrong.

    `audio_path` is the mono 16kHz file the clipper already made for Deepgram; `duration` is the source
    length. Never raises — a None here means the caller keeps the no-speech path.
    """
    if not (settings.sarvam_api_key or "").strip() or not math.isfinite(duration) or duration <= 0:
        return None

    words: list[Word] = []
    active = speech_frames(audio_path)
    try:
        with tempfile.TemporaryDirectory(prefix="clipper-sarvam-", ignore_cleanup_errors=True) as tmp:
            start = 0.0
            index = 0
            while start < duration - 0.05:
                seg_len = max(0.05, _window_end(active, start, duration) - start)
                seg = os.path.join(tmp, f"seg_{index}.wav")
                _segment(audio_path, seg, start, seg_len)
                words += _words_from(_call(seg, language_code), start, seg_len, active)
                metering.record("stt", provider="sarvam", model=_MODEL, audio_seconds=round(seg_len, 2))
                start += seg_len
                index += 1
    except Interrupted:
        raise
    except Exception as exc:  # fail-safe: never break the clip job over the captioner
        log.warning("sarvam: Hindi transcription failed, using the no-speech path: %s", type(exc).__name__)
        return None

    words = [w for w in words if w.text]
    if not words:
        return None
    log.info("sarvam: %d words across %.0fs of Hindi audio", len(words), duration)
    return Transcript(words=words, language="hi", duration=duration, engine="sarvam")
