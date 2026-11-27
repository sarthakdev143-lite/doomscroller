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
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from app import apikeys
from app.config import settings
from app.logging_config import scrub_secrets

from . import metering
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
        if not self.words or not math.isfinite(self.duration) or self.duration <= 0:
            return False
        per_minute = len(self.words) / (self.duration / 60)
        confidences = [w.confidence if math.isfinite(w.confidence) else 0.0 for w in self.words]
        confidence = sum(confidences) / len(confidences)
        return per_minute >= _MIN_WORDS_PER_MINUTE and confidence >= _MIN_MEAN_CONFIDENCE


def _clean_float(raw: object, default: float = 0.0, *, strict: bool = False) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool):
        if strict:
            raise TypeError("boolean is not a timestamp")
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        if strict:
            raise
        return default
    return value if math.isfinite(value) else default


def _clean_word(text: object, start_raw: object, end_raw: object, conf_raw: object,
                *, default_conf: float = 0.0, strict: bool = False) -> Optional[Word]:
    cleaned_text = str(text or "").strip()
    start = max(0.0, _clean_float(start_raw, 0.0, strict=strict))
    end = max(start, _clean_float(end_raw, start, strict=strict))
    confidence = min(1.0, max(0.0, _clean_float(conf_raw, default_conf, strict=strict)))
    if not cleaned_text:
        return None
    return Word(text=cleaned_text, start=start, end=end, confidence=confidence)


def parse_response(payload: dict, duration: float) -> Transcript:
    if not isinstance(payload, dict):
        raise TypeError("Deepgram reply must be a JSON object")
    raw_channels = (payload.get("results") or {}).get("channels") or [{}]
    channels = [c for c in raw_channels if isinstance(c, dict)]
    channel = channels[0] if channels else {}
    alts = [a for a in (channel.get("alternatives") or [{}]) if isinstance(a, dict)]
    alternative = alts[0] if alts else {}
    words: list[Word] = []
    for item in alternative.get("words") or []:
        if item is None:
            continue
        word = _clean_word(
            item.get("punctuated_word") or item.get("word"),
            item.get("start"),
            item.get("end"),
            item.get("confidence"),
            default_conf=0.0,
            strict=True,
        )
        if word is not None:
            words.append(word)
    safe_duration = duration if math.isfinite(duration) and duration > 0 else 0.0
    return Transcript(
        words=words,
        language=str(channel.get("detected_language") or ""),
        duration=safe_duration,
    )


def _request(audio_path: str) -> dict:
    params = {
        "model": settings.deepgram_model,
        "smart_format": "true",
        "punctuate": "true",
        "detect_language": "true",
    }
    raw_keys = settings.deepgram_api_key
    keys = apikeys.candidates(raw_keys) or [""]
    for index, key in enumerate(keys):
        headers = {"Authorization": f"Token {key}", "Content-Type": "audio/mpeg"}
        try:
            with open(audio_path, "rb") as audio:
                response = httpx.post(
                    _LISTEN_URL, params=params, headers=headers, content=audio,
                    timeout=httpx.Timeout(900, connect=30),
                )
        except httpx.HTTPError as exc:
            raise TranscribeError(f"could not reach Deepgram: {scrub_secrets(str(exc))}") from exc
        if response.status_code not in apikeys.REFUSED_STATUSES:
            if response.status_code == 200:
                apikeys.accept(key)
            break
        # DEEPGRAM_API_KEY may hold a backup key ("key1,key2"): a refused key passes the request to the next.
        apikeys.refuse(key)
        if index + 1 < len(keys):
            log.warning("transcribe: Deepgram refused %s (HTTP %s); trying %s", apikeys.label(key, raw_keys),
                        response.status_code, apikeys.label(keys[index + 1], raw_keys))
    if response.status_code != 200:
        raise TranscribeError(f"Deepgram returned {response.status_code}: {scrub_secrets(response.text[:300])}")
    try:
        return response.json()
    except ValueError as exc:
        raise TranscribeError(f"Deepgram sent an unreadable reply: {scrub_secrets(response.text[:200])!r}") from exc


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
        if not isinstance(data, dict) or data.get("source") != fingerprint:
            return None
        raw_words = data["words"]
        if not isinstance(raw_words, list):
            return None
        words: list[Word] = []
        for item in raw_words:
            if not isinstance(item, dict):
                return None
            built = _clean_word(
                item.get("text"),
                item.get("start"),
                item.get("end"),
                item.get("confidence", 1.0),
                default_conf=1.0,
            )
            if built is not None:
                words.append(built)
        safe_duration = duration if math.isfinite(duration) and duration > 0 else 0.0
        return Transcript(
            words=words,
            language=str(data.get("language") or ""),
            duration=safe_duration,
            engine=str(data.get("engine") or "deepgram"),
        )
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(paths: tuple[str, str], fingerprint: dict, transcript: Transcript) -> None:
    body = {"source": fingerprint, "language": transcript.language, "engine": transcript.engine,
            "words": [asdict(w) for w in transcript.words]}
    for path in paths:
        tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            log.warning("transcribe: could not cache at %s: %s", path, exc)


# Below this length a full Deepgram pass is cheap enough that sampling the language first saves nothing.
_SAMPLE_MIN_SEC = 180.0
_SAMPLE_SEC = 20.0
# Where the two samples are taken, as shares of the length: clear of intros and outros.
_SAMPLE_AT = (0.35, 0.65)


def _sample_language(audio_path: str, duration: float) -> str:
    """"hi" only when two 20-second Deepgram samples are both real speech and both Hindi, else "".

    One window can land on music, a guest or a code-switched aside, and a wrong "hi" would send an
    English video to Sarvam and lose its captions, so anything short of two clear agreeing samples
    falls through to the full pass, exactly as before.
    """
    heard: list[str] = []
    for index, share in enumerate(_SAMPLE_AT):
        sample = os.path.join(os.path.dirname(audio_path), f"sample_{index}.mp3")
        try:
            run_ffmpeg(["-ss", f"{max(0.0, duration * share - _SAMPLE_SEC / 2):.3f}", "-t", f"{_SAMPLE_SEC:.3f}",
                        "-i", audio_path, "-c", "copy", sample])
            payload = _request(sample)
            metering.record("stt", provider="deepgram", model=settings.deepgram_model, audio_seconds=_SAMPLE_SEC)
            piece = parse_response(payload, _SAMPLE_SEC)
        except TranscribeError as exc:
            # Deepgram itself refused (a revoked key, no credit, a malformed DEEPGRAM_API_KEY such as two keys
            # joined by a comma): say so, because every later Deepgram call fails the same way.
            log.warning("transcribe: no language from the sample: %s", exc)
            return ""
        except Exception as exc:  # the full pass still runs, so a failed sample only costs the saving
            log.info("transcribe: no language from the sample (%s)", type(exc).__name__)
            return ""
        language = piece.language.lower()[:2]
        if language != "hi" or not piece._is_speech:
            return ""
        heard.append(language)
    return "hi" if heard == ["hi"] * len(_SAMPLE_AT) else ""


def transcribe(video_path: str, info: MediaInfo, *, sidecar: bool = True, language_hint: str = "") -> Optional[Transcript]:
    """Return a transcript, or None when there is no audio or no API key.

    ``sidecar=False`` keeps the cache under content/transcripts only, never beside the video: a caller
    that must write nothing outside its own directory (the SaaS seam) points content/ there.

    Hindi is captioned by Sarvam, and a full Deepgram pass over Hindi audio is paid for and thrown away.
    So when ``language_hint`` says Hindi, or a 30-second Deepgram sample from the middle hears Hindi,
    Sarvam goes first and the full Deepgram pass only runs if Sarvam fails.
    """
    from . import whisper_stt

    if not info.has_audio or not math.isfinite(info.duration) or info.duration <= 0:
        return None
    # Taken first, so a file replaced during the request is not stamped as current.
    fingerprint = _fingerprint(video_path)
    caches = _cache_paths(video_path, fingerprint)
    if not sidecar:
        caches = caches[1:]
    for cache in caches:
        cached = _load_cache(cache, fingerprint, info.duration)
        if cached is not None:
            return cached
    deepgram = bool((settings.deepgram_api_key or "").strip())
    whisper = whisper_stt.available()
    has_sarvam = bool((settings.sarvam_api_key or "").strip())
    hint = str(language_hint or "").strip().lower()[:2]
    if not (deepgram or whisper or (has_sarvam and hint == "hi")):
        log.warning("DEEPGRAM_API_KEY not set; treating this video as no-speech")
        return None
    from . import sarvam

    with tempfile.TemporaryDirectory(prefix="clipper-audio-", ignore_cleanup_errors=True) as tmp:
        audio_path = os.path.join(tmp, "audio.mp3")
        with metering.timed("render", stage="extract", output_seconds=round(info.duration, 2)):
            run_ffmpeg(["-i", video_path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                        "-b:a", "48k", audio_path])
        transcript: Optional[Transcript] = None
        tried_sarvam = False
        if has_sarvam and hint != "en":
            if not hint and deepgram and info.duration > _SAMPLE_MIN_SEC:
                hint = _sample_language(audio_path, info.duration)
            if hint == "hi":
                tried_sarvam = True
                transcript = sarvam.transcribe(audio_path, info.duration, language_code="hi-IN")
        if transcript is None and whisper:
            transcript = whisper_stt.transcribe(audio_path, info.duration)
        if transcript is None:
            if not deepgram:
                return None
            payload = _request(audio_path)
            metering.record("stt", provider="deepgram", model=settings.deepgram_model,
                            audio_seconds=round(info.duration, 2))
            try:
                transcript = parse_response(payload, info.duration)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
                raise TranscribeError(f"unexpected Deepgram reply: {exc!r}") from exc
        # Deepgram (or Whisper) heard Hindi, which it does not caption well. Sarvam does — and the Devanagari
        # font renders it. Only Hindi: the installed font is Devanagari, so other Indic scripts stay on the
        # motion path rather than dropping to empty boxes. Any Sarvam failure keeps the first result.
        if (has_sarvam and not tried_sarvam and transcript.engine != "sarvam" and not transcript.is_english_speech
                and transcript.language.lower().startswith("hi")):
            hindi = sarvam.transcribe(audio_path, info.duration, language_code="hi-IN")
            if hindi is not None:
                transcript = hindi
    _write_cache(caches, fingerprint, transcript)
    log.info("transcribe: %d words, language=%s, via=%s",
             len(transcript.words), transcript.language or "?", transcript.engine)
    return transcript
