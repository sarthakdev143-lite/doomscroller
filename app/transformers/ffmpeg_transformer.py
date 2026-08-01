"""FFmpeg-based vertical transformer.

Pipeline:
1. Scale + pad source to fit a 1080x1920 vertical canvas
2. If `add_blur_background`, fill the canvas with a blurred + scaled copy of the
   original so the result is full-bleed vertical (avoids YouTube "reused content"
   flags from simple padding)
3. Optional caption burn-in (drawtext)
4. Optional horizontal mirror
5. Optional speed change via setpts + atempo
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict
from typing import List

from .base import Transformer, TransformRecipe


def _ff(*args) -> str:
    """Run ffmpeg and raise on failure."""
    cmd = ["ffmpeg", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-2000:]}")
    return proc.stdout


class FFmpegTransformer(Transformer):
    def __init__(self) -> None:
        from ..config import settings
        self.font = settings.transform_caption_font

    def transform(self, input_path: str, output_path: str, recipe: TransformRecipe) -> str:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH")
        if not os.path.exists(input_path):
            raise FileNotFoundError(input_path)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        W, H = recipe.width, recipe.height
        filters: List[str] = []

        # Build filter chain. Strategy:
        # - blurred background: take original, scale to cover canvas, blur
        # - foreground: original scaled to fit inside canvas, centered overlay
        pre = []
        if recipe.add_blur_background:
            pre.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:1[bg]")
            pre.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease[fg]")
            pre.append(f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v1]")
        else:
            pre.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black[v1]")
        filters.extend(pre)

        cur = "v1"
        if recipe.mirror:
            filters.append(f"[{cur}]hflip[{cur}=m]")
            cur = f"{cur}=m"
        if recipe.speed_factor and recipe.speed_factor != 1.0:
            # setpts uses 1/speed as multiplier
            filters.append(f"[{cur}]setpts={1.0/recipe.speed_factor:.4f}*PTS[{cur}=s]")
            cur = f"{cur}=s"
        if recipe.add_caption and recipe.caption_text:
            text = recipe.caption_text.replace(":", r"\:").replace("'", r"\\'").replace("%", r"\%")
            filters.append(
                f"[{cur}]drawtext=fontfile='{self.font}':text='{text}':"
                f"fontcolor=white:fontsize=72:borderw=4:bordercolor=black:"
                f"x=(w-text_w)/2:y=h-180[{cur}=c]"
            )
            cur = f"{cur}=c"

        # Rename final stream to 'vout'
        if cur != "vout":
            filters.append(f"[{cur}]copy[vout]")

        filter_complex = ";".join(filters)

        args = [
            "-i", input_path,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-shortest",
            output_path,
        ]
        _ff(*args)
        return output_path


def recipe_from_dict(d: dict) -> TransformRecipe:
    if not d:
        return TransformRecipe()
    allowed = {k for k in asdict(TransformRecipe()).keys()}
    return TransformRecipe(**{k: v for k, v in d.items() if k in allowed})
