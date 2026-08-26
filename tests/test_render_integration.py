"""End-to-end render tests that actually invoke ffmpeg.

These exist because the apostrophe bug was invisible to unit tests: the filter
graph was built as a perfectly reasonable-looking string, and only ffmpeg's own
parser rejected it. Anything that constructs a filter graph needs at least one
test that hands it to the real binary.

Skipped automatically when ffmpeg is unavailable.
"""
import os
import shutil
import subprocess

import pytest

from app.transformers.base import TransformRecipe
from app.transformers.ffmpeg_transformer import FFmpegTransformer
from app.transformers.variation import VariationPlan

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
        reason="ffmpeg/ffprobe not on PATH",
    ),
]


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> str:
    """A tiny 9:16 test clip with an audio track."""
    path = tmp_path_factory.mktemp("render") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=540x960:rate=30:duration=4",
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(path)],
        check=True, capture_output=True,
    )
    return str(path)


@pytest.fixture(scope="module")
def silent_source(tmp_path_factory) -> str:
    """A clip with no audio stream, as many Spotlight downloads have."""
    path = tmp_path_factory.mktemp("render_silent") / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=540x960:rate=30:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(path)],
        check=True, capture_output=True,
    )
    return str(path)


def _probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "default=noprint_wrappers=1", path],
        check=True, capture_output=True, text=True,
    ).stdout
    values = dict(
        line.split("=", 1) for line in out.strip().splitlines() if "=" in line
    )
    return {k: int(v) for k, v in values.items()}


def _plan(**kw) -> VariationPlan:
    base = dict(zoom=1.05, speed=1.03, mirror=False, watermark_position="mid_right",
                brand_text="Brand", end_card_text="Subscribe", sharpen=True)
    base.update(kw)
    return VariationPlan(**base)


RECIPE = TransformRecipe(add_blur_background=False, add_caption=False,
                         width=1080, height=1920)


def test_renders_to_the_shorts_canvas(source, tmp_path):
    out = str(tmp_path / "out.mp4")
    FFmpegTransformer().transform(source, out, RECIPE, plan=_plan())
    assert _probe(out) == {"width": 1080, "height": 1920}


@pytest.mark.parametrize("caption", [
    "Yadav's Emotional Poetry",          # apostrophe: the original bug
    "100% real, no clickbait",           # percent + comma
    'He said "wow" out loud',            # double quotes
    "Love & Heartbreak: [VIRAL]",        # ampersand, colon, brackets
    "path\\to\\somewhere",               # backslashes
    "semi;colon and =equals",            # filtergraph separators
])
def test_captions_with_hostile_punctuation_render(source, tmp_path, caption):
    out = str(tmp_path / "caption.mp4")
    FFmpegTransformer().transform(
        source, out, RECIPE, plan=_plan(caption_text=caption)
    )
    assert os.path.getsize(out) > 1000


def test_blurred_background_path_renders(source, tmp_path):
    out = str(tmp_path / "blur.mp4")
    recipe = TransformRecipe(add_blur_background=True, add_caption=False,
                            width=1080, height=1920)
    FFmpegTransformer().transform(source, out, recipe, plan=_plan())
    assert _probe(out) == {"width": 1080, "height": 1920}


def test_silent_source_renders_without_an_audio_map(silent_source, tmp_path):
    out = str(tmp_path / "silent_out.mp4")
    FFmpegTransformer().transform(silent_source, out, RECIPE, plan=_plan())
    audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", out],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert audio == "", "no audio stream should be produced for a silent source"


def test_retiming_keeps_audio_and_video_in_step(source, tmp_path):
    """A speed change must apply to both streams, or they desynchronise."""
    out = str(tmp_path / "speed.mp4")
    FFmpegTransformer().transform(source, out, RECIPE, plan=_plan(speed=1.25))

    def duration(path: str, stream: str) -> float:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=True, capture_output=True, text=True,
        ).stdout.strip().splitlines()
        return float(result[0]) if result and result[0] not in ("", "N/A") else 0.0

    video, audio = duration(out, "v:0"), duration(out, "a:0")
    assert video > 0 and audio > 0
    assert abs(video - audio) < 0.35, f"drift: video={video} audio={audio}"


def test_mirroring_renders(source, tmp_path):
    out = str(tmp_path / "mirror.mp4")
    FFmpegTransformer().transform(source, out, RECIPE, plan=_plan(mirror=True))
    assert _probe(out) == {"width": 1080, "height": 1920}


def test_non_latin_caption_is_skipped_not_fatal(source, tmp_path):
    """Unrenderable text must be dropped, never passed to drawtext."""
    out = str(tmp_path / "hindi.mp4")
    FFmpegTransformer().transform(
        source, out, RECIPE, plan=_plan(caption_text="भूमि ठाकुर की यात्रा")
    )
    assert os.path.getsize(out) > 1000


def test_source_crop_trims_and_still_fills_the_canvas(source, tmp_path, monkeypatch):
    transformer = FFmpegTransformer()
    monkeypatch.setattr(transformer.settings, "source_crop_top_pct", 12.0)
    monkeypatch.setattr(transformer.settings, "source_crop_bottom_pct", 10.0)
    out = str(tmp_path / "cropped.mp4")
    transformer.transform(source, out, RECIPE, plan=_plan())
    assert _probe(out) == {"width": 1080, "height": 1920}


def test_no_variation_plan_still_renders(source, tmp_path):
    out = str(tmp_path / "plain.mp4")
    FFmpegTransformer().transform(source, out, RECIPE, plan=None)
    assert _probe(out) == {"width": 1080, "height": 1920}
