"""The sound pack the SaaS ships: CC0 only, so no customer's clip can draw a claim for a sound.

The owner's own pack (``content/sfx/``) holds MyInstants meme sounds, which are fine for one channel
but not licensed for resale. This pack is laid out the same way (``trending/<name>.wav``, named with the
words :data:`app.clipper.reactions.STINGS` looks for), so reaction stings and ending sounds resolve
without code changes, and it carries ``MANIFEST.json`` with every file's source and licence.

Two ways to fill it (``scripts/fetch_sfx.py --cc0``):

* **Freesound**, when ``FREESOUND_API_KEY`` is set: one CC0 sound per mood query, filtered to CC0 by the
  search itself and checked again on every result. Moods Freesound cannot fill are synthesised.
* **Synthesis** otherwise: every sound is generated here from seeded numpy maths, with no samples, so the
  pack is our own work and is dedicated to the public domain (CC0 1.0).

:func:`ensure_pack` builds the synthesised pack on first use, so a deployment never runs without one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from app.config import settings

from . import reactions
from .sfx import RATE, _impact

log = logging.getLogger(__name__)

MANIFEST = "MANIFEST.json"
CC0 = "CC0-1.0"
CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
_PEAK = 0.89
_lock = threading.Lock()


def pack_root() -> Path:
    configured = (settings.clip_cc0_sfx_dir or "").strip()
    return Path(configured).expanduser() if configured else settings.content_path / "sfx_cc0"


# ---------------------------------------------------------------- synthesis

def _t(seconds: float) -> np.ndarray:
    return np.arange(int(RATE * seconds)) / RATE


def _phase(freq: np.ndarray) -> np.ndarray:
    return 2 * np.pi * np.cumsum(freq) / RATE


def _lowpass(x: np.ndarray, cutoff: float, taps: int = 129) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / RATE * n) * np.hamming(taps)
    return np.convolve(x, h / h.sum(), mode="same")


def _space(x: np.ndarray, amount: float = 0.25, seconds: float = 0.6) -> np.ndarray:
    """A small room: a few decaying echoes, so a dry synth does not sound like a test tone."""
    out = np.concatenate([x, np.zeros(int(RATE * seconds))])
    for delay_ms, gain in ((23, 0.6), (31, 0.5), (41, 0.42), (53, 0.35), (67, 0.28), (89, 0.2), (113, 0.14)):
        d = int(RATE * delay_ms / 1000)
        echo = np.zeros_like(out)
        echo[d:d + len(x)] = x[:len(out) - d]
        out += amount * gain * _lowpass(echo, 3500, 63)
    return out


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / RATE
    return np.minimum(1.0, t / max(attack, 1e-4)) * np.exp(-t * decay)


def _brass(freq: float, seconds: float, *, vibrato: float = 0.0, bright: float = 1.0, decay: float = 1.2) -> np.ndarray:
    t = _t(seconds)
    f = freq * (1 + vibrato * np.sin(2 * np.pi * 5.5 * t) * np.minimum(1.0, t / 0.4))
    phase = _phase(f)
    # Higher partials open with the attack and close as the note sustains: the "blat" of a brass stab.
    opening = np.exp(-t * 6) * 0.7 + 0.3
    tone = sum((1 / k) * np.minimum(1.0, bright * opening * 8 / k) * np.sin(k * phase) for k in range(1, 9))
    return tone * _env(len(t), 0.018, decay)


def _dun_dun() -> np.ndarray:
    hit = lambda f, s: _brass(f, s, bright=1.2, decay=2.5) + 0.9 * np.sin(_phase(np.full(int(RATE * s), 70.0))) * _env(int(RATE * s), 0.004, 9)
    parts = [hit(146.83, 0.2), np.zeros(int(RATE * 0.06)), hit(146.83, 0.2), np.zeros(int(RATE * 0.08)),
             _brass(116.54, 1.1, vibrato=0.012, bright=1.3, decay=1.1) + np.sin(_phase(np.full(int(RATE * 1.1), 58.0))) * _env(int(RATE * 1.1), 0.004, 3)]
    return _space(np.concatenate(parts), 0.3)


def _deep_boom() -> np.ndarray:
    t = _t(1.3)
    f = 38 + 52 * np.exp(-t * 9)
    body = np.tanh(2.5 * np.sin(_phase(f))) * np.exp(-t * 3.2)
    click = np.random.default_rng(7).standard_normal(len(t)) * np.exp(-t * 180) * 0.5
    return _space(body + _lowpass(click, 2500), 0.35)


def _boing() -> np.ndarray:
    t = _t(0.9)
    f = 210 * (1 + 0.45 * np.sin(2 * np.pi * 9 * t) * np.exp(-t * 2.2)) * (1 + 0.15 * t)
    phase = _phase(f)
    return (np.sin(phase) + 0.3 * np.sin(2 * phase) + 0.1 * np.sin(3 * phase)) * _env(len(t), 0.005, 3.5)


def _slide_up() -> np.ndarray:
    t = _t(0.55)
    f = 500 * (1600 / 500) ** np.minimum(1.0, t / 0.45) * (1 + 0.03 * np.sin(2 * np.pi * 6 * t))
    breath = _lowpass(np.random.default_rng(3).standard_normal(len(t)), 4000) * 0.05
    return (np.sin(_phase(f)) + breath) * _env(len(t), 0.03, 1.5) * np.minimum(1.0, (0.55 - t) / 0.06)


def _sad_trombone() -> np.ndarray:
    notes = [(293.66, 0.34), (277.18, 0.34), (261.63, 0.34), (246.94, 1.15)]
    out = []
    for i, (freq, seconds) in enumerate(notes):
        last = i == len(notes) - 1
        note = _brass(freq, seconds, vibrato=0.025 if last else 0.004, bright=0.8, decay=0.9 if last else 0.4)
        fade = int(RATE * 0.02)
        note[-fade:] *= np.linspace(1, 0, fade)
        out.append(note)
    return _space(np.concatenate(out), 0.2)


def _slip_down() -> np.ndarray:
    t = _t(0.55)
    f = 1800 * (350 / 1800) ** (t / 0.55) * (1 + 0.025 * np.sin(2 * np.pi * 7 * t))
    whistle = np.sin(_phase(f)) * _env(len(t), 0.01, 0.8)
    thud = _impact()
    return np.concatenate([whistle, np.zeros(int(RATE * 0.05)), thud * 1.1])


def _record_scratch() -> np.ndarray:
    rng = np.random.default_rng(11)
    base_t = _t(1.0)
    groove = _lowpass(rng.standard_normal(len(base_t)), 3000) * 0.6 + 0.5 * np.sign(np.sin(2 * np.pi * 196 * base_t))
    t = _t(0.62)
    # The hand drags the record back and forth twice, then lets it stop: speed swings through zero.
    position = 0.12 * (1 - np.cos(2 * np.pi * 3.4 * np.minimum(t, 0.44))) + np.where(t > 0.44, (t - 0.44) * 0.3, 0)
    scratch = np.interp(position * RATE + RATE * 0.2, np.arange(len(groove)), groove)
    speed = np.abs(np.gradient(position)) * RATE
    return _lowpass(scratch * np.minimum(1.0, speed / 0.25), 6000) * np.minimum(1.0, (0.62 - t) / 0.05)


def _crickets() -> np.ndarray:
    out = np.zeros(int(RATE * 1.7))
    for start, freq, level in ((0.0, 4600, 1.0), (0.55, 4600, 1.0), (1.1, 4600, 1.0), (0.27, 4200, 0.45), (0.82, 4200, 0.45)):
        for pulse in range(3):
            at = int(RATE * (start + pulse * 0.048))
            t = _t(0.03)
            chirp = np.sin(2 * np.pi * freq * t) * np.sin(np.pi * t / 0.03) ** 2 * level
            out[at:at + len(chirp)] += chirp[:len(out) - at]
    return _space(out, 0.15)


def _bell(freq: float, seconds: float) -> np.ndarray:
    t = _t(seconds)
    return (np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(2 * np.pi * 2.76 * freq * t)
            + 0.18 * np.sin(2 * np.pi * 5.4 * freq * t) * np.exp(-t * 6)) * _env(len(t), 0.002, 4.2)


def _sparkle() -> np.ndarray:
    out = np.zeros(int(RATE * 1.4))
    for i, freq in enumerate((1046.5, 1318.5, 1568.0, 2093.0)):
        at = int(RATE * 0.07 * i)
        bell = _bell(freq, 1.1) * (0.7 + 0.1 * i)
        out[at:at + len(bell)] += bell[:len(out) - at]
    t = _t(1.4)
    shimmer = (np.random.default_rng(5).standard_normal(len(t)) - _lowpass(np.random.default_rng(5).standard_normal(len(t)), 6000))
    swell = np.sin(_phase(np.full(len(t), 130.0))) * _env(len(t), 0.08, 2.5) * 0.35
    return _space(out + shimmer * 0.06 * _env(len(t), 0.05, 3) + swell, 0.3)


def _fanfare() -> np.ndarray:
    t = _t(0.5)
    riser = np.sin(_phase(200 * 4 ** (t / 0.5))) * (t / 0.5) ** 2 * 0.5
    riser += _lowpass(np.random.default_rng(9).standard_normal(len(t)), 5000) * (t / 0.5) ** 3 * 0.3
    chord = sum(_brass(f, 1.0, vibrato=0.006, bright=1.1, decay=1.6) for f in (261.63, 329.63, 392.0, 523.25)) / 2.5
    hit = np.concatenate([_impact() * 0.8, np.zeros(max(0, len(chord) - len(_impact())))])[:len(chord)]
    return _space(np.concatenate([riser, chord + hit]), 0.3)


# name -> (mood, what it is, generator). mood_sting takes the FIRST word of a mood's STINGS list that any file
# matches, so both sounds of a mood share one word (and no earlier one), or one of them would never play.
SYNTHS: dict[str, tuple[str, str, Callable[[], np.ndarray]]] = {
    "shocked-dramatic-stab": (reactions.SHOCK, "three low brass stabs, the last held", _dun_dun),
    "shocked-deep-boom": (reactions.SHOCK, "a distorted sub boom with a click", _deep_boom),
    "funny-cartoon-boing": (reactions.LAUGH, "a cartoon spring boing", _boing),
    "funny-slide-whistle-up": (reactions.LAUGH, "a slide whistle going up", _slide_up),
    "fail-sad-trombone": (reactions.FAIL, "a descending sad trombone, wah wah wah waaah", _sad_trombone),
    "fail-whistle-down-thud": (reactions.FAIL, "a falling slide whistle and a thud", _slip_down),
    "confused-record-stop": (reactions.AWKWARD, "a record dragged back and forth, then stopped", _record_scratch),
    "confused-crickets": (reactions.AWKWARD, "crickets in the silence", _crickets),
    "wow-sparkle-rise": (reactions.MINDBLOWN, "a rising bell sparkle", _sparkle),
    "wow-legendary-fanfare": (reactions.HYPE, "a riser into a major brass chord", _fanfare),
}


def _write_wav(path: Path, samples: np.ndarray) -> None:
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    samples = samples * (_PEAK / peak) if peak > 0 and math.isfinite(peak) else samples
    fade = min(len(samples), int(RATE * 0.03))
    if fade:
        samples[-fade:] *= np.linspace(1.0, 0.0, fade)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthesise(folder: Path, moods: Optional[set[str]] = None) -> list[dict]:
    """Write the synthesised stings (only for ``moods`` when given). Returns their manifest entries."""
    entries = []
    for name, (mood, what, make) in SYNTHS.items():
        if moods is not None and mood not in moods:
            continue
        path = folder / "trending" / f"{name}.wav"
        _write_wav(path, make())
        entries.append({"file": path.relative_to(folder).as_posix(), "mood": mood, "description": what,
                        "license": CC0, "license_url": CC0_URL, "source": "synthesised",
                        "source_url": None, "author": "generated by scripts/fetch_sfx.py --cc0 (no samples)",
                        "sha256": _sha256(path)})
    return entries


# ---------------------------------------------------------------- Freesound

# Per mood: the searches, and the one STINGS word every file of that mood is named with (see SYNTHS).
FREESOUND_QUERIES: dict[str, tuple[tuple[str, ...], str]] = {
    reactions.SHOCK: (("dramatic boom", "dun dun dun"), "shocked"),
    reactions.LAUGH: (("cartoon boing", "slide whistle up"), "funny"),
    reactions.FAIL: (("sad trombone", "cartoon slip"), "fail"),
    reactions.AWKWARD: (("crickets", "record scratch"), "confused"),
    reactions.HYPE: (("fanfare", "hype riser"), "wow"),
    reactions.MINDBLOWN: (("magic sparkle", "wow"), "wow"),
}
_CC0_FILTER = 'license:"Creative Commons 0" duration:[0.2 TO 3.0]'


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "sound"


def from_freesound(folder: Path, per_query: int = 1) -> list[dict]:
    """CC0 sounds from Freesound for each mood query. Raises trending_sfx.SfxError when the API refuses."""
    from . import trending_sfx as fs
    from .sfx import sting

    entries = []
    for mood, (searches, word) in FREESOUND_QUERIES.items():
        for query in searches:
            body = fs._request(fs._SEARCH_URL, {"query": query, "sort": "score", "filter": _CC0_FILTER,
                                                "fields": fs._FIELDS, "group_by_pack": "1", "page_size": 10})
            got = 0
            for cand in fs._sfx_results(body.get("results")):  # drops anything not CC0 a second time
                if got >= per_query:
                    break
                number = cand.sfx_id.split("-")[-1]
                suffix = ".ogg" if cand.link.split("?")[0].lower().endswith(".ogg") else ".mp3"
                # Named by mood word and id only: words in a title could match another mood's stings.
                path = folder / "trending" / f"{word}-{mood}-freesound-{number}{suffix}"
                try:
                    fs._download(cand.link, path)
                except fs.SfxError as exc:
                    log.warning("cc0 sfx: %s", fs.scrub_secrets(exc))
                    continue
                if sting(str(path)) is None:
                    path.unlink(missing_ok=True)
                    continue
                got += 1
                entries.append({"file": path.relative_to(folder).as_posix(), "mood": mood, "description": cand.title,
                                "license": CC0, "license_url": CC0_URL, "source": "freesound",
                                "source_url": cand.page_url, "download_url": cand.link,
                                "author": cand.author, "freesound_id": number, "sha256": _sha256(path)})
    return entries


# ---------------------------------------------------------------- the pack

def build_pack(folder: Optional[Path] = None, *, source: str = "auto", per_query: int = 1) -> dict:
    """Build the pack from scratch into ``folder`` (default :func:`pack_root`), replacing what was there."""
    from . import trending_sfx as fs

    folder = Path(folder) if folder else pack_root()
    chosen = source if source in ("freesound", "synth") else ("freesound" if fs.provider() else "synth")
    staging = folder.with_name(f"{folder.name}.building.{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        entries = from_freesound(staging, per_query) if chosen == "freesound" else []
        missing = set(reactions.MOODS) - {e["mood"] for e in entries}
        if missing:
            entries += synthesise(staging, missing)
        manifest = {"pack": "doomscroller-cc0-sfx", "version": 1, "license": CC0, "license_url": CC0_URL,
                    "source": chosen, "rebuild": "python scripts/fetch_sfx.py --cc0", "sounds": entries}
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        (staging / "LICENSE.txt").write_text(
            "Every sound in this folder is CC0 1.0 (public domain dedication): " + CC0_URL + "\n"
            "Synthesised sounds were generated by scripts/fetch_sfx.py from code, with no samples.\n"
            "Freesound sounds were downloaded only when Freesound listed them as Creative Commons 0;\n"
            "MANIFEST.json names each file's source page and author.\n", encoding="utf-8")
        old = folder.with_name(f"{folder.name}.old.{os.getpid()}")
        if folder.exists():
            folder.replace(old)
        staging.replace(folder)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"ok": True, "folder": str(folder), "source": chosen, "sounds": len(entries),
            "moods": sorted({e["mood"] for e in entries})}


def ensure_pack(folder: Optional[Path] = None) -> Path:
    """The pack folder, synthesising it first if it has never been built."""
    folder = Path(folder) if folder else pack_root()
    if (folder / MANIFEST).is_file():
        return folder
    with _lock:
        if not (folder / MANIFEST).is_file():
            build_pack(folder, source="synth")
    return folder
