"""Source cropping geometry.

Spotlight clips carry Snapchat's attribution -- ghost logo, "Snapchat", and the
original creator's handle -- burned into the pixels at the top-left, inside
roughly the top quarter of the frame. Removing it means cropping a band off the
top, which changes the aspect ratio and therefore has to be reasoned about
together with the framing step rather than bolted on before it.

Cropping a 540x960 (9:16) source by 25% leaves 540x720 (3:4). Two ways back to
the Shorts canvas:

``fill``
    Centre-crop the width until the ratio is 9:16 again, then upscale. No bars,
    full bleed -- which is what a Short should look like -- at the cost of side
    content and a harder upscale.
``fit``
    Keep the full cropped frame and let the blurred-background filler make up
    the canvas. Preserves everything that survived the band crop, but a Short
    with a visible pillarbox reads as recycled.

None of this makes the content original. It removes the platform's visible
attribution, which is why the description credit becomes mandatory whenever a
crop is active -- see ``metadata.compose``.
"""
from __future__ import annotations

from dataclasses import dataclass

# The Shorts canvas ratio.
TARGET_RATIO = 9 / 16
# Ratios within this tolerance count as "already 9:16".
RATIO_TOLERANCE = 0.03
# Refuse to crop away more than this share of the frame.
MAX_TOTAL_CROP = 0.45


def _even(value: float) -> int:
    """Round down to an even integer; libx264 requires even dimensions."""
    return max(2, int(value) // 2 * 2)


@dataclass
class CropPlan:
    """How to get from a source frame to something ready for the canvas."""

    filters: list[str]
    out_width: int
    out_height: int
    cropped_top: float = 0.0     # fraction of original height removed
    cropped_bottom: float = 0.0
    cropped_sides: float = 0.0   # fraction of original width removed
    mode: str = "none"
    keep: str = "center"

    @property
    def active(self) -> bool:
        return bool(self.filters)

    @property
    def ratio(self) -> float:
        return self.out_width / self.out_height if self.out_height else 0.0

    @property
    def matches_canvas(self) -> bool:
        return abs(self.ratio - TARGET_RATIO) < RATIO_TOLERANCE

    @property
    def prefix(self) -> str:
        """Filters to run before the canvas scale, with a trailing comma."""
        return (",".join(self.filters) + ",") if self.filters else ""

    def summary(self) -> str:
        if not self.active:
            return "no crop"
        bits = [f"{self.mode}"]
        if self.cropped_top:
            bits.append(f"top -{self.cropped_top * 100:.0f}%")
        if self.cropped_bottom:
            bits.append(f"bottom -{self.cropped_bottom * 100:.0f}%")
        if self.cropped_sides:
            bits.append(f"{self.keep}-keep -{self.cropped_sides * 100:.0f}% width")
        bits.append(f"{self.out_width}x{self.out_height}")
        return " ".join(bits)


def plan_crop(width: int, height: int, *, top_pct: float = 0.0,
              bottom_pct: float = 0.0, mode: str = "fill",
              keep: str = "center") -> CropPlan:
    """Build the crop chain for a source of ``width`` x ``height``.

    ``top_pct`` / ``bottom_pct`` are percentages of the *original* height.

    ``keep`` decides which part of the width survives the ``fill`` re-crop.
    That width is discarded either way to restore 9:16, so choosing *where* it
    comes from is free: Snapchat stacks its attribution against the left edge,
    so ``keep="right"`` spends the same pixels removing the remnant that a
    centred crop would leave behind.
    """
    if width <= 0 or height <= 0:
        return CropPlan(filters=[], out_width=width, out_height=height)

    top = max(0.0, min(45.0, float(top_pct))) / 100.0
    bottom = max(0.0, min(45.0, float(bottom_pct))) / 100.0
    if top + bottom > MAX_TOTAL_CROP:
        # Refuse rather than silently gutting the frame.
        return CropPlan(filters=[], out_width=width, out_height=height,
                        mode="rejected: crop too large")
    if top <= 0 and bottom <= 0:
        return CropPlan(filters=[], out_width=width, out_height=height)

    keep_h = _even(height * (1.0 - top - bottom))
    offset_y = _even(height * top)
    if keep_h < 120:
        return CropPlan(filters=[], out_width=width, out_height=height,
                        mode="rejected: result too small")

    filters = [f"crop={width}:{keep_h}:0:{offset_y}"]
    out_w, out_h = width, keep_h
    sides = 0.0

    if mode == "fill" and (out_w / out_h) > TARGET_RATIO + RATIO_TOLERANCE:
        # Too wide now: centre-crop the width back to 9:16 so the canvas fills
        # edge to edge instead of pillarboxing.
        fill_w = _even(out_h * TARGET_RATIO)
        if fill_w >= 120:
            spare = out_w - fill_w
            if keep == "right":
                offset_x = _even(spare)        # drop the left edge
            elif keep == "left":
                offset_x = 0                   # drop the right edge
            else:
                offset_x = _even(spare / 2)    # centred
            filters.append(f"crop={fill_w}:{out_h}:{offset_x}:0")
            sides = spare / out_w
            out_w = fill_w

    return CropPlan(
        filters=filters, out_width=out_w, out_height=out_h,
        cropped_top=top, cropped_bottom=bottom, cropped_sides=sides,
        mode=mode, keep=keep,
    )


def upscale_factor(out_width: int, canvas_width: int) -> float:
    """How hard the canvas scale has to stretch what the crop left behind."""
    return (canvas_width / out_width) if out_width else 0.0
