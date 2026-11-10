"""Word-level transcripts from Deepgram, cached next to the video.

The cache is what makes re-rendering free: changing the caption style or the
moment picker should never pay to transcribe the same file twice. It is tied to
the file's contents, so a new video saved under an old name is not cut and
captioned from the old words, and fetching a link again is not a new video.

The first release only captions English. Anything else, like music or ambience
that Deepgram hears a few stray words in, goes down the no-speech path instead.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings

from .media import MediaInfo, content_id, run_ffmpeg

log = logging.getLogger(__name__)

_LISTEN_URL = "https://api.deepgram.com/v1/listen"
# Below this a video is not really speech: lyrics, a stray announcement, noise.
_MIN_WORDS_PER_MINUTE = 40
_MIN_MEAN_CONFIDENCE = 0.55


class TranscribeError(RuntimeError):
    """Deepgram rejected the request or could not be reached."""


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    confidence: float = 1.0


@dataclass
class Transcript:
    words: list[Word] = field(default_factory=list)
    language: str = ""
    duration: float = 0.0
    # Which service produced this. "sarvam" is the Hindi captioner (app/clipper/sarvam.py); its words
    # come from chunk-level timestamps rather than Deepgram's per-word ones, so it has its own speech gate.
    engine: str = "deepgram"

    @property
    def is_english_speech(self) -> bool:
        if self.language and not self.language.lower().startswith("en"):
            return False
        return self._is_speech

    @property
    def is_hindi_speech(self) -> bool:
        """Sarvam only runs when we already want Hindi captions, so words from it mean real speech. Its
        word count and (absent) per-word confidence are not Deepgram's, so the Deepgram heuristic is skipped."""
        return self.engine == "sarvam" and bool(self.words)

    @property
    def spoken_language(self) -> str:
        """Empty unless this really is speech: a language detected from lyrics or noise says nothing about the video."""
        if self.is_hindi_speech:
            return "hi"
        return self.language.lower() if self._is_speech else ""

    @property
    def _is_speech(self) -> bool:
        if not self.words or self.duration <= 0:
            return False
        per_minute = len(self.words) / (self.duration / 60)
        confidence = sum(w.confidence for w in self.words) / len(self.words)
        return per_minute >= _MIN_WORDS_PER_MINUTE and confidence >= _MIN_MEAN_CONFIDENCE


def parse_response(payload: dict, duration: float) -> Transcript:
    channel = ((payload.get("results") or {}).get("channels") or [{}])[0]
    alternative = (channel.get("alternatives") or [{}])[0]
    words = [
        Word(
            text=str(w.get("punctuated_word") or w.get("word") or "").strip(),
            start=float(w.get("start") or 0),
            end=float(w.get("end") or 0),
            confidence=float(w.get("confidence") or 0),
        )
        for w in alternative.get("words") or []
    ]
    return Transcript(
        words=[w for w in words if w.text],
        language=str(channel.get("detected_language") or ""),
        duration=duration,
    )


def _request(audio_path: str) -> dict:
    params = {
        "model": settings.deepgram_model,
        "smart_format": "true",
        "punctuate": "true",
        "detect_language": "true",
    }
    headers = {
        "Authorization": f"Token {settings.deepgram_api_key}",
        "Content-Type": "audio/mpeg",
    }
    try:
        with open(audio_path, "rb") as audio:
            response = httpx.post(
                _LISTEN_URL, params=params, headers=headers, content=audio,
                timeout=httpx.Timeout(900, connect=30),
            )
    except httpx.HTTPError as exc:
        raise TranscribeError(f"could not reach Deepgram: {exc}") from exc
    if response.status_code != 200:
        raise TranscribeError(f"Deepgram returned {response.status_code}: {response.text[:300]}")
    try:
        return response.json()
    except ValueError as exc:
        raise TranscribeError(f"Deepgram sent an unreadable reply: {response.text[:200]!r}") from exc


def _fingerprint(video_path: str) -> dict:
    return {"content": content_id(video_path)}


def _cache_paths(video_path: str, fingerprint: dict) -> tuple[str, str]:
    """Next to the video, or under content/ when the video's folder is read-only."""
    key = hashlib.sha1(f"{video_path}|{fingerprint['content']}".encode()).hexdigest()[:16]
    return f"{video_path}.transcript.json", str(settings.content_path / "transcripts" / f"{key}.json")


def _load_cache(path: str, fingerprint: dict, duration: float) -> Optional[Transcript]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("source") != fingerprint:
            return None
        return Transcript(
            words=[Word(**w) for w in data["words"]],
            language=data.get("language", ""),
            duration=duration,
            engine=data.get("engine", "deepgram"),
        )
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(paths: tuple[str, str], fingerprint: dict, transcript: Transcript) -> None:
    body = {"source": fingerprint, "language": transcript.language, "engine": transcript.engine,
            "words": [asdict(w) for w in transcript.words]}
    for path in paths:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
            return
        except OSError as exc:
            log.warning("transcribe: could not cache at %s: %s", path, exc)


def transcribe(video_path: str, info: MediaInfo) -> Optional[Transcript]:
    """Return a transcript, or None when there is no audio or no API key."""
    if not info.has_audio:
        return None
    # Taken first, so a file replaced during the request is not stamped as current.
    fingerprint = _fingerprint(video_path)
    caches = _cache_paths(video_path, fingerprint)
    for cache in caches:
        cached = _load_cache(cache, fingerprint, info.duration)
        if cached is not None:
            return cached
    if not settings.deepgram_api_key:
        log.warning("DEEPGRAM_API_KEY not set; treating this video as no-speech")
        return None
    with tempfile.TemporaryDirectory(prefix="clipper-audio-") as tmp:
        audio_path = os.path.join(tmp, "audio.mp3")
        run_ffmpeg(["-i", video_path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                    "-b:a", "48k", audio_path])
        payload = _request(audio_path)
        try:
            transcript = parse_response(payload, info.duration)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise TranscribeError(f"unexpected Deepgram reply: {exc!r}") from exc
        # Deepgram heard Hindi, which it does not caption well. Sarvam does — and the Devanagari font
        # renders it. Only Hindi: the installed font is Devanagari, so other Indic scripts stay on the
        # motion path rather than dropping to empty boxes. Any Sarvam failure keeps the Deepgram result.
        if (settings.sarvam_api_key and not transcript.is_english_speech
                and transcript.language.lower().startswith("hi")):
            from . import sarvam
            hindi = sarvam.transcribe(audio_path, info.duration, language_code="hi-IN")
            if hindi is not None:
                transcript = hindi
    _write_cache(caches, fingerprint, transcript)
    log.info("transcribe: %d words, language=%s, via=%s",
             len(transcript.words), transcript.language or "?", transcript.engine)
    return transcript
