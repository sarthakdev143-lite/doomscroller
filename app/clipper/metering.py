"""Per-job cost metering: what one clip job actually consumed, so price is set from measurements.

The clipper records usage at the point it is spent (an STT call, an LLM call, an ffmpeg render, a
download); whoever runs a job wraps it in `metered()` and reads `meter.summary()` afterwards. With no
meter active every `record()` is a no-op, so the single-owner growth loop pays nothing for this.

Kinds and their fields (keep them stable; the SaaS admin panel and the cost memo read them):
  stt       provider, model, audio_seconds
  llm       provider, model, input_tokens, output_tokens, images, ok   (tokens 0 when the CLI hides them)
  render    wall_seconds, output_seconds, stage ("clip" | "probe" | "extract" | ...)
  download  bytes, seconds, source ("yt-dlp" | "broll" | "meme" | ...)
  api       provider, requests                                          (Pexels, Pixabay, Tenor...)

Lookup order is the context variable, then a process-wide fallback: ThreadPoolExecutor.submit does
not copy context, and a worker runs one job at a time (recipes.applied serialises renders anyway).
"""
from __future__ import annotations

import contextvars
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator, Optional


class Meter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.entries: list[dict[str, Any]] = []
        self.started = time.monotonic()

    def record(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self.entries.append({"kind": kind, **fields})

    def summary(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self.entries)
        stt: dict[str, float] = defaultdict(float)
        llm: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        render: dict[str, float] = defaultdict(float)
        download: dict[str, float] = defaultdict(float)
        api: dict[str, float] = defaultdict(float)
        for e in entries:
            k = e.get("kind")
            if k == "stt":
                stt[f"{e.get('provider', '?')}:{e.get('model', '')}"] += float(e.get("audio_seconds") or 0)
            elif k == "llm":
                bucket = llm[f"{e.get('provider', '?')}:{e.get('model', '')}"]
                bucket["calls"] += 1
                bucket["failed"] += 0 if e.get("ok", True) else 1
                for f in ("input_tokens", "output_tokens", "images"):
                    bucket[f] += float(e.get(f) or 0)
            elif k == "render":
                render["wall_seconds"] += float(e.get("wall_seconds") or 0)
                render["output_seconds"] += float(e.get("output_seconds") or 0)
                render["calls"] += 1
            elif k == "download":
                download[f"{e.get('source', '?')}_bytes"] += float(e.get("bytes") or 0)
                download[f"{e.get('source', '?')}_seconds"] += float(e.get("seconds") or 0)
            elif k == "api":
                api[str(e.get("provider", "?"))] += float(e.get("requests") or 1)
        return {
            "wall_seconds": round(time.monotonic() - self.started, 2),
            "stt_seconds": {k: round(v, 2) for k, v in stt.items()},
            "llm": {k: {f: round(v, 2) for f, v in b.items()} for k, b in llm.items()},
            "render": {k: round(v, 2) for k, v in render.items()},
            "download": {k: round(v, 2) for k, v in download.items()},
            "api_requests": dict(api),
            "entries": len(entries),
        }


_current: contextvars.ContextVar[Optional[Meter]] = contextvars.ContextVar("clip_meter", default=None)
_process_meter: Optional[Meter] = None
_process_lock = threading.Lock()


def active() -> Optional[Meter]:
    return _current.get() or _process_meter


def record(kind: str, **fields: Any) -> None:
    meter = active()
    if meter is not None:
        meter.record(kind, **fields)


@contextmanager
def metered() -> Iterator[Meter]:
    global _process_meter
    meter = Meter()
    token = _current.set(meter)
    with _process_lock:
        previous, _process_meter = _process_meter, meter
    try:
        yield meter
    finally:
        _current.reset(token)
        with _process_lock:
            _process_meter = previous


@contextmanager
def timed(kind: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """`with timed("render", stage="clip") as f: ...; f["output_seconds"] = 31.2` records wall time."""
    extra: dict[str, Any] = {}
    t0 = time.monotonic()
    try:
        yield extra
    finally:
        record(kind, wall_seconds=round(time.monotonic() - t0, 3), **fields, **extra)
