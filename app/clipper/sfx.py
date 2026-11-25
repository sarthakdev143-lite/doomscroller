"""Sound design, synthesised rather than sampled, so there is nothing to license.

Effects are mixed well below the voice: they should be felt on the cuts rather than
noticed as sounds. An owner-curated pack in ``content/sfx/`` (one shot per
file, e.g. ``whoosh.wav``) overrides the synthesis per cue kind; without it the
synth below plays, so clips never depend on downloaded files. Meme sounds the AI
may play after a punchline live in ``content/sfx/trending/``, named for what they mean.
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
# A meme sound is the joke itself, so it plays at voice level, and is cut short before it drags.
_STING_MAX_SEC = 2.5
_STING_PEAK = 0.9
_STING_FADE_SEC = 0.25


@dataclass(frozen=True)
class Cue:
    at: float
    sound: str


def _whoosh(rng: np.random.Generator) -> np.ndarray:
    count = int(RATE * 0.24)
    noise = rng.normal(0, 1, count)
    t = np.linspace(0, 1, count)
    smooth = np.convolve(noise, np.ones(18) / 18, mode="same")
    bright = (0.15 + 0.55 * t) * noise + (0.85 - 0.55 * t) * smooth
    return bright * (np.sin(np.pi * t) ** 1.6)


def _impact() -> np.ndarray:
    count = int(RATE * 0.42)
    t = np.arange(count) / RATE
    frequency = 42 + 88 * np.exp(-t * 32)
    sub = np.sin(2 * np.pi * np.cumsum(frequency) / RATE)
    click = np.exp(-t * 140) * np.sin(2 * np.pi * 220 * t)
    return (sub * 0.9 + click * 0.35) * np.exp(-t * 8.5)


def _pop() -> np.ndarray:
    count = int(RATE * 0.065)
    t = np.arange(count) / RATE
    return np.sin(2 * np.pi * (820 - 4200 * t) * t) * np.exp(-t * 60)


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


def _iter_usable(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    try:
        return sorted(p for p in directory.iterdir() if _usable_pack_file(p))
    except OSError:
        return []


def _norm_lang(language: str) -> str:
    code = str(language or "").strip().lower()
    if not code:
        return ""
    return "hi" if code.startswith("hi") else "en"


def pack_pick(kind: str, seed: str, *, language: str = "") -> Optional[Path]:
    """A pack file for this cue kind, or None when the pack has nothing usable.

    Files whose stem names the kind (or an alias) win; otherwise the pick rotates
    through the matching language folder (never mixing ``hi/`` into English).
    Deterministic per seed, so a re-render sounds the same.
    """
    root = pack_dir()
    if not root.is_dir():
        return None
    lang = _norm_lang(language)
    aliases = _KIND_ALIASES.get(kind, ())

    search_dirs = [root] if not lang else [root / lang, root]
    for folder in search_dirs:
        files = _iter_usable(folder)
        if not files:
            continue
        named = [p for p in files if any(a in p.stem.lower() for a in aliases)]
        if named:
            index = int(hashlib.sha1(f"{seed}|{kind}".encode()).hexdigest(), 16) % len(named)
            return named[index]
    return None


def trending(*, language: str = "") -> dict[str, Path]:
    """The owner's meme sounds by file name, scoped by language when specified."""
    root = pack_dir()
    lang = _norm_lang(language)
    if not lang:
        return {p.stem: p for p in _iter_usable(root / "trending")}
    if lang == "hi":
        # Merge trending + hi/ with hi/ taking precedence on duplicate stems.
        merged = {p.stem: p for p in _iter_usable(root / "trending")}
        for p in _iter_usable(root / "hi"):
            merged[p.stem] = p
        return dict(sorted(merged.items()))
    # English (or non-Hindi): prefer en/, then non-Hindi root/trending files (never hi/).
    en_files = {p.stem: p for p in _iter_usable(root / "en")}
    if en_files:
        return en_files
    hi_stems = {p.stem for p in _iter_usable(root / "hi")}
    fallback = {p.stem: p for p in _iter_usable(root / "trending") if p.stem not in hi_stems}
    if not fallback:
        fallback = {p.stem: p for p in _iter_usable(root) if p.stem not in hi_stems}
    return fallback


def sting(path: str) -> Optional[np.ndarray]:
    """A meme sound ready to mix: leading silence trimmed, capped and faded, at a fixed peak. None if unreadable."""
    try:
        from .media import Interrupted, read_ffmpeg
        raw = read_ffmpeg(["-i", str(path), "-map", "0:a:0", "-af", "silenceremove=start_periods=1:start_threshold=-50dB",
                           "-ac", "1", "-ar", str(RATE), "-t", f"{_STING_MAX_SEC}", "-f", "f32le", "-"])
        sound = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    except Interrupted:
        raise
    except Exception:
        return None
    peak = float(np.max(np.abs(sound))) if len(sound) else 0.0
    if not math.isfinite(peak) or peak <= 0:
        return None
    sound = sound * (_STING_PEAK / peak)
    fade = min(len(sound), int(RATE * _STING_FADE_SEC))
    sound[len(sound) - fade:] *= np.linspace(1.0, 0.0, fade)
    return sound


def _decode_pack(path: Path) -> Optional[np.ndarray]:
    """Mono float64 samples at RATE for a pack file, else None. Never raises."""
    from .media import Interrupted, read_ffmpeg

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
        raw = read_ffmpeg(["-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
                           "-t", f"{_PACK_SLICE_SEC}", "-f", "f32le", "-"])
        return np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    except Interrupted:
        raise
    except Exception:
        return None


def build_track(path: str, duration: float, cues: Sequence[Cue], seed: str = "", *, language: str = "") -> None:
    """Write a mono WAV holding every cue at its time."""
    safe_duration = duration if math.isfinite(duration) and duration > 0 else 0.0
    rng = np.random.default_rng(7)
    track = np.zeros(max(1, int(RATE * (safe_duration + 1.0))), dtype=np.float64)
    makers = {WHOOSH: lambda: _whoosh(rng), IMPACT: _impact, POP: _pop}
    pack = {kind: pack_pick(kind, seed, language=language) for kind in makers}
    for cue in cues:
        if not math.isfinite(cue.at):
            continue
        start = max(0, int(cue.at * RATE))
        if start >= len(track):
            continue
        if cue.sound not in makers:
            # Anything else is a meme sound's file.
            sound = sting(cue.sound) if Path(cue.sound).is_file() else None
            if sound is None:
                continue
        else:
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
