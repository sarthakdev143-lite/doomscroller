"""Locating a usable font for ffmpeg's ``drawtext`` filter.

``TRANSFORM_CAPTION_FONT`` historically defaulted to a Debian DejaVu path, which
does not exist on Windows or macOS. A missing font makes ffmpeg fail the whole
render, so the configured value is validated and a bold system font is found
instead when it does not resolve.

Bold faces are preferred: burned-in captions sit over arbitrary video and need
the weight to stay legible.
"""
from __future__ import annotations

import logging
import os
import platform
from functools import lru_cache

log = logging.getLogger(__name__)

# Bold-ish faces, best first, per platform.
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Windows": (
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\ariblk.ttf",
        r"C:\Windows\Fonts\impact.ttf",
        r"C:\Windows\Fonts\tahomabd.ttf",
        r"C:\Windows\Fonts\verdanab.ttf",
        r"C:\Windows\Fonts\calibrib.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ),
    "Darwin": (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ),
    "Linux": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/system/fonts/DroidSans-Bold.ttf",       # Android / Termux
        "/system/fonts/Roboto-Bold.ttf",
        "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
}

# Devanagari (Hindi) needs its own font: the Latin faces above draw empty boxes for it. Bold first.
# Install one with e.g. `apt install fonts-noto-devanagari` (Debian) and it is picked up automatically.
_DEVANAGARI_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Windows": (r"C:\Windows\Fonts\Nirmala.ttf", r"C:\Windows\Fonts\mangal.ttf"),
    "Darwin": ("/System/Library/Fonts/Supplemental/Devanagari Sangam MN.ttc",
               "/Library/Fonts/Kohinoor.ttc"),
    "Linux": ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
              "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
              "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
              "/system/fonts/NotoSansDevanagari-Bold.ttf",
              "/system/fonts/NotoSansDevanagariUI-Bold.ttf"),
}
_DEVANAGARI_NAME_HINTS = ("devanagari", "mangal", "nirmala", "lohit-devanagari", "kohinoor")
_DEVANAGARI_RANGE = (0x0900, 0x097F)


def _font_roots() -> tuple[str, ...]:
    roots = [
        r"C:\Windows\Fonts",
        "/usr/share/fonts",
        "/system/fonts",
        "/Library/Fonts",
        "/System/Library/Fonts",
        os.path.expanduser("~/.fonts"),
    ]
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        roots.insert(1, os.path.join(local_app, "Microsoft", "Windows", "Fonts"))
    return tuple(roots)


_FONT_ROOTS = _font_roots()


def dominant_script(text: str) -> str:
    """'devanagari' when most of the letters are Devanagari, else 'default'. Used to pick a font."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return "default"
    deva = sum(1 for c in letters if _DEVANAGARI_RANGE[0] <= ord(c) <= _DEVANAGARI_RANGE[1])
    return "devanagari" if deva / len(letters) >= 0.5 else "default"


@lru_cache(maxsize=1)
def devanagari_font() -> str | None:
    """A Devanagari-capable font on this machine, or None."""
    for path in _DEVANAGARI_CANDIDATES.get(platform.system(), ()):
        if os.path.isfile(path):
            return path
    for root in _FONT_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, filenames in os.walk(root):
            for name in sorted(filenames):
                low = name.lower()
                if low.endswith((".ttf", ".otf", ".ttc")) and any(h in low for h in _DEVANAGARI_NAME_HINTS):
                    return os.path.join(dirpath, name)
    return None


def has_devanagari_font() -> bool:
    return devanagari_font() is not None


@lru_cache(maxsize=1)
def _discovered_font() -> str | None:
    """First existing candidate for this platform, else any font we can find."""
    system = platform.system()
    for path in _CANDIDATES.get(system, ()):
        if os.path.isfile(path):
            return path
    # Fall back to scanning the usual directories for anything usable.
    for root in _font_roots():
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if name.lower().endswith((".ttf", ".otf", ".ttc")):
                    return os.path.join(dirpath, name)
    return None


def resolve_font(configured: str = "", text: str = "") -> str | None:
    """Return a usable font file path, or ``None`` if there is none.

    ``None`` means callers must skip text rendering rather than hand ffmpeg a
    path that does not exist. When ``text`` is predominantly Devanagari and a
    Devanagari font exists, that font is returned so Hindi overlays actually draw.
    """
    if text and dominant_script(text) == "devanagari":
        deva = devanagari_font()
        if deva is not None:
            return deva
        # No Devanagari font: fall through to the Latin font (the caller's is_renderable check
        # will have already decided whether to draw this text at all).
    candidate = (configured or "").replace("\x00", "").strip().strip('"').strip("'").strip()
    if candidate and os.path.isfile(candidate):
        return candidate
    if candidate:
        log.warning(
            "configured font %r not found; falling back to a system font", candidate
        )
    found = _discovered_font()
    if found is None:
        log.warning("no usable font found; captions and watermarks are disabled")
    return found


def ffmpeg_path(path: str) -> str:
    r"""Escape a path for use inside an ffmpeg filter expression.

    ffmpeg parses filter graphs itself, so on Windows both the backslashes and
    the drive-letter colon have to be escaped or ``C:\Windows`` reads as a new
    filter argument. Single quotes are also escaped so paths such as
    ``C:\Users\O'Connor\...`` remain valid inside ``drawtext=fontfile='...'``.
    """
    clean = "".join(ch for ch in (path or "") if ch >= " " and ch != "\x7f")
    escaped = clean.replace("\\", "/").replace(":", r"\:")
    return escaped.replace("'", r"'\''")


# Historical name; the function escapes any path, not just a font.
ffmpeg_font_path = ffmpeg_path
