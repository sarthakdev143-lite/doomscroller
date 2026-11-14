"""Sound design, synthesised rather than sampled, so there is nothing to license.

Effects are mixed well below the voice: they should be felt on the cuts rather than
noticed as sounds.
"""
from __future__ import annotations

import math
import wave
from dataclasses import dataclass
from typing import Sequence

import numpy as np

RATE = 48000
WHOOSH, IMPACT, POP = "whoosh", "impact", "pop"
_GAIN = {WHOOSH: 0.05, IMPACT: 0.22, POP: 0.10}


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


def build_track(path: str, duration: float, cues: Sequence[Cue]) -> None:
    """Write a mono WAV holding every cue at its time."""
    safe_duration = duration if math.isfinite(duration) and duration > 0 else 0.0
    rng = np.random.default_rng(7)
    track = np.zeros(max(1, int(RATE * (safe_duration + 1.0))), dtype=np.float64)
    makers = {WHOOSH: lambda: _whoosh(rng), IMPACT: _impact, POP: _pop}
    for cue in cues:
        if cue.sound not in makers or not math.isfinite(cue.at):
            continue
        sound = makers[cue.sound]() * _GAIN[cue.sound]
        start = max(0, int(cue.at * RATE))
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
