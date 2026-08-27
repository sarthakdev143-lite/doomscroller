"""FFmpeg vertical transformer.

Renders a source video onto the 1080x1920 Shorts canvas in a single pass, and
optionally applies a :class:`~app.transformers.variation.VariationPlan` so the
output is genuinely differentiated from the source rather than a re-encode.

Filter chain, in order:

1.  **Framing** -- either a blurred-background fill (for non-9:16 sources) or a
    straight scale-to-canvas. Lanczos, because most Spotlight sources are
    540x960 and get upscaled.
2.  **Zoom** -- scale past the canvas and centre-crop back. A static crop rather
    than ``zoompan``, which is costly and not always compiled in.
3.  **Mirror** / **sharpen**.
4.  **Retime** -- ``setpts`` on video *and* ``atempo`` on audio. Changing one
    without the other desynchronises them.
5.  **Text** -- hook caption over the opening seconds, brand watermark
    throughout, subscribe end card over the closing seconds. All time-gated with
    ``enable=between(t,..)``, so none of them adds duration.

Text is passed to ``drawtext`` via ``textfile=`` with ``expansion=none``, never
as an inline ``text=`` value. Inline text has to survive ffmpeg's own filtergraph
parser, and a single apostrophe in a scraped title ("Yadav's") terminates the
quoted argument early and corrupts the whole graph -- backslash-escaping does not
help, because the parser ends a quoted run at the first bare quote. Reading from
a file sidesteps escaping entirely, and ``expansion=none`` additionally stops
``%`` being treated as a strftime directive.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from typing import List, Optional

from .base import Transformer, TransformRecipe
from .fonts import ffmpeg_path, resolve_font
from .geometry import CropPlan, plan_crop, upscale_factor
from .variation import VariationPlan, watermark_position, wrap_caption

log = logging.getLogger(__name__)

# atempo is only well behaved within [0.5, 2.0].
_ATEMPO_MIN, _ATEMPO_MAX = 0.5, 2.0

# Unicode blocks a plain Latin font (Arial, DejaVu, Liberation) cannot draw.
# Burning text we cannot render produces a row of empty boxes, so we skip it.
_NON_LATIN_RANGES = (
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0700, 0x074F),  # Syriac
    (0x0900, 0x097F),  # Devanagari
    (0x0980, 0x09FF),  # Bengali
    (0x0A00, 0x0A7F),  # Gurmukhi
    (0x0B80, 0x0BFF),  # Tamil
    (0x0E00, 0x0E7F),  # Thai
    (0x1000, 0x109F),  # Myanmar
    (0x3040, 0x30FF),  # Hiragana / Katakana
    (0x4E00, 0x9FFF),  # CJK
    (0xAC00, 0xD7AF),  # Hangul
)


def _probe_dims(path: str) -> tuple[int, int]:
    """Return (width, height) via ffprobe, or (0, 0) if unreadable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, check=True, timeout=15,
        )
        width = height = 0
        for line in out.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "width":
                width = int(value.strip())
            elif key.strip() == "height":
                height = int(value.strip())
        return width, height
    except Exception:
        return 0, 0


def _probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True, timeout=15,
        )
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _has_audio(path: str) -> bool:
    """Whether the file carries an audio stream (many Spotlight clips do not)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True, timeout=15,
        )
        return "audio" in (out.stdout or "")
    except Exception:
        return False


def is_renderable(text: str) -> bool:
    """Whether a Latin font can plausibly draw ``text``.

    Returns False when a meaningful share of the letters come from a script the
    font will not have glyphs for, so callers can skip the overlay instead of
    burning in empty boxes.
    """
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return False
    foreign = sum(
        1 for c in letters
        if any(low <= ord(c) <= high for low, high in _NON_LATIN_RANGES)
    )
    return (foreign / len(letters)) < 0.15


def _atempo_chain(speed: float) -> str:
    """Build an atempo chain that stays inside the filter's supported range."""
    remaining = speed
    stages: list[str] = []
    while remaining > _ATEMPO_MAX:
        stages.append(f"atempo={_ATEMPO_MAX}")
        remaining /= _ATEMPO_MAX
    while remaining < _ATEMPO_MIN:
        stages.append(f"atempo={_ATEMPO_MIN}")
        remaining /= _ATEMPO_MIN
    stages.append(f"atempo={remaining:.6f}")
    return ",".join(stages)


class FFmpegTransformer(Transformer):
    def __init__(self) -> None:
        from ..config import settings

        self.settings = settings
        self.font = resolve_font(settings.transform_caption_font)

    # ------------------------------------------------------------------
    def transform(self, input_path: str, output_path: str,
                  recipe: TransformRecipe,
                  plan: Optional[VariationPlan] = None) -> str:
        """Render ``input_path`` to ``output_path``. Returns the output path."""
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH")
        if not os.path.exists(input_path):
            raise FileNotFoundError(input_path)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        width, height = recipe.width, recipe.height
        source_w, source_h = _probe_dims(input_path)
        duration = _probe_duration(input_path)
        has_audio = _has_audio(input_path)
        speed = plan.speed if (plan and plan.changes_timing) else recipe.speed_factor
        crop = self.crop_plan(source_w, source_h)
        if crop.active:
            log.info(
                "crop: %s (upscale %.2fx -> %dpx canvas)",
                crop.summary(), upscale_factor(crop.out_width, width), width,
            )

        # drawtext reads its strings from files in here; both are torn down
        # together once ffmpeg has exited.
        with tempfile.TemporaryDirectory(prefix="doomscroller_text_") as text_dir:
            video_chain = self._build_video_chain(
                recipe=recipe, plan=plan, canvas=(width, height),
                duration=duration, speed=speed, text_dir=text_dir, crop=crop,
            )
            filter_parts = [f"{video_chain}[vout]"]
            if has_audio:
                if abs(speed - 1.0) > 0.001:
                    filter_parts.append(f"[0:a]{_atempo_chain(speed)}[aout]")
                else:
                    filter_parts.append("[0:a]anull[aout]")
            filter_complex = ";".join(filter_parts)

            args = ["-i", input_path, "-filter_complex", filter_complex,
                    "-map", "[vout]"]
            if has_audio:
                args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
            else:
                args += ["-an"]
            args += [
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "21",
                "-pix_fmt", "yuv420p",
                "-r", "30",
                "-movflags", "+faststart",
                output_path,
            ]

            log.debug("ffmpeg filter_complex: %s", filter_complex)
            self._run(args)

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            raise RuntimeError("ffmpeg produced no usable output")
        log.info(
            "render: %s (%dx%d -> %dx%d)%s",
            os.path.basename(output_path), source_w, source_h, width, height,
            f" [{plan.summary()}]" if plan else "",
        )
        return output_path

    # ------------------------------------------------------------------
    def crop_plan(self, width: int, height: int) -> CropPlan:
        """Plan the source crop for a frame of ``width`` x ``height``.

        Cropping changes the aspect ratio, so the framing step must be decided
        from the *post-crop* dimensions -- otherwise a 3:4 crop goes down the
        no-blur path and gets black bars.
        """
        return plan_crop(
            width, height,
            top_pct=float(self.settings.source_crop_top_pct),
            bottom_pct=float(self.settings.source_crop_bottom_pct),
            mode=str(getattr(self.settings, "source_crop_mode", "fill")),
            keep=str(getattr(self.settings, "source_crop_keep", "right")),
        )

    def _build_video_chain(self, *, recipe: TransformRecipe,
                           plan: Optional[VariationPlan],
                           canvas: tuple[int, int], duration: float,
                           speed: float, text_dir: str,
                           crop: Optional[CropPlan] = None) -> str:
        width, height = canvas
        suffix = self._post_framing_suffix(
            plan=plan, canvas=canvas, duration=duration,
            recipe=recipe, speed=speed, text_dir=text_dir,
        )
        crop_prefix = crop.prefix if crop else ""
        # A crop that leaves a non-9:16 frame needs the blurred filler even if
        # the untouched source would not have.
        needs_blur = recipe.add_blur_background or bool(
            crop and crop.active and not crop.matches_canvas
        )

        if needs_blur:
            # Fill the canvas with a blurred cover copy, then centre the source.
            blur = self.settings.transform_blur_sigma
            return (
                f"[0:v]{crop_prefix}split=2[bg][fg];"
                f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{height},boxblur={blur}:1[bgb];"
                f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2" + suffix
            )

        return (
            f"[0:v]{crop_prefix}"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black" + suffix
        )

    def _post_framing_suffix(self, *, plan: Optional[VariationPlan],
                             canvas: tuple[int, int], duration: float,
                             recipe: TransformRecipe, speed: float,
                             text_dir: str) -> str:
        """Everything applied after the source is on the canvas."""
        width, height = canvas
        parts: list[str] = []

        zoom = plan.zoom if plan else 1.0
        if zoom > 1.001:
            # Scale past the canvas, then crop back to it: a static push-in.
            zoom_w = int(width * zoom) // 2 * 2
            zoom_h = int(height * zoom) // 2 * 2
            parts.append(f"scale={zoom_w}:{zoom_h}:flags=lanczos")
            parts.append(f"crop={width}:{height}:(iw-ow)/2:(ih-oh)/2")

        if (plan and plan.mirror) or recipe.mirror:
            parts.append("hflip")

        if plan and plan.sharpen:
            parts.append("unsharp=5:5:0.8:3:3:0.4")

        if abs(speed - 1.0) > 0.001:
            parts.append(f"setpts={1.0 / speed:.6f}*PTS")

        parts.extend(self._text_filters(
            plan=plan, canvas=canvas, duration=duration,
            recipe=recipe, speed=speed, text_dir=text_dir,
        ))
        return ("," + ",".join(parts)) if parts else ""

    # ------------------------------------------------------------------
    def _write_text(self, text_dir: str, name: str, text: str) -> str:
        """Write one overlay string to a file and return its escaped path."""
        path = os.path.join(text_dir, f"{name}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return ffmpeg_path(path)

    def _drawtext(self, *, text_dir: str, name: str, text: str,
                  font: str, size: int, color: str, x: str, y: str,
                  border: int = 4, box: str = "", gate: str = "",
                  line_spacing: int = 0) -> str:
        """Build one drawtext filter that reads its string from a file."""
        textfile = self._write_text(text_dir, name, text)
        filter_str = (
            f"drawtext=fontfile='{font}':textfile='{textfile}':expansion=none"
            f":fontcolor={color}:fontsize={size}"
            f":borderw={border}:bordercolor=black@0.85"
        )
        if box:
            filter_str += f":box=1:boxcolor={box}:boxborderw=16"
        if line_spacing:
            filter_str += f":line_spacing={line_spacing}"
        filter_str += f":x={x}:y={y}"
        if gate:
            filter_str += f":enable='{gate}'"
        return filter_str

    def _text_filters(self, *, plan: Optional[VariationPlan],
                      canvas: tuple[int, int], duration: float,
                      recipe: TransformRecipe, speed: float,
                      text_dir: str) -> list[str]:
        """drawtext filters for caption, watermark and end card."""
        if self.font is None:
            return []
        width, _height = canvas
        font = ffmpeg_path(self.font)
        filters: list[str] = []
        # setpts rescales the timeline, so time-gated overlays must use the
        # post-retime duration or they drift off the end of a sped-up video.
        effective_duration = (duration / speed) if speed > 0 else duration

        caption = ""
        if plan and plan.caption_text:
            caption = plan.caption_text
        elif recipe.add_caption and recipe.caption_text:
            caption = recipe.caption_text
        if caption and not is_renderable(caption):
            log.info("skipping caption: font cannot render %r", caption[:40])
            caption = ""
        if caption:
            seconds = float(self.settings.caption_seconds)
            gate = ""
            if effective_duration > seconds > 0:
                gate = f"between(t,0,{seconds:.2f})"
            filters.append(self._drawtext(
                text_dir=text_dir, name="caption",
                text=wrap_caption(caption), font=font,
                size=max(30, width // 22), color="white",
                x="(w-text_w)/2", y="h*0.19", border=6,
                box="black@0.35", gate=gate, line_spacing=10,
            ))

        if plan and plan.brand_text and is_renderable(plan.brand_text):
            x_expr, y_expr = watermark_position(plan.watermark_position)
            opacity = float(self.settings.brand_watermark_opacity)
            filters.append(self._drawtext(
                text_dir=text_dir, name="brand", text=plan.brand_text, font=font,
                size=max(22, width // 34), color=f"white@{opacity:.2f}",
                x=x_expr, y=y_expr, border=2,
            ))

        if plan and plan.end_card_text and effective_duration > 3:
            if is_renderable(plan.end_card_text):
                seconds = float(self.settings.end_card_seconds)
                start = max(0.0, effective_duration - seconds)
                filters.append(self._drawtext(
                    text_dir=text_dir, name="endcard", text=plan.end_card_text,
                    font=font, size=max(30, width // 24), color="white",
                    x="(w-text_w)/2", y="h*0.80", border=5, box="black@0.45",
                    gate=f"between(t,{start:.2f},{effective_duration:.2f})",
                ))
        return filters

    # ------------------------------------------------------------------
    @staticmethod
    def _run(args: List[str]) -> None:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {(proc.stderr or '')[-1800:]}")


def recipe_from_dict(d: dict) -> TransformRecipe:
    if not d:
        return TransformRecipe()
    allowed = set(asdict(TransformRecipe()))
    return TransformRecipe(**{k: v for k, v in d.items() if k in allowed})
