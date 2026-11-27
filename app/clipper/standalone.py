"""The clipper as a self-contained function, for callers that are not the owner's growth loop (the SaaS).

``clip_to_directory`` takes one long video (a link or a file the caller has the rights to) and writes
finished 1080x1920 clips, each with a thumbnail, into one directory. It never touches the database or
any channel or growth state, and every file it writes lands under ``out_dir`` (downloads, stock and meme
caches and transcripts go to ``out_dir/.cache`` unless ``cache_dir`` names another place). The edit,
decoration and render go through :func:`app.clipper.produce.produce_clip`, the same core the owner's
queue uses, so both get the same fixes.

The clipper reads its options from the process-wide ``settings``, so a run applies the caller's options
as temporary overrides under a lock: one run at a time per process (renders are CPU-bound anyway).

Every stage is a module-level name (``fetch``, ``transcribe``, ``analyse``, ``pick_speech``,
``produce_clip``, ``make_thumbnail``...) so tests can replace the ffmpeg and network work.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from app.config import settings
from app.logging_config import scrub_secrets

from . import ai, metering, subtitles
from . import music as music_mod
from . import sfx as sfx_mod
from .broll import StockSession
from .ingest import IngestError, SourceVideo, fetch
from .media import Interrupted, MediaError, probe, require_tools, run_ffmpeg
from .memes import MemeSession, custom_candidates
from .moments import Moment, pick_speech, pick_visual
from .produce import produce_clip
from .signals import analyse
from .transcribe import TranscribeError, transcribe

log = logging.getLogger(__name__)

STAGES = ("queued", "fetching", "transcribing", "selecting", "rendering", "done")
_LAYOUTS = ("framed", "fill")
_LANGUAGES = ("auto", "en", "hi")
_MAX_HASHTAGS = 5
_lock = threading.RLock()


class ClipError(RuntimeError):
    """A job that produced no clips. ``code`` is machine-readable; the message is safe to show a customer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@contextmanager
def _overrides(values: dict) -> Iterator[None]:
    before = {name: getattr(settings, name) for name in values}
    try:
        for name, value in values.items():
            setattr(settings, name, value)
        yield
    finally:
        for name, value in before.items():
            setattr(settings, name, value)


def _meter():
    """The caller's meter when one is active (the SaaS engine wraps the job), else one for this run."""
    current = metering.active()
    return nullcontext(current) if current is not None else metering.metered()


def hashtags(tags: Sequence[str], title: str = "") -> list[str]:
    """Up to five clean hashtags from the AI's topic tags, always ending with #shorts."""
    out: list[str] = []
    for raw in list(tags or ()):
        tag = re.sub(r"[^0-9A-Za-zऀ-ॿ]+", "", str(raw or "")).lower()
        if tag and not tag.isdigit() and f"#{tag}" not in out:
            out.append(f"#{tag}")
        if len(out) >= _MAX_HASHTAGS - 1:
            break
    out.append("#shorts")
    return out


def logo_ok(path: str) -> bool:
    """Whether a customer's logo is a real image: checked once, so a bad upload cannot fail every clip."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            return img.width > 0 and img.height > 0
    except Exception:
        return False


def make_thumbnail(clip_path: Path, duration: float) -> Optional[Path]:
    """A JPEG of one frame a third of the way in, after the hook has left the screen. None if it fails."""
    target = clip_path.with_suffix(".jpg")
    at = max(0.0, min(duration * 0.33, max(0.0, duration - 0.5))) if math.isfinite(duration) else 0.0
    try:
        with metering.timed("render", stage="thumbnail", output_seconds=0.0):
            run_ffmpeg(["-ss", f"{at:.3f}", "-i", str(clip_path), "-frames:v", "1", "-q:v", "3", str(target)])
    except MediaError as exc:
        log.warning("standalone: no thumbnail for %s: %s", clip_path.name, exc)
        return None
    return target if target.is_file() and target.stat().st_size > 0 else None


def clip_to_directory(source: str, out_dir: Path, *, count: int = 3, language: str = "auto",
                      layout: str = "framed", captions: bool = True, broll: bool = True,
                      memes: bool = True, sfx: bool = True, music: bool = False,
                      max_clip_seconds: int = 60, brand_name: Optional[str] = None,
                      watermark_text: Optional[str] = None, recent_memes: Sequence[str] = (),
                      on_stage: Optional[Callable[[str, float], None]] = None,
                      logo_path: Optional[str] = None, custom_memes: Sequence[str] = (),
                      cache_dir: Optional[Path] = None, sfx_pack_dir: Optional[Path] = None,
                      music_dir: Optional[Path] = None, shared_cache_dir: Optional[Path] = None) -> dict:
    """Cut ``count`` finished clips from ``source`` into ``out_dir``. Raises :class:`ClipError` if none can be made.

    Returns ``{"source": {...}, "clips": [...], "warnings": [...], "meter": <metering summary>}``. Each clip's
    ``memes_used`` holds opaque meme identity keys: pass the ones from a customer's recent clips back in as
    ``recent_memes`` and their next clips will not repeat a meme. ``on_stage(stage, progress)`` is called
    with the job stages (fetching, transcribing, selecting, rendering, done) and overall progress 0..1; an
    exception raised from it stops the run (that is how a caller cancels).

    ``logo_path`` is an image shown small in the safe band where the brand handle would be (it replaces
    ``brand_name``'s text). ``custom_memes`` are the customer's own GIF/MP4/WebM/PNG files, offered before
    the meme library on each reaction beat, with the same no-repeat rule and a sound of their own or a
    matching sting. Both are read from where they are; nothing is written beside them.

    ``sfx_pack_dir`` defaults to this deployment's configured pack; ``music_dir`` defaults to an empty
    folder under the cache, so music (when on) is the zero-copyright synthesised bed.

    ``shared_cache_dir`` holds only stock-footage and meme searches and downloads, so jobs share them
    (Pixabay asks for 24h caching; Giphy's quota is small). The source, transcript and everything else
    stay private under ``cache_dir``. Without it those caches live under ``cache_dir`` too.
    """
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(cache_dir).expanduser().resolve() if cache_dir else out / ".cache"
    pack = Path(sfx_pack_dir).expanduser() if sfx_pack_dir else sfx_mod.pack_dir()
    count = max(1, min(20, int(count)))
    max_clip_seconds = max(15, min(90, int(max_clip_seconds)))
    wanted_language = language if language in _LANGUAGES else "auto"

    def report(stage: str, progress: float) -> None:
        if on_stage is not None:
            on_stage(stage, round(max(0.0, min(1.0, progress)), 3))

    values = {
        "content_dir": str(cache),
        "clip_layout": layout if layout in _LAYOUTS else "framed",
        "clip_broll": bool(broll),
        "clip_memes": bool(memes),
        "clip_sfx": bool(sfx),
        "clip_music": bool(music),
        "clip_sfx_pack_dir": str(pack),
        "clip_music_dir": str(Path(music_dir).expanduser() if music_dir else cache / "music"),
        "clip_max_sec": max_clip_seconds,
        "clip_min_sec": min(settings.clip_min_sec, max_clip_seconds),
        "clip_edit_target_sec": min(settings.clip_edit_target_sec, max(12, int(max_clip_seconds * 0.8))),
        "clip_auto_queue": False,
        "brand_name": "",
        "clip_shared_cache_dir": str(Path(shared_cache_dir).expanduser().resolve()) if shared_cache_dir else "",
    }
    with _lock, _overrides(values), _meter() as meter:
        try:
            result = _run(source, out, count=count, wanted_language=wanted_language, captions=captions,
                          max_clip_seconds=max_clip_seconds, brand=(brand_name or "").strip(),
                          watermark=(watermark_text or "").strip(), recent_memes=recent_memes, report=report,
                          logo=str(logo_path or "").strip(), custom_memes=custom_memes)
        except (ClipError, Interrupted):
            raise
        except IngestError as exc:
            raise ClipError("fetch_failed", scrub_secrets(str(exc))) from None
        except MediaError as exc:
            raise ClipError("media_error", scrub_secrets(str(exc))) from None
        result["meter"] = meter.summary() if meter is not None else {}
    return result


def _run(source: str, out: Path, *, count: int, wanted_language: str, captions: bool, max_clip_seconds: int,
         brand: str, watermark: str, recent_memes: Sequence[str],
         report: Callable[[str, float], None], logo: str = "", custom_memes: Sequence[str] = ()) -> dict:
    warnings: list[str] = []

    def warn(message: str) -> None:
        cleaned = scrub_secrets(str(message))
        if cleaned not in warnings:
            warnings.append(cleaned)
            log.warning("standalone: %s", cleaned)

    require_tools()
    report("fetching", 0.02)
    src = fetch(source)
    info = probe(src.path)
    if not math.isfinite(info.duration) or info.duration <= 0:
        raise ClipError("unreadable_source", "the video has no playable length")

    report("transcribing", 0.15)
    transcript = None
    try:
        transcript = transcribe(src.path, info, sidecar=False,
                                language_hint="" if wanted_language == "auto" else wanted_language)
    except TranscribeError as exc:
        warn(f"transcription failed, so no captions: {exc}")
    except MediaError as exc:
        warn(f"transcription failed, so no captions: {exc}")
    speech = transcript is not None and (transcript.is_english_speech or transcript.is_hindi_speech)
    words = [w for w in transcript.words if w.end <= info.duration] if speech else []
    language = (("hi" if transcript.is_hindi_speech else "en") if speech
                else (transcript.spoken_language if transcript else ""))
    if wanted_language != "auto" and language and language != wanted_language:
        warn(f"asked for {wanted_language}, but the speech sounds like {language}; clips follow the speech")
    burned_in = speech and subtitles.already_captioned(src.path, info)

    report("selecting", 0.3)
    signals = analyse(src.path, info)
    timed = language != "hi"
    clean_vlog = speech and not timed
    stings = sfx_mod.trending(language=language) if settings.clip_sfx else {}
    min_score = 75.0 if ai.available() else 0.0
    if clean_vlog:
        pick_kw = {"min_sec": 15, "max_sec": min(max_clip_seconds, 30), "target_sec": min(22, max_clip_seconds)}
    else:
        pick_kw = {"min_sec": min(settings.clip_min_sec, max_clip_seconds), "max_sec": max_clip_seconds,
                   "target_sec": settings.clip_edit_target_sec}
    moments: list[Moment] = (pick_speech(words, signals, count=count, title=src.title, sounds=tuple(stings),
                                         min_score=min_score, language=language, **pick_kw) if speech else [])
    if not moments:
        moments = pick_visual(src.path, info, signals, count=count, title=src.title, min_score=min_score,
                              max_sec=min(max_clip_seconds, settings.clip_visual_sec))
    if not moments:
        raise ClipError("no_moments", "no usable moments were found in this video")

    # The customer owns the footage, so the clip carries no "Credit:" line naming their own channel.
    shown = SourceVideo(path=src.path, title=src.title, description=src.description, author="", author_name="",
                        url=src.url, source=src.source, source_id=src.source_id, keywords=list(src.keywords),
                        title_from_filename=src.title_from_filename)
    stock = StockSession()
    mstock = MemeSession(recent={str(k) for k in recent_memes if k}, custom=custom_candidates(custom_memes))
    if logo and not logo_ok(logo):
        warn("the logo file is not a readable image, so clips carry no logo")
        logo = ""
    chosen = moments[:count]
    clips: list[dict] = []
    for number, moment in enumerate(chosen, 1):
        report("rendering", 0.4 + 0.55 * (number - 1) / len(chosen))
        clip_path = out / f"clip_{number:02d}_{int(moment.start)}_{int(moment.end)}.mp4"
        track = music_mod.pick(f"{src.source_id}_{int(moment.start)}") if settings.clip_music else None
        try:
            made = produce_clip(shown, info, signals, moment, words, str(clip_path), timed=timed, language=language,
                                burned_in=burned_in, stings=stings, stock=stock, mstock=mstock, warn=warn,
                                track=track, brand=brand, watermark=watermark, logo=logo, max_sec=max_clip_seconds,
                                captions=captions)
        except Interrupted:
            raise
        except Exception as exc:  # one clip failing costs only that clip
            warn(f"clip {number} failed: {exc}")
            continue
        thumb = make_thumbnail(clip_path, made.plan.duration)
        title = moment.title or (f"{src.title} - part {number}" if src.title else f"Clip {number}")
        description = " ".join(p for p in (moment.description, made.credit) if p).strip()
        clips.append({
            "path": str(clip_path),
            "thumbnail_path": str(thumb) if thumb else None,
            "duration_s": round(made.plan.duration, 2),
            "start_s": round(moment.start, 2),
            "end_s": round(moment.end, 2),
            "title": title[:100],
            "description": description[:5000],
            "hashtags": hashtags(moment.tags or tuple(src.keywords), title),
            "score": round(float(moment.score or 0.0), 2),
            "language": language or "",
            "kind": moment.kind,
            "hook": moment.hook,
            "memes_used": [key for keys in made.meme_keys for key in keys],
        })
    if not clips:
        raise ClipError("render_failed", warnings[-1] if warnings else "every clip failed to render")
    report("done", 1.0)
    return {
        "source": {"title": src.title, "duration_s": round(info.duration, 2), "author": src.author_name or src.author},
        "clips": clips,
        "warnings": warnings,
    }
