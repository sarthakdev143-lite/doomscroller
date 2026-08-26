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


@lru_cache(maxsize=1)
def _discovered_font() -> str | None:
    """First existing candidate for this platform, else any font we can find."""
    system = platform.system()
    for path in _CANDIDATES.get(system, ()):
        if os.path.isfile(path):
            return path
    # Fall back to scanning the usual directories for anything usable.
    roots = [
        r"C:\Windows\Fonts", "/usr/share/fonts", "/system/fonts",
        "/Library/Fonts", "/System/Library/Fonts",
        os.path.expanduser("~/.fonts"),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if name.lower().endswith((".ttf", ".otf")):
                    return os.path.join(dirpath, name)
    return None


def resolve_font(configured: str = "") -> str | None:
    """Return a usable font file path, or ``None`` if there is none.

    ``None`` means callers must skip text rendering rather than hand ffmpeg a
    path that does not exist.
    """
    candidate = (configured or "").strip()
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
    filter argument.
    """
    escaped = path.replace("\\", "/")
    return escaped.replace(":", r"\:")


# Historical name; the function escapes any path, not just a font.
ffmpeg_font_path = ffmpeg_path
