"""Tests for source-crop geometry and the credit guarantee that goes with it.

Cropping the top band removes Snapchat's burned-in attribution, which changes
the aspect ratio. The bug these pin down: the framing decision used to be taken
from the *pre-crop* dimensions, so a cropped 3:4 frame went down the no-blur
path and came out letterboxed.
"""
import pytest

from app.metadata import compose
from app.transformers.geometry import (
    MAX_TOTAL_CROP, TARGET_RATIO, plan_crop, upscale_factor,
)


# ------------------------------------------------------------------ no crop
def test_zero_crop_is_inactive():
    plan = plan_crop(540, 960)
    assert not plan.active
    assert plan.prefix == ""
    assert (plan.out_width, plan.out_height) == (540, 960)


@pytest.mark.parametrize("width,height", [(0, 960), (540, 0), (-1, -1)])
def test_bad_dimensions_are_inert(width, height):
    assert not plan_crop(width, height, top_pct=20).active


# --------------------------------------------------------------- fill mode
def test_fill_restores_the_canvas_ratio():
    plan = plan_crop(540, 960, top_pct=22, mode="fill")
    assert plan.active
    assert plan.matches_canvas, f"ratio {plan.ratio} is not 9:16"
    assert abs(plan.ratio - TARGET_RATIO) < 0.03


@pytest.mark.parametrize("width,height", [
    (540, 960), (480, 854), (1080, 1920), (720, 1280), (540, 956),
])
def test_fill_restores_the_ratio_for_every_common_source(width, height):
    plan = plan_crop(width, height, top_pct=22, mode="fill")
    assert plan.matches_canvas


def test_fill_emits_two_crops_and_even_dimensions():
    plan = plan_crop(540, 960, top_pct=22, mode="fill")
    assert len(plan.filters) == 2          # band crop, then width crop
    assert plan.out_width % 2 == 0
    assert plan.out_height % 2 == 0
    assert plan.prefix.endswith(",")


# ---------------------------------------------------------------- fit mode
def test_fit_leaves_a_non_canvas_ratio_for_the_blur_filler():
    plan = plan_crop(540, 960, top_pct=22, mode="fit")
    assert plan.active
    assert not plan.matches_canvas
    assert len(plan.filters) == 1
    assert plan.cropped_sides == 0.0


# ------------------------------------------------------------- keep side
def test_keep_right_drops_the_left_edge():
    """Snapchat stacks its attribution on the left, so that is what should go."""
    right = plan_crop(540, 960, top_pct=22, mode="fill", keep="right")
    left = plan_crop(540, 960, top_pct=22, mode="fill", keep="left")
    centre = plan_crop(540, 960, top_pct=22, mode="fill", keep="center")

    def x_offset(plan):
        return int(plan.filters[1].split(":")[2])

    assert x_offset(left) == 0
    assert x_offset(centre) > 0
    assert x_offset(right) > x_offset(centre)
    # Same amount of width discarded whichever side it comes from.
    assert right.out_width == left.out_width == centre.out_width


def test_keep_side_is_reported_in_the_summary():
    assert "right-keep" in plan_crop(540, 960, top_pct=22, keep="right").summary()


# ------------------------------------------------------------------ limits
def test_oversized_crop_is_refused_rather_than_gutting_the_frame():
    plan = plan_crop(540, 960, top_pct=40, bottom_pct=40)
    assert not plan.active
    assert "too large" in plan.mode
    assert (plan.out_width, plan.out_height) == (540, 960)


def test_crop_at_the_limit_is_allowed():
    total = int(MAX_TOTAL_CROP * 100) - 1
    assert plan_crop(540, 960, top_pct=total, bottom_pct=0).active


def test_tiny_source_is_not_cropped_to_nothing():
    plan = plan_crop(100, 120, top_pct=40)
    assert not plan.active or plan.out_height >= 120


def test_percentages_are_clamped_not_wrapped():
    """An out-of-range percentage clamps to the cap; it is never rescaled.

    An earlier version compared a percentage against a *fraction* bound, so
    22 clamped to 0.4 and a requested 22% crop silently became 0.4%.
    """
    plan = plan_crop(540, 960, top_pct=1000)
    assert plan.cropped_top == pytest.approx(0.45)
    # Not silently reinterpreted as 10% (1000/100) or as a fraction.
    assert plan.cropped_top > 0.4


def test_requested_percentage_is_applied_faithfully():
    plan = plan_crop(540, 960, top_pct=22)
    assert plan.cropped_top == pytest.approx(0.22)
    assert plan.out_height == pytest.approx(960 * 0.78, abs=2)


# ----------------------------------------------------------------- cost
def test_upscale_factor_grows_with_the_crop():
    baseline = upscale_factor(plan_crop(540, 960).out_width, 1080)
    cropped = upscale_factor(plan_crop(540, 960, top_pct=22).out_width, 1080)
    assert baseline == pytest.approx(2.0, rel=0.01)
    assert cropped > baseline
    assert cropped < 3.0, "22% should not require a 3x stretch"


def test_upscale_factor_handles_zero_width():
    assert upscale_factor(0, 1080) == 0.0


# --------------------------------------------------------- credit guarantee
def test_credit_is_emitted_without_a_handle_when_crop_is_active():
    """Cropping removes the on-screen credit, so the text one cannot be optional."""
    md = compose(
        title="T", description="B", keywords=["dance"],
        author="", platform="snapchat", source_url="https://snap/x",
        require_credit=True,
    )
    assert "Originally posted on Snapchat" in md.description
    assert "https://snap/x" in md.description


def test_no_credit_block_when_uncropped_and_anonymous():
    md = compose(title="T", description="B", keywords=[], author="",
                 platform="snapchat", require_credit=False)
    assert "Originally posted" not in md.description
    assert "Credit:" not in md.description


def test_named_creator_is_always_credited():
    for require in (True, False):
        md = compose(title="T", description="B", keywords=[], author="bob",
                     author_name="Bob", platform="snapchat",
                     require_credit=require)
        assert "Credit: Bob (@bob)" in md.description


def test_settings_expose_whether_crop_is_active(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "source_crop_top_pct", 0.0)
    monkeypatch.setattr(settings, "source_crop_bottom_pct", 0.0)
    assert settings.source_crop_active is False
    monkeypatch.setattr(settings, "source_crop_top_pct", 22.0)
    assert settings.source_crop_active is True
