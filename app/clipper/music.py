"""Background music from a folder the owner fills with tracks they may use.

Nothing is downloaded: music is the part of a Short most likely to draw a copyright claim,
so only tracks the owner chose go in. YouTube's own Audio Library is the safe source for a
monetised channel. A track that needs a credit gets a text file beside it with the same
name, and that line goes into the description.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional, Union

from app.config import settings

_AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
_UNSAFE_CREDIT_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f<>]")


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


def pick(seed: str) -> Optional[Path]:
    """A track for this clip: varied across clips, the same on a re-render."""
    tracks = library() if settings.clip_music else []
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
