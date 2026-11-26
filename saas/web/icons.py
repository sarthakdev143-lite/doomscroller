"""Regenerate the favicon set in saas/web/static/icons from one drawing.

    .venv-saas/Scripts/python.exe -m saas.web.icons

The mark is brand-neutral on purpose (the product name is not final): an ink tile with the two
yellow marks the renderer burns into every clip, the progress bar along the top and a caption bar.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "static" / "icons"
INK, YELLOW, PAPER = "#111111", "#ffe500", "#f4f4f0"

SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="{INK}"/>
<rect x="0" y="0" width="30" height="6" fill="{YELLOW}"/>
<rect x="14" y="38" width="36" height="10" fill="{YELLOW}"/>
</svg>
"""


def draw(size: int, *, safe_zone: float = 1.0, background: str | None = None) -> Image.Image:
    """The tile at `size` px. safe_zone < 1 shrinks it onto `background` (maskable and Apple icons)."""
    scale = 8
    big = size * scale
    img = Image.new("RGBA", (big, big), background or (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    tile = round(big * safe_zone)
    off = (big - tile) // 2
    unit = tile / 64

    def box(x: float, y: float, w: float, h: float) -> tuple[int, int, int, int]:
        return (round(off + x * unit), round(off + y * unit), round(off + (x + w) * unit), round(off + (y + h) * unit))

    radius = 0 if background else round(14 * unit)
    d.rounded_rectangle(box(0, 0, 64, 64), radius=radius, fill=INK)
    # Clip the progress bar to the rounded corner by drawing it, then re-cutting the corner.
    d.rectangle(box(0, 0, 30, 6), fill=YELLOW)
    if radius:
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle(box(0, 0, 64, 64), radius=radius, fill=255)
        clear = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        img = Image.composite(img, clear, mask)
        d = ImageDraw.Draw(img)
    d.rectangle(box(14, 38, 36, 10), fill=YELLOW)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon.svg").write_text(SVG, encoding="utf-8")
    draw(48).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    draw(180, safe_zone=1.0, background=INK).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    draw(192).save(OUT / "icon-192.png", optimize=True)
    draw(512).save(OUT / "icon-512.png", optimize=True)
    # Maskable icons are cropped to a circle by some launchers: keep the mark inside the middle 80%.
    draw(512, safe_zone=0.72, background=INK).convert("RGB").save(OUT / "icon-maskable-512.png", optimize=True)
    print(f"wrote icons to {OUT}")


if __name__ == "__main__":
    main()
