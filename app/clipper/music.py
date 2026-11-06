"""Background music from a folder the owner fills with tracks they may use.

Nothing is downloaded: music is the part of a Short most likely to draw a copyright claim,
so only tracks the owner chose go in. YouTube's own Audio Library is the safe source for a
monetised channel. A track that needs a credit gets a text file beside it with the same
name, and that line goes into the description.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from app.config import settings

_AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}


def folder() -> Path:
    return Path(settings.clip_music_dir) if settings.clip_music_dir else settings.content_path / "music"


def library() -> list[Path]:
    root = folder()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO)


def pick(seed: str) -> Optional[Path]:
    """A track for this clip: varied across clips, the same on a re-render."""
    tracks = library() if settings.clip_music else []
    if not tracks:
        return None
    index = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(tracks)
    return tracks[index]


def credit(track: Optional[Path]) -> str:
    if track is None:
        return ""
    note = track.with_suffix(".txt")
    try:
        return note.read_text(encoding="utf-8").strip()[:300] if note.is_file() else ""
    except OSError:
        return ""
