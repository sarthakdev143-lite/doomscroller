"""Background music from a folder the owner fills with tracks they may use.

Nothing is downloaded: music is the part of a Short most likely to draw a copyright claim,
so only tracks the owner chose go in. YouTube's own Audio Library is the safe source for a
monetised channel. A track that needs a credit gets a text file beside it with the same
name, and that line goes into the description.
"""
from __future__ import annotations

import hashlib
import re
import wave
from pathlib import Path
from typing import Optional, Union

from app.config import settings

_AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
_UNSAFE_CREDIT_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f<>]")
_SYNTH_RATE = 48000
_SYNTH_SPECS = (
    ("bed_warm.wav", 110.0, (110.0, 164.81, 196.0), 16.0),
    ("bed_pulse.wav", 98.0, (98.0, 146.83, 174.61), 16.0),
    ("bed_chill.wav", 130.81, (130.81, 196.0, 246.94), 16.0),
)


def folder() -> Path:
    configured = (settings.clip_music_dir or "").strip()
    return Path(configured).expanduser() if configured else settings.content_path / "music"


def _usable_track(path: Path) -> bool:
    if path.name.startswith(".") or path.suffix.lower() not in _AUDIO:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def library() -> list[Path]:
    root = folder()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if _usable_track(p))


def _write_synth_bed(path: Path, bass_hz: float, chord_hz: tuple[float, ...], seconds: float) -> None:
    """Synthesize a warm, loopable stereo ambient groove bed without external assets."""
    import numpy as np

    n = int(_SYNTH_RATE * seconds)
    t = np.arange(n, dtype=np.float64) / _SYNTH_RATE
    # Gentle bass pulse + stereo chord pads (detuned L/R for genuine stereo width).
    pulse = 0.55 + 0.45 * np.sin(2.0 * np.pi * (t * 2.0))
    bass = 0.35 * np.sin(2.0 * np.pi * bass_hz * t) * pulse
    left = bass.copy()
    right = bass.copy()
    for idx, freq in enumerate(chord_hz):
        detune = 0.45 if idx % 2 == 0 else -0.45
        env = 0.75 + 0.25 * np.sin(2.0 * np.pi * (t / 4.0 + idx * 0.25))
        left += 0.14 * env * np.sin(2.0 * np.pi * (freq + detune) * t + 0.3 * idx)
        right += 0.14 * env * np.sin(2.0 * np.pi * (freq - detune) * t + 1.1 * idx)
    # Seamless loop cross-envelope on first/last 0.1s.
    edge = min(n // 4, int(_SYNTH_RATE * 0.1))
    ramp = np.linspace(0.0, 1.0, edge)
    left[:edge] *= ramp
    right[:edge] *= ramp
    left[-edge:] *= ramp[::-1]
    right[-edge:] *= ramp[::-1]
    stereo = np.stack([left, right], axis=1)
    pcm = np.clip(np.rint(np.clip(stereo, -0.95, 0.95) * 32767.0), -32768, 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(_SYNTH_RATE)
        handle.writeframes(pcm.tobytes())
    tmp.replace(path)


def _synth_library() -> list[Path]:
    synth_dir = folder() / "_synth"
    beds: list[Path] = []
    for name, bass, chord, secs in _SYNTH_SPECS:
        target = synth_dir / name
        if not _usable_track(target):
            try:
                _write_synth_bed(target, bass, chord, secs)
            except OSError:
                continue
        if _usable_track(target):
            beds.append(target)
    return beds


def pick(seed: str) -> Optional[Path]:
    """A track for this clip: owner tracks first, else zero-copyright stereo synth beds."""
    if not settings.clip_music:
        return None
    tracks = library()
    if not tracks and settings.clip_music_synth_fallback:
        tracks = _synth_library()
    if not tracks:
        return None
    index = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(tracks)
    return tracks[index]


def credit(track: Optional[Union[Path, str]]) -> str:
    if not track:
        return ""
    path = Path(track).expanduser()
    candidates = []
    if path.suffix:
        candidates.append(path.with_suffix(".txt"))
    candidates.append(path.with_name(f"{path.name}.txt"))
    for note in candidates:
        try:
            if not note.is_file():
                continue
            raw = note.read_text(encoding="utf-8-sig", errors="replace")
            cleaned = re.sub(r"[\s\u2028\u2029]+", " ", raw)
            cleaned = _UNSAFE_CREDIT_CHARS.sub("", cleaned).strip()
            return cleaned[:300]
        except OSError:
            continue
    return ""
