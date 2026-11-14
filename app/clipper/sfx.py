"""Sound design, synthesised rather than sampled, so there is nothing to license.

Effects are mixed well below the voice: they should be felt on the cuts rather than
noticed as sounds. An owner-curated pack in ``content/sfx/`` (one shot per
file, e.g. ``whoosh.wav``) overrides the synthesis per cue kind; without it the
synth below plays, so clips never depend on downloaded files.
"""
from __future__ import annotations

import hashlib
import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from app.config import settings

RATE = 48000
WHOOSH, IMPACT, POP = "whoosh", "impact", "pop"
_GAIN = {WHOOSH: 0.05, IMPACT: 0.22, POP: 0.10}
# A pack file whose stem names one of these replaces the synth for that cue kind.
_KIND_ALIASES = {WHOOSH: ("whoosh", "sweep", "riser"), IMPACT: ("impact", "boom", "thud", "hit"),
                 POP: ("pop", "click", "snap", "blip")}
_AUDIO = {".wav", ".mp3", ".ogg", ".opus", ".flac"}
# A downloaded one-shot never holds the mix longer than this.
_PACK_SLICE_SEC = 1.2


@dataclass(frozen=True)
class Cue:
    at: float
    sound: str


def _whoosh(rng: np.random.Generator) -> np.ndarray:
    count = int(RATE * 0.28)
    noise = rng.normal(0, 1, count)
    # A sweep of moving averages: long window (dull) to short (bright) and back.
    t = np.linspace(0, 1, count)
    smooth = np.convolve(noise, np.ones(24) / 24, mode="same")
    bright = t * noise * 0.3 + (1 - t) * smooth
    return bright * np.sin(np.pi * t) ** 2


def _impact() -> np.ndarray:
    count = int(RATE * 0.45)
    t = np.arange(count) / RATE
    frequency = 40 + 70 * np.exp(-t * 28)
    return np.sin(2 * np.pi * np.cumsum(frequency) / RATE) * np.exp(-t * 9)


def _pop() -> np.ndarray:
    count = int(RATE * 0.07)
    t = np.arange(count) / RATE
    return np.sin(2 * np.pi * (700 - 3500 * t) * t) * np.exp(-t * 55)


def pack_dir() -> Path:
    configured = (settings.clip_sfx_pack_dir or "").strip()
    return Path(configured).expanduser() if configured else settings.content_path / "sfx"


def _usable_pack_file(path: Path) -> bool:
    if path.name.startswith(".") or path.suffix.lower() not in _AUDIO:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def pack_pick(kind: str, seed: str) -> Optional[Path]:
    """A pack file for this cue kind, or None when the pack has nothing usable.

    Files whose stem names the kind (or an alias) win; otherwise the pick rotates
    through the whole pack. Deterministic per seed, so a re-render sounds the same.
    """
    root = pack_dir()
    if not root.is_dir():
        return None
    try:
        files = sorted(p for p in root.iterdir() if _usable_pack_file(p))
    except OSError:
        return None
    if not files:
        return None
    aliases = _KIND_ALIASES.get(kind, ())
    named = [p for p in files if any(a in p.stem.lower() for a in aliases)]
    pool = named or files
    index = int(hashlib.sha1(f"{seed}|{kind}".encode()).hexdigest(), 16) % len(pool)
    return pool[index]


def _decode_pack(path: Path) -> Optional[np.ndarray]:
    """Mono float64 samples at RATE for a pack file, else None. Never raises."""
    try:
        if path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as handle:
                raw = handle.readframes(handle.getnframes())
                width, rate, channels = handle.getsampwidth(), handle.getframerate(), handle.getnchannels()
            if width != 2 or rate <= 0:
                return None
            mono = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
            if channels > 1:
                mono = mono.reshape(-1, channels).mean(axis=1)
            if rate != RATE and len(mono):
                # Nearest-neighbour resample: one-shots, not music.
                mono = mono[np.minimum(len(mono) - 1, (np.arange(int(len(mono) * RATE / rate)) * rate / RATE).astype(int))]
            return mono
        from .media import read_ffmpeg
        raw = read_ffmpeg(["-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
                           "-t", f"{_PACK_SLICE_SEC}", "-f", "f32le", "-"])
        return np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    except Exception:
        return None


def build_track(path: str, duration: float, cues: Sequence[Cue], seed: str = "") -> None:
    """Write a mono WAV holding every cue at its time."""
    safe_duration = duration if math.isfinite(duration) and duration > 0 else 0.0
    rng = np.random.default_rng(7)
    track = np.zeros(max(1, int(RATE * (safe_duration + 1.0))), dtype=np.float64)
    makers = {WHOOSH: lambda: _whoosh(rng), IMPACT: _impact, POP: _pop}
    pack = {kind: pack_pick(kind, seed) for kind in makers}
    for cue in cues:
        if cue.sound not in makers or not math.isfinite(cue.at):
            continue
        start = max(0, int(cue.at * RATE))
        if start >= len(track):
            continue
        clip = _decode_pack(pack[cue.sound]) if pack[cue.sound] is not None else None
        if clip is not None and len(clip):
            peak = float(np.max(np.abs(clip)))
            sound = clip[:int(RATE * _PACK_SLICE_SEC)] * (0.25 / peak if peak > 0 else 0.0)
        else:
            sound = makers[cue.sound]() * _GAIN[cue.sound]
        end = min(len(track), start + len(sound))
        if end > start:
            track[start:end] += sound[:end - start]
    clean = np.nan_to_num(track, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = np.clip(np.rint(np.clip(clean, -1.0, 1.0) * 32767.0), -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())
