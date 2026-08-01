"""Transformer interface."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TransformRecipe:
    add_blur_background: bool = True
    add_caption: bool = True
    caption_text: str = ""
    speed_factor: float = 1.0
    mirror: bool = False
    width: int = 1080
    height: int = 1920


class Transformer:
    def transform(self, input_path: str, output_path: str, recipe: TransformRecipe) -> str:
        raise NotImplementedError
