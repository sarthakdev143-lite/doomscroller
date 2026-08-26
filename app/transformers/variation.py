"""Per-video output variation.

YouTube's reused-content policy is the main risk to an automated republishing
channel: uploading someone else's video with only a credit line added is exactly
what it targets, and at several uploads a day a mechanical pattern is easy to
spot. This module decides, per video, *how* a render should differ.

Two properties matter:

**Genuine difference.** Each output is reframed, retimed, captioned and branded
rather than re-encoded verbatim.

**Non-uniformity.** If every upload got the same treatment, the channel would
just have a different mechanical fingerprint. The parameters are therefore
drawn from ranges.

The draw is seeded from the video id, so it is *deterministic* -- re-rendering a
video produces the same output, which keeps retries idempotent and makes a
surprising result reproducible when debugging.

Deliberately absent: an intro bumper. On Shorts the opening moment decides
whether a viewer stays, so spending it on branding costs more retention than
the distinctiveness is worth. The end card is *overlaid* over the closing
seconds instead of appended, so it never lengthens the video either.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# Corners available to the watermark, as (x, y) ffmpeg expressions.
#
# Spotlight sources arrive with Snapchat's own attribution burned into the top
# and bottom of the frame (ghost logo, "Snapchat", the creator handle). These
# positions deliberately sit inside the safe middle band so our watermark never
# lands on top of theirs, which looked broken when it did.
_WATERMARK_POSITIONS = {
    "upper_right": ("w-tw-36", "h*0.24"),
    "mid_right": ("w-tw-36", "h*0.44"),
    "mid_left": ("36", "h*0.44"),
    "lower_right": ("w-tw-36", "h*0.56"),
}


@dataclass
class VariationPlan:
    """How one specific video should be differentiated."""

    zoom: float = 1.0
    speed: float = 1.0
    mirror: bool = False
    watermark_position: str = "mid_right"
    caption_text: str = ""
    end_card_text: str = ""
    brand_text: str = ""
    sharpen: bool = False
    seed: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def changes_timing(self) -> bool:
        return abs(self.speed - 1.0) > 0.001

    @property
    def changes_framing(self) -> bool:
        return self.zoom > 1.001 or self.mirror

    def summary(self) -> str:
        bits = [f"zoom={self.zoom:.3f}", f"speed={self.speed:.3f}"]
        if self.mirror:
            bits.append("mirrored")
        if self.caption_text:
            bits.append("caption")
        if self.brand_text:
            bits.append(f"brand@{self.watermark_position}")
        if self.end_card_text:
            bits.append("endcard")
        if self.sharpen:
            bits.append("sharpen")
        return " ".join(bits)


def _rng(seed: str):
    """A Random seeded stably from a string (``hash()`` is salted per process)."""
    import random

    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def wrap_caption(text: str, max_chars_per_line: int = 24,
                 max_lines: int = 2) -> str:
    """Wrap a caption onto at most ``max_lines`` lines.

    A single long line overflows the 1080px canvas and gets clipped at both
    edges. Short stacked lines also simply read better on a phone screen.
    """
    words = (text or "").split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars_per_line or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    lines = lines[:max_lines]
    # A truncated caption should say so rather than stopping mid-thought.
    consumed = len(" ".join(lines).split())
    if consumed < len(words) and lines:
        tail = lines[-1]
        if len(tail) + 1 <= max_chars_per_line:
            lines[-1] = tail + "..."
        else:
            lines[-1] = tail[: max_chars_per_line - 3].rstrip() + "..."
    return "\n".join(lines)


def caption_from(title: str, keywords: list[str] | None, max_chars: int) -> str:
    """Pick a short hook to burn over the opening seconds.

    The title is preferred, trimmed at a word boundary. Snapchat titles often
    carry a ``A | B`` structure whose first half is the actual hook.
    """
    text = " ".join((title or "").split())
    for separator in (" | ", " - ", ": "):
        if separator in text and len(text) > max_chars:
            head = text.split(separator)[0].strip()
            if head:
                text = head
                break
    if not text and keywords:
        text = " ".join(str(k) for k in keywords[:3])
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].strip() or text[:max_chars].strip()
    return text


def plan_variation(
    *,
    video_id: str,
    title: str = "",
    keywords: list[str] | None = None,
    settings=None,
) -> VariationPlan:
    """Build the variation plan for one video."""
    if settings is None:
        from ..config import settings as default_settings

        settings = default_settings

    plan = VariationPlan(seed=video_id)

    if not settings.transform_variation:
        plan.notes.append("variation disabled")
        return plan

    rng = _rng(video_id or "seed")

    low, high = float(settings.zoom_min), float(settings.zoom_max)
    if high > low:
        plan.zoom = round(rng.uniform(low, high), 4)
    elif low > 1.0:
        plan.zoom = round(low, 4)

    low, high = float(settings.speed_min), float(settings.speed_max)
    if high > low:
        plan.speed = round(rng.uniform(low, high), 4)

    # Mirroring is a strong visual difference but wrong for anything with text
    # or writing in frame, so it stays a minority treatment.
    plan.mirror = rng.random() < 0.15

    plan.watermark_position = rng.choice(sorted(_WATERMARK_POSITIONS))
    plan.brand_text = (settings.brand_name or "").strip()

    if settings.caption_enabled:
        plan.caption_text = caption_from(
            title, keywords, int(settings.caption_max_chars)
        )

    if settings.end_card_enabled:
        plan.end_card_text = (settings.end_card_text or "").strip()

    plan.sharpen = bool(settings.upscale_sharpen)
    return plan


def watermark_position(name: str) -> tuple[str, str]:
    """Resolve a watermark position name to ffmpeg (x, y) expressions."""
    return _WATERMARK_POSITIONS.get(name, _WATERMARK_POSITIONS["mid_right"])
