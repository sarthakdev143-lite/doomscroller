"""Editing a finished clip by rendering it again, server-side.

Clips stay burned-in (captions, graphics, memes and sound are part of the pixels and the mix), so an edit
is a re-render. When the seam makes a clip it also keeps what is needed to make it again without the
original video or the network:

* ``<clip>_source.mp4``: the stretch of the source the clip came from, two seconds either side (starting on
  a whole second), re-encoded near-losslessly so its first frame is exactly where the manifest says;
* ``<clip>_assets/``: the stock-footage windows, meme files, meme sounds, music bed, ending sting and logo;
* ``<clip>_manifest.json``: the moment, the measured signals and the whole edit plan in the excerpt's time,
  plus the clip-timeline view an editor shows (caption words, memes, cutaways, beats).

:func:`rerender_clip` applies an edit spec to a manifest and renders a new file. The spec is the whole
desired edit, always applied to the ORIGINAL manifest (it is not cumulative, and a re-render writes no new
manifest). It never searches or downloads anything: memes come only from the manifest or the customer's own
files. The same manifest and spec give the same render, and the work is metered like a clip job.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from app.config import settings

from . import metering
from .broll import StockClip
from .edit import Beat, Cutaway, EditPlan, MemeOverlay, Reaction
from .media import MediaError, MediaInfo, probe, run_ffmpeg
from .memes import CUSTOM, MemeClip, _playable, custom_candidates, identity_keys
from .moments import Moment
from .render import LOGO_POSITIONS, NO_SOUND, ClipPlan
from .sfx import IMPACT, POP, WHOOSH
from .signals import Signals
from .timeline import Segment, on_grid, source_time
from .transcribe import Word

log = logging.getLogger(__name__)

MANIFEST_VERSION = 1
EXCERPT_PAD_SEC = 2.0
_KIND_SOUNDS = (IMPACT, POP, WHOOSH, NO_SOUND)
# Settings the render reads, kept so a later re-render looks like the first one.
_RENDER_SETTINGS = ("clip_backdrop_blur", "clip_layout", "clip_loop_ending", "clip_music_db",
                    "clip_music_synth_fallback", "clip_silence_gap_sec", "clip_sfx")
# Limits the spec is held to.
MAX_CAPTION_CHARS = 40
MAX_HOOK_CHARS = 80
MAX_WATERMARK_CHARS = 40
MAX_MEMES = 10
MIN_CLIP_SEC = 3.0
MEME_SEC_RANGE = (0.5, 4.0)
LOGO_SCALE_RANGE = (0.06, 0.25)
MUSIC_DB_RANGE = (-30.0, -6.0)
# The names saas/jobs/editor.py's mirror test reads.
_MEME_SEC_RANGE, _LOGO_SCALE_RANGE, _MUSIC_DB_RANGE = MEME_SEC_RANGE, LOGO_SCALE_RANGE, MUSIC_DB_RANGE
_SPEC_SHAPE = {
    "trim": {"start_s", "end_s"},
    "captions": {"enabled", "edits"},
    "hook": {"enabled", "text"},
    "logo": {"enabled", "position", "scale"},
    "memes": None,
    "sfx": {"enabled"},
    "music": {"enabled", "volume_db"},
    "broll": {"enabled"},
    "watermark_text": None,
}
_MEME_KEYS = {"id", "start_s", "duration_s", "sound"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


class SpecError(ValueError):
    """An edit spec that cannot be applied, with a message safe to show the customer."""


class ManifestError(SpecError):
    """A manifest, or a file it names, that is missing or damaged."""


# ---------------------------------------------------------------- writing the bundle

def _rel(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _copy(src: object, folder: Path, name: str) -> Optional[Path]:
    if not src:
        return None
    path = Path(str(src))
    if not path.is_file():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}{path.suffix.lower()}"
    if not target.exists():
        shutil.copy2(path, target)
    return target


def _word(w: Word, shift: float) -> dict:
    return {"text": w.text, "start": w.start - shift, "end": w.end - shift, "confidence": float(w.confidence)}


def _signals_doc(signals: object, lo: int, hi: float) -> Optional[dict]:
    """The per-second signals of the excerpt, so a re-render frames shots exactly as the first render did."""
    try:
        a, b = lo, min(int(signals.seconds), int(math.ceil(hi)))
        if b <= a:
            return None
        return {"duration": hi - lo,
                **{name: np.asarray(getattr(signals, name))[a:b].tolist()
                   for name in ("motion", "loudness", "brightness", "centroid", "centroid_weight")},
                "cuts": [float(c) - lo for c in signals.cuts if math.isfinite(c) and lo <= c <= hi]}
    except (AttributeError, TypeError, ValueError):
        return None


def write_bundle(clip_path: Path, *, src_path: str, info: MediaInfo, signals: object, moment: Moment,
                 edit: Optional[EditPlan], plan: ClipPlan, words: Sequence[Word], language: str, timed: bool,
                 burned_in: bool, captions: bool, track: Optional[str], sting: Optional[str], brand: str,
                 watermark: str, logo: str, options: dict, title: str, description: str,
                 hashtags: Sequence[str], score: float, custom_memes: Sequence[str] = ()) -> Path:
    """Write the excerpt, assets and manifest beside ``clip_path``. Returns the manifest's path."""
    clip_path = Path(clip_path)
    base = clip_path.parent
    stem = clip_path.stem
    assets = base / f"{stem}_assets"
    # The cut as rendered: an opening the render skipped is gone, and the end hold is not part of it.
    cut: list[Segment] = [tuple(s) for s in getattr(plan, "edit_segments", ())] or (
        list(edit.segments) if edit else [(moment.start, moment.end)])
    start = getattr(plan, "moment_start", 0.0) or moment.start
    end = getattr(plan, "moment_end", 0.0) or moment.end
    speed = plan.speed if math.isfinite(plan.speed) and plan.speed > 0 else 1.0
    view = replace(edit, segments=cut, speed=speed) if edit else EditPlan(words=[], segments=cut, speed=1.0)
    first, last = min([start] + [s for s, _ in cut]), max([end] + [e for _, e in cut])
    duration = info.duration if math.isfinite(info.duration) and info.duration > 0 else last + EXCERPT_PAD_SEC
    # A whole second, so the cut stays on the frame grid and the per-second signals slice cleanly.
    lo = max(0, int(math.floor(first - EXCERPT_PAD_SEC)))
    hi = min(duration, last + EXCERPT_PAD_SEC)
    excerpt = base / f"{stem}_source.mp4"
    with metering.timed("render", stage="excerpt", output_seconds=0.0):
        run_ffmpeg(["-ss", f"{lo}", "-i", str(src_path), "-t", f"{hi - lo:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(excerpt)])

    def inside(t: float) -> bool:
        return cut[0][0] - 1e-6 <= t <= cut[-1][1] + 1e-6

    edit_doc = None
    captions_out: list[dict] = []
    memes_out: list[dict] = []
    cutaways_out: list[dict] = []
    beats_out: list[dict] = []
    available: dict[str, dict] = {}
    if edit is not None:
        cutaway_docs = []
        for index, c in enumerate(edit.cutaways):
            if c.clip is None:
                continue
            window = assets / f"broll_{index}.mp4"
            assets.mkdir(parents=True, exist_ok=True)
            # Only the stretch the render shows (it reads `seconds + 0.2` from the offset), at most 1080p.
            with metering.timed("render", stage="excerpt", output_seconds=0.0):
                run_ffmpeg(["-ss", f"{c.clip.offset:.3f}", "-i", c.clip.path, "-t", f"{c.seconds + 0.5:.3f}", "-an",
                            "-vf", "scale='min(1920,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                            "-pix_fmt", "yuv420p", str(window)])
            cutaway_docs.append({"at": c.at - lo, "seconds": c.seconds, "query": c.query, "subject": c.subject,
                                 "clip": {"video_id": c.clip.video_id, "path": _rel(window, base),
                                          "duration": c.seconds + 0.5, "author": c.clip.author,
                                          "page_url": c.clip.page_url, "provider": c.clip.provider}})
            when = view.to_output(c.at)
            if when is not None and inside(c.at):
                cutaways_out.append({"stock_id": c.clip.video_id, "start_s": round(when, 3),
                                     "duration_s": round(c.seconds, 3), "query": c.query})
        meme_docs = []
        for index, m in enumerate(edit.meme_overlays):
            if m.clip is None:
                continue
            copied = _copy(m.clip.path, assets, f"meme_{re.sub(r'[^A-Za-z0-9_-]+', '_', m.clip.meme_id)}")
            if copied is None:
                continue
            sound = m.sound if m.sound in _KIND_SOUNDS else None
            if sound is None:
                sound_file = _copy(m.sound, assets, f"meme_sound_{index}")
                sound = _rel(sound_file, base) if sound_file else IMPACT
            entry = {"meme_id": m.clip.meme_id, "path": _rel(copied, base), "title": m.clip.title,
                     "provider": m.clip.provider}
            meme_docs.append({"at": m.at - lo, "seconds": m.seconds, "mood": m.mood, "sound": sound, "clip": entry})
            available.setdefault(m.clip.meme_id, {"id": m.clip.meme_id, "path": entry["path"], "title": m.clip.title,
                                                  "provider": m.clip.provider, "mood": m.mood, "sound": sound})
            when = view.to_output(m.at)
            if when is not None:
                memes_out.append({"id": m.clip.meme_id, "start_s": round(when, 3), "duration_s": m.seconds,
                                  "sound": sound != NO_SOUND})
        for b in edit.beats:
            when = view.to_output(b.at)
            if when is not None and inside(b.at):
                beats_out.append({"start_s": round(when, 3), "style": b.style, "text": b.text})
        edit_doc = {
            "segments": [[s - lo, e - lo] for s, e in cut],
            "speed": edit.speed,
            "words": [_word(w, lo) for w in edit.words],
            "opening_words": [_word(w, lo) for w in edit.opening_words],
            "beats": [{"at": b.at - lo, "text": b.text, "style": b.style, "label": b.label} for b in edit.beats],
            "reactions": [{"at": r.at - lo, "mood": r.mood} for r in edit.reactions],
            "payoff_at": None if edit.payoff_at is None else edit.payoff_at - lo,
            "tail_room": edit.tail_room,
            "cutaways": cutaway_docs,
            "memes": meme_docs,
        }
    kept_words = [w for w in words if lo <= w.start and w.end <= hi]
    if timed and captions:
        # Caption i indexes the edit's words when there is an edit, else the manifest's own words.
        pool = edit.words if edit is not None else kept_words
        for i, w in enumerate(pool):
            begin, finish = view.to_output(w.start), view.to_output(w.end)
            if begin is not None and inside(w.start) and (edit is not None or start <= w.start < end):
                captions_out.append({"i": i, "text": w.text, "start_s": round(begin, 3),
                                     "end_s": round(finish if finish is not None else begin, 3)})
    for cand in custom_candidates(custom_memes):
        copied = _copy(cand.link, assets, f"custom_{cand.meme_id}")
        if copied is not None and cand.meme_id not in available:
            available[cand.meme_id] = {"id": cand.meme_id, "path": _rel(copied, base), "title": cand.title,
                                       "provider": CUSTOM, "mood": "", "sound": None}
    music_file = _copy(track, assets, "music") if track and plan.music else None
    sting_file = _copy(sting, assets, "ending_sting") if sting and plan.tail else None
    logo_file = _copy(logo, assets, "logo") if logo else None
    manifest = {
        "version": MANIFEST_VERSION,
        "clip": clip_path.name,
        "excerpt": excerpt.name,
        "excerpt_offset_s": lo,
        "duration_s": round(plan.duration, 3),
        "moment": {"start": start - lo, "end": end - lo, "title": moment.title, "hook": moment.hook,
                   "reveal": moment.reveal, "kind": moment.kind, "description": moment.description,
                   "tags": list(moment.tags), "score": moment.score, "sound": moment.sound if sting_file else ""},
        "words": [_word(w, lo) for w in kept_words],
        "caption_words": "edit" if edit is not None else "words",
        "signals": _signals_doc(signals, lo, hi),
        "language": language,
        "timed": timed,
        "burned_in": burned_in,
        "layout": settings.clip_layout,
        "render_settings": {name: getattr(settings, name) for name in _RENDER_SETTINGS},
        "options": dict(options),
        "brand": brand,
        "watermark_text": watermark,
        "logo": _rel(logo_file, base) if logo_file else None,
        "music": {"track": _rel(music_file, base) if music_file else None, "volume_db": settings.clip_music_db},
        "ending_sting": _rel(sting_file, base) if sting_file else None,
        "title": title,
        "description": description,
        "hashtags": list(hashtags),
        "score": score,
        "edit": edit_doc,
        # The clip-timeline view an editor shows; every time is in seconds from the clip's first frame.
        "captions": captions_out,
        "memes": memes_out,
        "cutaways": cutaways_out,
        "beats": beats_out,
        "memes_available": list(available.values()),
    }
    path = base / f"{stem}_manifest.json"
    partial = path.with_suffix(".json.tmp")
    partial.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    partial.replace(path)
    return path


# ---------------------------------------------------------------- the spec

def _fail(message: str) -> None:
    raise SpecError(message)


def _keys(obj: object, allowed: set[str], where: str) -> dict:
    if not isinstance(obj, dict):
        _fail(f"{where} must be an object")
    unknown = sorted(str(k) for k in set(obj) - allowed)
    if unknown:
        _fail(f"unknown field{'s' if len(unknown) > 1 else ''} in {where}: {', '.join(unknown)[:200]}")
    return obj


def _bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{where} must be true or false")
    return value


def _number(value: object, where: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        _fail(f"{where} must be a number")
    return min(hi, max(lo, float(value)))


def _text(value: object, where: str, limit: int, *, allow_empty: bool = False) -> str:
    from app.transformers.ffmpeg_transformer import is_renderable

    if not isinstance(value, str):
        _fail(f"{where} must be text")
    clean = " ".join(_CONTROL.sub(" ", value).split())
    if len(clean) > limit:
        _fail(f"{where} is longer than {limit} characters")
    if not clean and not allow_empty:
        _fail(f"{where} cannot be empty")
    if clean and not is_renderable(clean):
        _fail(f"{where} uses letters the caption font cannot draw")
    return clean


def validate_spec(spec: object, manifest: dict, available_ids: Sequence[str]) -> dict:
    """The spec, checked and clamped. Raises SpecError on anything it cannot apply."""
    spec = _keys(spec if spec is not None else {}, set(_SPEC_SHAPE), "the edit")
    duration = float(manifest.get("duration_s") or 0.0)
    out: dict = {}
    for key, value in spec.items():
        allowed = _SPEC_SHAPE[key]
        if key == "memes":
            if not isinstance(value, list):
                _fail("memes must be a list")
            if len(value) > MAX_MEMES:
                _fail(f"at most {MAX_MEMES} memes")
            if value and manifest.get("edit") is None:
                _fail("memes can only go on a clip cut from speech")
            memes = []
            for n, item in enumerate(value):
                item = _keys(item, _MEME_KEYS, f"memes[{n}]")
                if not isinstance(item.get("id"), str) or item["id"] not in available_ids:
                    _fail(f"memes[{n}].id must be one of the clip's memes or your own")
                memes.append({"id": item["id"],
                              "start_s": _number(item.get("start_s", 0.0), f"memes[{n}].start_s", 0.0,
                                                 max(0.0, duration - MEME_SEC_RANGE[0])),
                              "duration_s": _number(item.get("duration_s", 1.5), f"memes[{n}].duration_s",
                                                    *MEME_SEC_RANGE),
                              "sound": _bool(item.get("sound", True), f"memes[{n}].sound")})
            # Memes share one corner, so an earlier one ends where the next begins.
            memes.sort(key=lambda m: m["start_s"])
            for earlier, later in zip(memes, memes[1:]):
                room = later["start_s"] - earlier["start_s"]
                if room < MEME_SEC_RANGE[0]:
                    _fail("two memes start less than half a second apart")
                earlier["duration_s"] = min(earlier["duration_s"], room)
            out["memes"] = [{**m, "start_s": round(m["start_s"], 3), "duration_s": round(m["duration_s"], 3)}
                            for m in memes]
            continue
        if key == "watermark_text":
            out["watermark_text"] = None if value is None else _text(value, "watermark_text", MAX_WATERMARK_CHARS,
                                                                      allow_empty=True)
            continue
        part = _keys(value, allowed, key)
        checked: dict = {}
        if "enabled" in part:
            checked["enabled"] = _bool(part["enabled"], f"{key}.enabled")
        if key == "trim":
            start = _number(part.get("start_s", 0.0), "trim.start_s", 0.0, duration)
            end = _number(part.get("end_s", duration), "trim.end_s", 0.0, duration)
            if end - start < MIN_CLIP_SEC:
                _fail(f"a trimmed clip must keep at least {MIN_CLIP_SEC:g} seconds")
            checked = {"start_s": round(start, 3), "end_s": round(end, 3)}
        elif key == "captions" and "edits" in part:
            if not isinstance(part["edits"], list):
                _fail("captions.edits must be a list")
            known = {c["i"] for c in manifest.get("captions") or []}
            edits = {}
            for n, item in enumerate(part["edits"]):
                item = _keys(item, {"i", "text"}, f"captions.edits[{n}]")
                index = item.get("i")
                if isinstance(index, bool) or not isinstance(index, int) or index not in known:
                    _fail(f"captions.edits[{n}].i is not a caption word of this clip")
                edits[index] = _text(item.get("text"), f"captions.edits[{n}].text", MAX_CAPTION_CHARS)
            checked["edits"] = edits
        elif key == "hook" and "text" in part:
            checked["text"] = _text(part["text"], "hook.text", MAX_HOOK_CHARS, allow_empty=True)
        elif key == "logo":
            if "position" in part:
                if part["position"] not in LOGO_POSITIONS[1:]:
                    _fail(f"logo.position must be one of {', '.join(LOGO_POSITIONS[1:])}")
                checked["position"] = part["position"]
            if "scale" in part:
                checked["scale"] = round(_number(part["scale"], "logo.scale", *LOGO_SCALE_RANGE), 3)
        elif key == "music" and "volume_db" in part:
            checked["volume_db"] = round(_number(part["volume_db"], "music.volume_db", *MUSIC_DB_RANGE), 2)
        out[key] = checked
    return out


# ---------------------------------------------------------------- rebuilding and re-rendering

def _asset(base: Path, rel: object) -> str:
    """A path the manifest names, which must stay inside the clip's own folder."""
    path = (base / str(rel)).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ManifestError(f"the clip's file {str(rel)[:80]} is missing")
    return str(path)


def _words(items: Sequence[dict]) -> list[Word]:
    return [Word(str(w["text"]), float(w["start"]), float(w["end"]), float(w.get("confidence", 1.0))) for w in items]


def load_signals(doc: Optional[dict]) -> Optional[Signals]:
    if not doc:
        return None
    return Signals(duration=float(doc["duration"]),
                   **{name: np.asarray(doc[name], dtype=np.float64)
                      for name in ("motion", "loudness", "brightness", "centroid", "centroid_weight")},
                   cuts=[float(c) for c in doc.get("cuts") or []])


def load_edit(doc: Optional[dict], base: Path) -> Optional[EditPlan]:
    if doc is None:
        return None
    plan = EditPlan(words=_words(doc.get("words") or []), segments=[(float(a), float(b)) for a, b in doc["segments"]],
                    speed=float(doc.get("speed") or 1.0), opening_words=_words(doc.get("opening_words") or []))
    plan.beats = [Beat(float(b["at"]), str(b.get("text") or ""), str(b["style"]), str(b.get("label") or ""))
                  for b in doc.get("beats") or []]
    plan.reactions = [Reaction(float(r["at"]), str(r["mood"])) for r in doc.get("reactions") or []]
    plan.payoff_at = doc.get("payoff_at")
    plan.tail_room = float(doc.get("tail_room") or 0.0)
    plan.cutaways = []
    for c in doc.get("cutaways") or []:
        clip = c["clip"]
        plan.cutaways.append(Cutaway(float(c["at"]), float(c["seconds"]), str(c.get("query") or ""),
                                     StockClip(clip["video_id"], _asset(base, clip["path"]), float(clip["duration"]),
                                               clip.get("author", ""), clip.get("page_url", ""), 0.0,
                                               clip.get("provider", "")),
                                     subject=str(c.get("subject") or "")))
    plan.meme_overlays = []
    for m in doc.get("memes") or []:
        clip = m["clip"]
        sound = m.get("sound") or IMPACT
        plan.meme_overlays.append(MemeOverlay(float(m["at"]), float(m["seconds"]),
                                              MemeClip(clip["meme_id"], _asset(base, clip["path"]),
                                                       clip.get("title", ""), clip.get("provider", "")),
                                              mood=str(m.get("mood") or ""),
                                              sound=sound if sound in _KIND_SOUNDS else _asset(base, sound)))
    return plan


def _keep_between(edit: EditPlan, a: float, b: float) -> None:
    """Drop what falls outside [a, b]: the timeline would snap it to the first frame instead."""
    def ok(t: Optional[float]) -> bool:
        return t is not None and a - 1e-6 <= t <= b + 1e-6

    edit.words = [w for w in edit.words if ok(w.start)]
    edit.opening_words = [w for w in edit.opening_words if ok(w.start)]
    edit.beats = [x for x in edit.beats if ok(x.at)]
    edit.reactions = [x for x in edit.reactions if ok(x.at)]
    edit.cutaways = [x for x in edit.cutaways if ok(x.at)]
    edit.meme_overlays = [x for x in edit.meme_overlays if ok(x.at)]
    if edit.payoff_at is not None and not ok(edit.payoff_at):
        edit.payoff_at = None


def _trim_edit(edit: EditPlan, start_out: float, end_out: float) -> bool:
    """Cut the edit to [start_out, end_out] of its own timeline. True when the end was cut."""
    speed = edit._effective_speed
    a = source_time(edit.segments, start_out * speed)
    b = source_time(edit.segments, end_out * speed) if end_out < edit.duration - 1e-3 else edit.segments[-1][1]
    kept = []
    for s, e in edit.segments:
        s2, e2 = max(s, a), min(e, b)
        if e2 - s2 > 0.05:
            kept.append((on_grid(s2), on_grid(e2)))
    if not kept:
        raise SpecError("the trim leaves nothing of the clip")
    full_end = end_out >= edit.duration - 1e-3
    edit.segments = kept
    # The tail after the last word belongs to the original ending; a trimmed one has none.
    _keep_between(edit, kept[0][0], kept[-1][1] + (edit.tail_room if full_end else 0.0))
    return not full_end


def rerender_clip(manifest_path: str | Path, spec: Optional[dict], out_path: str | Path, *,
                  custom_memes: Sequence[str] = (), logo_path: Optional[str] = None,
                  sfx_pack_dir: Optional[Path] = None, music_dir: Optional[Path] = None,
                  cache_dir: Optional[Path] = None,
                  on_stage: Optional[Callable[[str, float], None]] = None) -> dict:
    """Render a clip again from its manifest and excerpt with ``spec`` applied. Returns the clip dict.

    Spec times are on the ORIGINAL clip's timeline (the manifest's), whatever the trim. Raises
    :class:`app.clipper.standalone.ClipError` with code ``invalid_manifest``, ``invalid_spec`` or
    ``render_failed``.
    """
    from . import standalone

    manifest_path = Path(manifest_path)
    base = manifest_path.parent
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise standalone.ClipError("invalid_manifest", f"the clip's manifest could not be read: {exc}") from None
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise standalone.ClipError("invalid_manifest", "the clip's manifest is from another version")
    available = {m["id"]: m for m in manifest.get("memes_available") or []}
    customs = custom_candidates(custom_memes)
    for cand in customs:
        # The caller's own files, named by them rather than by the manifest.
        available.setdefault(cand.meme_id, {"id": cand.meme_id, "file": str(Path(cand.link).resolve()),
                                            "title": cand.title, "provider": CUSTOM, "mood": "", "sound": None})
    try:
        excerpt = _asset(base, manifest["excerpt"])
        edit = load_edit(manifest.get("edit"), base)
        spec = validate_spec(spec, manifest, list(available))
    except ManifestError as exc:
        raise standalone.ClipError("invalid_manifest", str(exc)) from None
    except SpecError as exc:
        raise standalone.ClipError("invalid_spec", str(exc)) from None
    except (KeyError, TypeError, ValueError) as exc:
        raise standalone.ClipError("invalid_manifest", f"the clip's manifest is damaged: {exc}") from None

    def report(stage: str, progress: float) -> None:
        if on_stage is not None:
            on_stage(stage, progress)

    m = manifest["moment"]
    moment = Moment(float(m["start"]), float(m["end"]), title=m.get("title", ""), hook=m.get("hook", ""),
                    kind=m.get("kind", "speech"), description=m.get("description", ""), tags=tuple(m.get("tags") or ()),
                    score=float(m.get("score") or 0.0), reveal=m.get("reveal") or "", sound=m.get("sound") or "")
    source_words = _words(manifest.get("words") or [])
    original = list(edit.segments) if edit else [(moment.start, moment.end)]
    speed = edit._effective_speed if edit else 1.0
    options = dict(manifest.get("options") or {})
    timed = bool(manifest.get("timed"))
    captions_on = bool(options.get("captions", True))
    summary = True
    try:
        # Memes first: their times are on the original timeline, before a trim moves it.
        if "memes" in spec:
            overlays = []
            for item in spec["memes"]:
                meme = available[item["id"]]
                path = meme["file"] if "file" in meme else _asset(base, meme["path"])
                if meme.get("provider") == CUSTOM and not _playable(Path(path), item["duration_s"]):
                    raise SpecError(f"the meme {meme.get('title') or item['id']} will not play")
                if not item["sound"]:
                    sound = NO_SOUND
                elif meme.get("sound") in _KIND_SOUNDS:
                    sound = meme["sound"]
                elif meme.get("sound"):
                    sound = _asset(base, meme["sound"])
                else:
                    sound = _custom_sound(path)
                at = source_time(original, item["start_s"] * speed)
                overlays.append(MemeOverlay(at, item["duration_s"],
                                            MemeClip(item["id"], path, meme.get("title", ""), meme.get("provider", "")),
                                            mood=meme.get("mood", ""), sound=sound))
            edit.meme_overlays = overlays
        if "captions" in spec:
            if "enabled" in spec["captions"]:
                captions_on = spec["captions"]["enabled"]
                summary = captions_on
            for index, text in (spec["captions"].get("edits") or {}).items():
                if manifest.get("caption_words") == "edit" and edit is not None:
                    edit.words[index] = replace(edit.words[index], text=text)
                else:
                    source_words[index] = replace(source_words[index], text=text)
        if "hook" in spec:
            if spec["hook"].get("enabled") is False:
                moment.hook = ""
            elif "text" in spec["hook"]:
                moment.hook = spec["hook"]["text"]
        if spec.get("broll", {}).get("enabled") is False and edit is not None:
            edit.cutaways = []
        if "trim" in spec:
            start, end = spec["trim"]["start_s"], spec["trim"]["end_s"]
            if edit is not None:
                end_cut = _trim_edit(edit, start, end)
            else:
                full = sum(e - s for s, e in original)
                end_cut = end < full - 1e-3
                moment = replace(moment, start=on_grid(source_time(original, start)),
                                 end=on_grid(source_time(original, end)) if end_cut else moment.end)
            if end_cut:
                # The ending sting (and the frame held under it) belongs to the original last line.
                moment = replace(moment, sound="")
        logo = _asset(base, manifest["logo"]) if manifest.get("logo") else ""
        track = _asset(base, manifest["music"]["track"]) if (manifest.get("music") or {}).get("track") else None
        sting = _asset(base, manifest["ending_sting"]) if manifest.get("ending_sting") else None
    except ManifestError as exc:
        raise standalone.ClipError("invalid_manifest", str(exc)) from None
    except SpecError as exc:
        raise standalone.ClipError("invalid_spec", str(exc)) from None
    # logo_path is the customer's current logo: it replaces the clip's own logo file, and is what a spec that
    # turns a logo on uses for a clip made without one. It never adds a logo nobody asked for.
    logo_spec = spec.get("logo", {})
    asked = bool(logo_spec.get("enabled") or "position" in logo_spec or "scale" in logo_spec)
    if logo_spec.get("enabled") is False:
        logo = ""
    else:
        if logo_path and (logo or asked):
            if not standalone.logo_ok(str(logo_path)):
                raise standalone.ClipError("invalid_spec", "the logo file is not a readable image")
            logo = str(logo_path)
        if asked and not logo:
            raise standalone.ClipError("invalid_spec", "this clip has no logo to show")
    watermark = manifest.get("watermark_text") or ""
    if "watermark_text" in spec:
        watermark = spec["watermark_text"] or ""
    music_spec = spec.get("music", {})
    music_on = music_spec.get("enabled", track is not None)
    sfx_on = spec.get("sfx", {}).get("enabled", bool((manifest.get("render_settings") or {}).get("clip_sfx", True)))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as scratch:
        # Without a cache folder the scratch work (a synthesised music bed) goes in a temporary one, never
        # beside the clip, where it would outlive the edit.
        cache = (Path(cache_dir).expanduser().resolve() if cache_dir
                 else Path(scratch.enter_context(tempfile.TemporaryDirectory(prefix="rerender_"))))
        return _render(manifest, manifest_path, excerpt, edit, moment, source_words, out_path, cache,
                       timed=timed, captions_on=captions_on, summary=summary, logo=logo, logo_spec=logo_spec,
                       watermark=watermark, music_spec=music_spec, music_on=music_on, track=track, sfx_on=sfx_on,
                       sting=sting, sfx_pack_dir=sfx_pack_dir, music_dir=music_dir, report=report)


def _render(manifest: dict, manifest_path: Path, excerpt: str, edit: Optional[EditPlan], moment: Moment,
            source_words: list[Word], out_path: Path, cache: Path, *, timed: bool, captions_on: bool, summary: bool,
            logo: str, logo_spec: dict, watermark: str, music_spec: dict, music_on: bool, track: Optional[str],
            sfx_on: bool, sting: Optional[str], sfx_pack_dir: Optional[Path], music_dir: Optional[Path],
            report: Callable[[str, float], None]) -> dict:
    from . import standalone
    from .render import render_moment
    from .signals import analyse

    values = {name: value for name, value in (manifest.get("render_settings") or {}).items() if name in _RENDER_SETTINGS}
    values.update({
        "content_dir": str(cache),
        "clip_sfx": bool(sfx_on),
        "clip_music": bool(music_on),
        "clip_sfx_pack_dir": str(standalone.resolve_sfx_pack(sfx_pack_dir, cache)),
        "clip_music_dir": str(Path(music_dir).expanduser() if music_dir else cache / "music"),
        "brand_name": "",
    })
    if "volume_db" in music_spec:
        values["clip_music_db"] = music_spec["volume_db"]
    with standalone._lock, standalone._overrides(values), standalone._meter() as meter:
        if music_on and track is None:
            from . import music as music_mod
            picked = music_mod.pick(Path(manifest["clip"]).stem)
            track = str(picked) if picked else None
        report("rendering", 0.1)
        info = probe(excerpt)
        signals = load_signals(manifest.get("signals")) or analyse(excerpt, info)
        try:
            plan = render_moment(excerpt, info, signals, moment, source_words if timed else [], str(out_path),
                                 edit=edit, creator="", music=track if music_on else None,
                                 captions=timed and captions_on, source_captioned=bool(manifest.get("burned_in")),
                                 brand=manifest.get("brand") or "", sting=sting if moment.sound else None,
                                 language=manifest.get("language") or "", watermark=watermark, logo=logo,
                                 logo_position=logo_spec.get("position", "center"),
                                 logo_scale=float(logo_spec.get("scale", 0.0)), summary=summary,
                                 skip_dark_opening=False)
        except MediaError as exc:
            raise standalone.ClipError("render_failed", str(exc)) from None
        thumb = standalone.make_thumbnail(out_path, plan.duration)
        report("done", 1.0)
        meter_summary = meter.summary() if meter is not None else {}
    used = [o.clip for o in (edit.meme_overlays if edit else []) if o.clip is not None and edit.to_output(o.at) is not None]
    offset = float(manifest.get("excerpt_offset_s") or 0.0)
    cut = plan.edit_segments or ((tuple(edit.segments) if edit else ((moment.start, moment.end),)))
    return {
        "path": str(out_path),
        "thumbnail_path": str(thumb) if thumb else None,
        "duration_s": round(plan.duration, 2),
        "start_s": round(offset + cut[0][0], 2),
        "end_s": round(offset + cut[-1][1], 2),
        "title": manifest.get("title", ""),
        "description": manifest.get("description", ""),
        "hashtags": list(manifest.get("hashtags") or []),
        "score": manifest.get("score", 0.0),
        "language": manifest.get("language", ""),
        "kind": moment.kind,
        "hook": moment.hook,
        "memes_used": [key for clip in used for key in identity_keys(clip)],
        "manifest_path": str(manifest_path),
        "source_excerpt_path": excerpt,
        "meter": meter_summary,
    }


def _custom_sound(path: str) -> str:
    """A customer's own meme plays its own audio when it has some, else a hit."""
    try:
        if Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"} and probe(path).has_audio:
            return path
    except MediaError:
        pass
    return IMPACT
