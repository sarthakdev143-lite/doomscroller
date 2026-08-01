"""Transformers package."""
from .ffmpeg_transformer import FFmpegTransformer, TransformRecipe
from .base import Transformer


def get_transformer() -> Transformer:
    return FFmpegTransformer()


__all__ = ["Transformer", "FFmpegTransformer", "TransformRecipe"]
