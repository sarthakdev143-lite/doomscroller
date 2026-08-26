"""Tests for output variation, caption wrapping, fonts and text safety.

The escaping tests exist because a scraped title containing an apostrophe
("Yadav's") silently corrupted the whole ffmpeg filter graph -- the parser ends a
quoted argument at the first bare quote, and backslash-escaping does not help.
The fix was to stop passing text inline at all.
"""
import os

import pytest

from app.transformers import fonts
from app.transformers.ffmpeg_transformer import (
    _atempo_chain, is_renderable, recipe_from_dict,
)
from app.transformers.variation import (
    caption_from, plan_variation, watermark_position, wrap_caption,
)


class FakeSettings:
    """Minimal stand-in so variation tests do not depend on the real .env."""

    transform_variation = True
    zoom_min, zoom_max = 1.02, 1.08
    speed_min, speed_max = 0.97, 1.04
    brand_name = "TestBrand"
    caption_enabled = True
    caption_max_chars = 42
    end_card_enabled = True
    end_card_text = "Subscribe"
    upscale_sharpen = True


# ------------------------------------------------------------------ planning
def test_plan_is_deterministic_per_video():
    a = plan_variation(video_id="vid-1", title="T", settings=FakeSettings())
    b = plan_variation(video_id="vid-1", title="T", settings=FakeSettings())
    assert (a.zoom, a.speed, a.mirror, a.watermark_position) == (
        b.zoom, b.speed, b.mirror, b.watermark_position
    )


def test_plans_differ_across_videos():
    ids = [f"vid-{i}" for i in range(24)]
    plans = [plan_variation(video_id=i, settings=FakeSettings()) for i in ids]
    # If every upload got identical treatment the channel would just have a
    # different mechanical fingerprint.
    assert len({(p.zoom, p.speed) for p in plans}) > 15


def test_plan_stays_inside_configured_ranges():
    for i in range(40):
        plan = plan_variation(video_id=f"v{i}", settings=FakeSettings())
        assert FakeSettings.zoom_min <= plan.zoom <= FakeSettings.zoom_max
        assert FakeSettings.speed_min <= plan.speed <= FakeSettings.speed_max


def test_variation_can_be_disabled():
    class Off(FakeSettings):
        transform_variation = False

    plan = plan_variation(video_id="v", title="T", settings=Off())
    assert plan.zoom == 1.0
    assert plan.speed == 1.0
    assert not plan.changes_framing and not plan.changes_timing
    assert plan.caption_text == ""


def test_mirror_is_a_minority_treatment():
    plans = [plan_variation(video_id=f"v{i}", settings=FakeSettings()) for i in range(200)]
    ratio = sum(1 for p in plans if p.mirror) / len(plans)
    assert 0.05 < ratio < 0.30


def test_summary_reports_what_was_applied():
    plan = plan_variation(video_id="v1", title="A title", settings=FakeSettings())
    summary = plan.summary()
    assert "zoom=" in summary and "speed=" in summary
    assert "brand@" in summary


def test_watermark_positions_are_inside_the_safe_band():
    # Snapchat brands the top and bottom of the frame; our watermark must not
    # land on top of theirs.
    for name in ("upper_right", "mid_right", "mid_left", "lower_right"):
        _x, y = watermark_position(name)
        assert y.startswith("h*")
        fraction = float(y.split("*")[1])
        assert 0.15 < fraction < 0.65
    # An unknown name still resolves rather than raising.
    assert watermark_position("nope") == watermark_position("mid_right")


# ------------------------------------------------------------------ captions
def test_caption_prefers_the_hook_before_a_separator():
    text = caption_from("The Real Hook | a much longer trailing clause here", [], 30)
    assert text == "The Real Hook"


def test_caption_falls_back_to_keywords():
    assert caption_from("", ["dance", "party", "fun"], 40) == "dance party fun"


def test_caption_is_length_capped_on_a_word_boundary():
    text = caption_from("word " * 40, [], 20)
    assert len(text) <= 20
    assert not text.endswith("wor")


@pytest.mark.parametrize("text,max_lines", [
    ("short", 2),
    ("a moderately long caption that needs wrapping across lines", 2),
    ("one two three four five six seven eight nine ten", 3),
])
def test_wrap_caption_respects_line_budget(text, max_lines):
    wrapped = wrap_caption(text, max_chars_per_line=20, max_lines=max_lines)
    lines = wrapped.split("\n")
    assert len(lines) <= max_lines
    for line in lines:
        assert len(line) <= 23  # 20 + the "..." marker


def test_wrap_caption_marks_truncation():
    wrapped = wrap_caption("one two three four five six seven eight", 12, 2)
    assert wrapped.endswith("...")


def test_wrap_caption_handles_empty_and_long_words():
    assert wrap_caption("") == ""
    # A single word longer than the line budget must not be dropped.
    assert "supercalifragilistic" in wrap_caption("supercalifragilistic", 10, 2)


# --------------------------------------------------------------- renderability
def test_latin_text_is_renderable():
    assert is_renderable("Kitten Drama Alert")
    assert is_renderable("Yadav's Poetry & Love")
    assert is_renderable("Bonne nuit les petits")


@pytest.mark.parametrize("text", [
    "भूमि ठाकुर की यात्रा",   # Devanagari
    "안녕하세요 여러분",          # Hangul
    "مرحبا بالعالم",           # Arabic
])
def test_non_latin_text_is_not_renderable(text):
    # A Latin font would draw empty boxes, so callers skip the overlay.
    assert not is_renderable(text)


def test_text_without_letters_is_not_renderable():
    assert not is_renderable("12345 !!!")
    assert not is_renderable("")


# ---------------------------------------------------------------------- fonts
def test_resolve_font_finds_something_on_this_machine():
    assert fonts.resolve_font("") is not None


def test_resolve_font_ignores_a_missing_configured_path():
    # The historical default was a Debian path that does not exist on Windows.
    resolved = fonts.resolve_font("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if resolved is not None:
        assert os.path.isfile(resolved)


def test_resolve_font_honours_a_valid_configured_path():
    existing = fonts.resolve_font("")
    if existing:
        assert fonts.resolve_font(existing) == existing


def test_ffmpeg_path_escapes_windows_drive_letters():
    escaped = fonts.ffmpeg_path(r"C:\Windows\Fonts\arialbd.ttf")
    assert "\\W" not in escaped          # backslashes normalised
    assert escaped.startswith("C\\:")     # drive colon escaped
    assert escaped.endswith("arialbd.ttf")


# --------------------------------------------------------------------- atempo
@pytest.mark.parametrize("speed", [0.97, 1.0, 1.04, 0.5, 2.0, 4.5, 0.2])
def test_atempo_chain_multiplies_back_to_the_requested_speed(speed):
    chain = _atempo_chain(speed)
    product = 1.0
    for stage in chain.split(","):
        product *= float(stage.split("=")[1])
    assert product == pytest.approx(speed, rel=1e-4)


def test_atempo_stages_stay_in_supported_range():
    for stage in _atempo_chain(5.0).split(","):
        value = float(stage.split("=")[1])
        assert 0.5 <= value <= 2.0


# --------------------------------------------------------------------- recipe
def test_recipe_from_dict_ignores_unknown_keys():
    recipe = recipe_from_dict({"add_blur_background": False, "bogus": 1})
    assert recipe.add_blur_background is False
    assert recipe.width == 1080


def test_recipe_from_dict_defaults_on_empty():
    assert recipe_from_dict({}).add_blur_background is True
