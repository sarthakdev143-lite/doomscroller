"""Tests for transform recipe parsing."""
from app.transformers.ffmpeg_transformer import recipe_from_dict


def test_empty_returns_default():
    r = recipe_from_dict({})
    assert r.add_blur_background is True
    assert r.width == 1080
    assert r.height == 1920


def test_overrides_apply():
    r = recipe_from_dict({"add_blur_background": False, "speed_factor": 1.5, "extra_field": 1})
    assert r.add_blur_background is False
    assert r.speed_factor == 1.5
