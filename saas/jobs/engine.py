"""The clip engine seam. The worker calls `get_engine().run(...)` and never touches app.clipper directly.

* `ClipperEngine` calls `app.clipper.standalone.clip_to_directory` (pipeline agent; signature in the
  contract) inside `app.clipper.metering.metered()` and returns the meter summary with the result.
  It is imported lazily: the web process never imports app.* at all.
* `MockEngine` writes tiny placeholder files and walks the stages, for tests and local end-to-end runs.

`on_stage(stage, progress)` may raise JobCanceled (the worker raises it when the customer cancels),
which aborts the run between stages.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

from ..config import settings

log = logging.getLogger("saas.jobs.engine")

StageCallback = Callable[[str, float], None]


class EngineError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class JobCanceled(Exception):
    pass


class Engine(Protocol):
    name: str

    def run(self, *, source: str, out_dir: Path, options: dict, brand_name: Optional[str],
            watermark_text: Optional[str], recent_memes: Sequence[str], on_stage: StageCallback,
            logo_path: Optional[str] = None, custom_memes: Sequence[str] = ()) -> dict:
        """-> {"source": {"title", "duration_s", "author"}, "clips": [{"path", "thumbnail_path", "duration_s",
        "start_s", "end_s", "title", "description", "hashtags", "score", "language", "memes_used"}],
        "meter": {...}}. Paths must be inside out_dir. Editable clips also carry "manifest_path" and
        "source_excerpt_path" (app.clipper.editor's bundle beside the clip)."""
        ...

    def rerender(self, *, manifest_path: str, spec: dict, out_path: Path, on_stage: StageCallback,
                 logo_path: Optional[str] = None, custom_memes: Sequence[str] = ()) -> dict:
        """Render the clip again from its manifest with `spec` applied (app.clipper.standalone.rerender_clip).
        -> {"path", "thumbnail_path", "duration_s", "memes_used", "meter", ...}. Raises EngineError
        ("invalid_spec" with a customer-safe message for a spec the engine cannot apply)."""
        ...


# A tiny but structurally valid MP4 header ("ftyp" box) so magic-byte sniffers accept mock output.
_MOCK_MP4 = (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 2048)
_MOCK_JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


class MockEngine:
    name = "mock"

    def __init__(self, *, fail_with: Optional[EngineError] = None, source_duration_s: Optional[float] = None,
                 rerender_fail_with: Optional[EngineError] = None):
        self.fail_with = fail_with
        self.source_duration_s = source_duration_s
        self.rerender_fail_with = rerender_fail_with
        self.calls: list[dict] = []
        self.rerenders: list[dict] = []

    def run(self, *, source: str, out_dir: Path, options: dict, brand_name: Optional[str],
            watermark_text: Optional[str], recent_memes: Sequence[str], on_stage: StageCallback,
            logo_path: Optional[str] = None, custom_memes: Sequence[str] = ()) -> dict:
        self.calls.append({"source": source, "options": dict(options), "watermark_text": watermark_text,
                           "recent_memes": list(recent_memes), "logo_path": logo_path,
                           "custom_memes": list(custom_memes)})
        for stage, progress in (("fetching", 0.1), ("transcribing", 0.3), ("selecting", 0.5), ("rendering", 0.7)):
            on_stage(stage, progress)
        if self.fail_with is not None:
            raise self.fail_with
        out_dir.mkdir(parents=True, exist_ok=True)
        clips = []
        for i in range(int(options.get("clips", 3))):
            video = out_dir / f"clip_{i + 1:02d}.mp4"
            thumb = out_dir / f"clip_{i + 1:02d}.jpg"
            video.write_bytes(_MOCK_MP4 + i.to_bytes(4, "big"))
            thumb.write_bytes(_MOCK_JPG)
            length = min(float(options.get("max_clip_seconds", 60)), 30.0 + i)
            manifest, excerpt = _mock_bundle(video, length)
            clips.append({"path": str(video), "thumbnail_path": str(thumb), "duration_s": length,
                          "start_s": 60.0 * i, "end_s": 60.0 * i + length, "title": f"Mock clip {i + 1}",
                          "description": "A mock clip for testing.", "hashtags": ["#shorts", "#mock"],
                          "score": round(0.9 - i * 0.1, 2), "language": "en",
                          "memes_used": [f"meme-{i}"] if options.get("memes") else [],
                          "manifest_path": str(manifest), "source_excerpt_path": str(excerpt)})
            on_stage("rendering", 0.7 + 0.3 * (i + 1) / max(1, int(options.get("clips", 3))))
        return {"source": {"title": "Mock source", "duration_s": self.source_duration_s, "author": "mock"},
                "clips": clips,
                "meter": {"wall_seconds": 0.01, "stt_seconds": {}, "llm": {}, "render": {"calls": len(clips)},
                          "download": {}, "api_requests": {}, "entries": 0}}

    def rerender(self, *, manifest_path: str, spec: dict, out_path: Path, on_stage: StageCallback,
                 logo_path: Optional[str] = None, custom_memes: Sequence[str] = ()) -> dict:
        self.rerenders.append({"manifest_path": manifest_path, "spec": dict(spec), "out_path": str(out_path),
                               "logo_path": logo_path, "custom_memes": list(custom_memes)})
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        on_stage("rendering", 0.5)
        if self.rerender_fail_with is not None:
            raise self.rerender_fail_with
        trim = spec.get("trim") or {}
        duration = float(trim.get("end_s", manifest["duration_s"])) - float(trim.get("start_s", 0.0))
        out_path = Path(out_path)
        out_path.write_bytes(_MOCK_MP4 + json.dumps(spec, sort_keys=True).encode("utf-8"))
        thumb = out_path.with_suffix(".jpg")
        thumb.write_bytes(_MOCK_JPG)
        return {"path": str(out_path), "thumbnail_path": str(thumb), "duration_s": round(duration, 2),
                "memes_used": [m["id"] for m in spec.get("memes") or []],
                "meter": {"wall_seconds": 0.01, "render": {"calls": 1}}}


def _mock_bundle(video: Path, length: float) -> tuple[Path, Path]:
    """What app.clipper.editor.write_bundle leaves beside a clip, in miniature."""
    stem = video.stem
    excerpt = video.with_name(f"{stem}_source.mp4")
    excerpt.write_bytes(_MOCK_MP4)
    assets = video.with_name(f"{stem}_assets")
    assets.mkdir(exist_ok=True)
    (assets / "meme_0.gif").write_bytes(b"GIF89a")
    words = ["this", "is", "the", "moment"]
    manifest = {
        "version": 1, "clip": video.name, "excerpt": excerpt.name, "excerpt_offset_s": 0.0, "duration_s": length,
        "moment": {"hook": "Watch this", "title": "Mock"}, "words": [], "layout": "framed", "options": {},
        "logo": None, "captions": [{"i": n, "text": w, "start": n * 0.5, "end": n * 0.5 + 0.4} for n, w in enumerate(words)],
        "memes": [{"id": "giphy-mock1", "start_s": 2.0, "duration_s": 1.5}],
        "cutaways": [], "beats": [{"at": 1.0, "text": "", "style": "punch"}],
        "memes_available": [{"id": "giphy-mock1", "path": f"{stem}_assets/meme_0.gif", "title": "lol",
                             "provider": "giphy", "mood": "funny", "sound": None}],
    }
    path = video.with_name(f"{stem}_manifest.json")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, excerpt


class ClipperEngine:
    name = "clipper"

    def run(self, *, source: str, out_dir: Path, options: dict, brand_name: Optional[str],
            watermark_text: Optional[str], recent_memes: Sequence[str], on_stage: StageCallback,
            logo_path: Optional[str] = None, custom_memes: Sequence[str] = ()) -> dict:
        # The owner's growth-loop .env must never configure a customer job (it carries their cookies,
        # keys and turns real uploads on). saas_serve.py sets this before anything imports app.*; this
        # is the second lock on the same door.
        os.environ.setdefault("DOOMSCROLLER_ENV_FILE", "")
        if os.environ.get("DOOMSCROLLER_ENV_FILE"):
            raise EngineError("engine_misconfigured", "The clip engine is misconfigured.")
        try:
            from app.clipper import metering
            from app.clipper import standalone
        except ImportError as exc:
            log.error("clipper engine unavailable: %s", exc)
            raise EngineError("engine_unavailable", "The clip engine is not available right now.", retryable=True)
        # cache_dir also receives the source download and transcript, so it is per job (private, and
        # removed by the worker afterwards), never a cache shared between customers. shared_cache_dir holds only
        # public stock/meme search results and downloads: shared so Pixabay's 24h cache rule holds and Giphy
        # quota is not re-spent on every job.
        from ..config import settings as saas_settings

        shared_cache = Path(saas_settings.data_dir) / "_shared_cache"
        with metering.metered() as meter:
            try:
                result = standalone.clip_to_directory(
                    source, out_dir, count=int(options.get("clips", 3)), language=options.get("language", "auto"),
                    layout=options.get("layout", "framed"), captions=bool(options.get("captions", True)),
                    broll=bool(options.get("broll", True)), memes=bool(options.get("memes", True)),
                    sfx=bool(options.get("sfx", True)), music=bool(options.get("music", False)),
                    max_clip_seconds=int(options.get("max_clip_seconds", 60)), brand_name=brand_name,
                    watermark_text=watermark_text, recent_memes=tuple(recent_memes), on_stage=on_stage,
                    logo_path=logo_path, custom_memes=tuple(custom_memes),
                    cache_dir=Path(out_dir).parent / "work", shared_cache_dir=shared_cache)
            except JobCanceled:
                raise
            except Exception as exc:
                clip_error = getattr(standalone, "ClipError", None)
                if clip_error is not None and isinstance(exc, clip_error):
                    raise EngineError(str(getattr(exc, "code", "clip_failed")), str(getattr(exc, "message", exc))[:300])
                log.exception("clip engine crashed")
                raise EngineError("engine_crashed", "Something went wrong while making your clips.", retryable=True)
            summary = meter.summary()
        if not isinstance(result, dict):
            raise EngineError("engine_crashed", "The clip engine returned nothing.")
        result = dict(result)
        result["meter"] = result.get("meter") or summary
        return result

    def rerender(self, *, manifest_path: str, spec: dict, out_path: Path, on_stage: StageCallback,
                 logo_path: Optional[str] = None, custom_memes: Sequence[str] = ()) -> dict:
        os.environ.setdefault("DOOMSCROLLER_ENV_FILE", "")
        if os.environ.get("DOOMSCROLLER_ENV_FILE"):
            raise EngineError("engine_misconfigured", "The clip engine is misconfigured.")
        try:
            from app.clipper import metering
            from app.clipper import standalone
        except ImportError as exc:
            log.error("clipper engine unavailable: %s", exc)
            raise EngineError("engine_unavailable", "The clip editor is not available right now.", retryable=True)
        if not hasattr(standalone, "rerender_clip"):
            raise EngineError("engine_unavailable", "The clip editor is not available right now.", retryable=True)
        with metering.metered() as meter:
            try:
                result = standalone.rerender_clip(manifest_path, spec, out_path, custom_memes=tuple(custom_memes),
                                                  logo_path=logo_path, on_stage=on_stage)
            except JobCanceled:
                raise
            except Exception as exc:
                clip_error = getattr(standalone, "ClipError", None)
                if clip_error is not None and isinstance(exc, clip_error):
                    raise EngineError(str(getattr(exc, "code", "clip_failed")), str(getattr(exc, "message", exc))[:300])
                log.exception("clip editor crashed")
                raise EngineError("engine_crashed", "Something went wrong while rendering your edit.", retryable=True)
            summary = meter.summary()
        if not isinstance(result, dict):
            raise EngineError("engine_crashed", "The clip editor returned nothing.")
        result = dict(result)
        result["meter"] = result.get("meter") or summary
        return result


def get_engine() -> Engine:
    name = (settings.engine or "mock").lower()
    if name == "clipper":
        return ClipperEngine()
    if settings.is_prod:
        log.warning("SAAS_ENGINE=%s in prod: jobs will produce placeholder clips", name)
    return MockEngine()
