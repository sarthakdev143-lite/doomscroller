"""Wave 9 audit & regression tests for app/metadata.py and app/transformers/*."""
from __future__ import annotations

import math
import subprocess
from unittest.mock import patch

import pytest

from app.metadata import build_tags, compose, finished_title, youtube_description, youtube_title
from app.transformers import fonts
from app.transformers.base import TransformRecipe
from app.transformers.ffmpeg_transformer import FFmpegTransformer, _atempo_chain, _probe_duration
from app.transformers.geometry import plan_crop
from app.transformers.variation import caption_from, plan_variation, wrap_caption


# ---------------------------------------------------------------------------
# app/metadata.py — control-char / null-byte stripping & boundary safety
# ---------------------------------------------------------------------------
def test_metadata_strips_null_bytes_and_control_chars_everywhere():
    dirty_title = "Shocking\x00 Reveal\x07\x1b\x7f <now>"
    dirty_desc = "Line one\x00\r\nLine\x0b two\x1f <tag>"

    ft = finished_title(dirty_title)
    assert "\x00" not in ft and "\x07" not in ft and "\x1b" not in ft and "\x7f" not in ft
    assert "<" not in ft and ">" not in ft
    assert ft.startswith("Shocking Reveal now")

    yt = youtube_title("Hello\x00\x08World")
    assert "\x00" not in yt and "\x08" not in yt and "Hello" in yt

    yd = youtube_description(dirty_desc)
    assert "\x00" not in yd and "\x0b" not in yd and "\x1f" not in yd and "\r" not in yd
    assert "Line one\nLine" in yd
    assert youtube_description("hello", limit=0) == ""
    assert youtube_description("hello", limit=-10) == ""

    md = compose(
        title=dirty_title,
        description=dirty_desc,
        keywords=["gpu\x00chip", "ai\x07"],
        author="@cre\x00ator<1>",
        author_name="Bob\x00\n<Smith>",
        source_url="https://youtu.be/abc\x00\n",
        platform="you\x00tube",
    )
    for ch in ("\x00", "\x07", "\x0b", "\x1b", "\x1f", "\x7f", "<", ">", "\r"):
        assert ch not in md.title
        assert ch not in md.description
        assert all(ch not in t for t in md.tags)
    assert "Credit: Bob Smith (@creator1) on Youtube" in md.description


def test_build_tags_accepts_bare_string_and_ignores_none_items():
    tags = build_tags("robotics")
    assert "robotics" in tags
    tags_with_none = build_tags([None, "", "  Quantum  ", "quantum"])  # type: ignore[list-item]
    assert tags_with_none.count("quantum") == 1


# ---------------------------------------------------------------------------
# app/transformers/geometry.py — odd source dimensions & NaN/Inf safety
# ---------------------------------------------------------------------------
def test_plan_crop_enforces_even_width_and_height_on_odd_sources():
    # Fit mode previously passed odd source width straight into crop=541:768 and out_width=541
    fit = plan_crop(541, 961, top_pct=20, mode="fit")
    assert fit.active
    assert fit.out_width % 2 == 0 and fit.out_height % 2 == 0
    crop_dims = fit.filters[0].split("=")[1].split(":")
    assert int(crop_dims[0]) % 2 == 0 and int(crop_dims[1]) % 2 == 0

    # Fill mode with narrow aspect where second crop does not trigger must still have even out_width
    narrow = plan_crop(401, 961, top_pct=10, mode="fill")
    assert narrow.active
    assert narrow.out_width % 2 == 0 and narrow.out_height % 2 == 0


@pytest.mark.parametrize("bad_pct", [math.nan, math.inf, -math.inf])
def test_plan_crop_handles_nan_and_inf_percentages_without_crashing(bad_pct):
    plan = plan_crop(540, 960, top_pct=bad_pct, bottom_pct=0.0)
    assert not plan.active


# ---------------------------------------------------------------------------
# app/transformers/fonts.py — Windows path safety, quotes & .ttc fallback
# ---------------------------------------------------------------------------
def test_ffmpeg_path_escapes_single_quotes_and_strips_nulls():
    tricky = r"C:\Users\O'Connor\AppData\Local\Temp\caption.txt" + "\x00"
    escaped = fonts.ffmpeg_path(tricky)
    assert "\x00" not in escaped
    assert escaped.startswith(r"C\:/Users/O")
    # Single quote must be safely escaped for drawtext='...' filter syntax
    assert r"'\''" in escaped


def test_resolve_font_strips_wrapping_quotes_on_configured_path(tmp_path):
    fake_font = tmp_path / "CustomBold.ttc"
    fake_font.write_bytes(b"fake-ttc")
    quoted = f'"{fake_font}"'
    assert fonts.resolve_font(quoted) == str(fake_font)


# ---------------------------------------------------------------------------
# app/transformers/variation.py — negative slice, None keywords & inverted bounds
# ---------------------------------------------------------------------------
def test_variation_edge_cases_tiny_line_budget_none_keywords_and_inverted_ranges():
    # Tiny max_chars_per_line must not trigger negative slice bug (max_chars_per_line - 3 < 0)
    wrapped = wrap_caption("alpha beta gamma", max_chars_per_line=2, max_lines=2)
    assert wrapped.endswith("...")
    assert wrap_caption("alpha beta", max_chars_per_line=0, max_lines=2) == ""
    assert wrap_caption("alpha beta", max_chars_per_line=20, max_lines=0) == ""

    # None / blank keywords must never turn into literal "None" burned onto video
    assert caption_from("", [None, "", "  science  "], 30) == "science"  # type: ignore[list-item]
    assert caption_from("Hook text", [], 0) == ""

    class InvertedSettings:
        transform_variation = True
        zoom_min, zoom_max = 1.08, 1.02
        speed_min, speed_max = 1.05, 0.95
        brand_name = "@brand"
        caption_enabled = True
        caption_max_chars = 30
        end_card_enabled = False
        end_card_text = ""
        upscale_sharpen = False

    plan = plan_variation(video_id="inv-1", title="Test hook", settings=InvertedSettings())
    assert 1.02 <= plan.zoom <= 1.08
    assert 0.95 <= plan.speed <= 1.05


# ---------------------------------------------------------------------------
# app/transformers/ffmpeg_transformer.py — speed safety, even canvas, Hindi font & subprocess kwargs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_speed", [0.0, -1.0, math.nan, math.inf])
def test_atempo_chain_rejects_non_positive_or_non_finite_speed_instead_of_looping(bad_speed):
    with pytest.raises(ValueError):
        _atempo_chain(bad_speed)


def test_ffmpeg_transformer_uses_even_canvas_force_divisible_devanagari_font_and_clean_textfiles(
    tmp_path, monkeypatch
):
    deva_font = tmp_path / "NotoSansDevanagari-Bold.ttf"
    latin_font = tmp_path / "ArialBold.ttf"
    deva_font.write_bytes(b"deva")
    latin_font.write_bytes(b"latin")

    monkeypatch.setattr(fonts, "devanagari_font", lambda: str(deva_font))
    monkeypatch.setattr(fonts, "_discovered_font", lambda: str(latin_font))

    transformer = FFmpegTransformer()
    transformer.settings.transform_caption_font = ""

    # Odd canvas dims (1081x1921) -> normalized to even (1080x1920) & scale uses force_divisible_by=2
    recipe = TransformRecipe(
        add_blur_background=True,
        add_caption=True,
        caption_text="नमस्ते दुनिया\x00\r\nदूसरी पंक्ति",
        width=1081,
        height=1921,
    )
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    chain = transformer._build_video_chain(
        recipe=recipe,
        plan=None,
        canvas=(1081, 1921),
        duration=12.0,
        speed=1.0,
        text_dir=str(text_dir),
    )
    assert "1081" not in chain and "1921" not in chain
    assert "scale=1080:1920" in chain
    assert "force_divisible_by=2" in chain
    # Hindi caption must resolve the Devanagari font, not the Latin fallback
    assert "NotoSansDevanagari-Bold.ttf" in chain

    caption_file = text_dir / "caption.txt"
    written = caption_file.read_text(encoding="utf-8")
    assert "\x00" not in written and "\r" not in written


def test_probe_duration_handles_na_and_nan_and_passes_background_kwargs():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)

        class _Proc:
            stdout = "N/A\n"

        return _Proc()

    with patch("app.transformers.ffmpeg_transformer.subprocess.run", side_effect=fake_run):
        assert _probe_duration("dummy.webm") == 0.0

    assert calls and calls[0].get("stdin") == subprocess.DEVNULL
