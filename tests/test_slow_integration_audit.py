"""Wave 12 real-FFmpeg edge-case audit of ``app/transformers``.

Every ``@real_ffmpeg`` test hands a synthetic clip to the real ``ffmpeg``/``ffprobe`` binaries,
because filter-graph bugs are only visible to ffmpeg's own parser. Fast unit tests at the bottom
cover probe orientation & timeout-retry behaviour in the CI fast tier as well.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.transformers.base import TransformRecipe
from app.transformers.ffmpeg_transformer import FFmpegTransformer, _probe_dims
from app.transformers.variation import VariationPlan

_HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def real_ffmpeg(fn):
    """Mark a test slow, and skip it when the real binaries are missing."""
    fn = pytest.mark.skipif(not _HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")(fn)
    return pytest.mark.slow(fn)


RECIPE = TransformRecipe(add_blur_background=False, add_caption=False, width=1080, height=1920)


def _make_clip(path: Path, size: str, *, seconds: float = 1.2, audio: bool = True) -> str:
    from app.winproc import background_subprocess_kwargs

    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration={seconds}",
    ]
    if audio:
        args += [
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest", "-c:a", "aac",
        ]
    else:
        args += ["-an"]
    # yuv444p keeps odd dimensions legal for the *source* (libx264 rejects odd yuv420p).
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p", str(path)]
    subprocess.run(
        args,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        **background_subprocess_kwargs(low_priority=True),
    )
    return str(path)


@pytest.fixture(scope="module")
def clips_pool(tmp_path_factory) -> dict[str, str]:
    """Generate reusable synthetic source clips once per module."""
    base = tmp_path_factory.mktemp("transformer_audit")
    return {
        "odd_both": _make_clip(base / "odd_541x961.mp4", "541x961", seconds=1.2),
        "odd_down": _make_clip(base / "odd_539x959.mp4", "539x959", seconds=1.2),
        "odd_wide": _make_clip(base / "odd_1281x721.mp4", "1281x721", seconds=1.2),
        "tiny": _make_clip(base / "tiny_162x288.mp4", "162x288", seconds=1.0),
        "plain": _make_clip(base / "plain_540x960.mp4", "540x960", seconds=1.2),
        "landscape": _make_clip(base / "landscape_960x540.mp4", "960x540", seconds=1.2),
        "blink": _make_clip(base / "blink_540x960.mp4", "540x960", seconds=0.5),
        "sync": _make_clip(base / "sync_540x960.mp4", "540x960", seconds=3.0),
    }


def _probe_stream(path: str) -> dict:
    from app.winproc import background_subprocess_kwargs

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,pix_fmt,codec_name", "-of", "json", path],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        **background_subprocess_kwargs(low_priority=True),
    ).stdout
    return json.loads(out)["streams"][0]


def _plan(**kw) -> VariationPlan:
    base = dict(
        zoom=1.05, speed=1.03, mirror=False, watermark_position="mid_right",
        brand_text="Brand", end_card_text="Subscribe", sharpen=True,
    )
    base.update(kw)
    return VariationPlan(**base)


def _assert_valid_short(out: str) -> None:
    info = _probe_stream(out)
    assert info["codec_name"] == "h264"
    assert info["pix_fmt"] == "yuv420p"
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["width"] % 2 == 0 and info["height"] % 2 == 0


@real_ffmpeg
@pytest.mark.parametrize("key", ["odd_both", "odd_down"])
@pytest.mark.parametrize("crop_top", [0, 22])
def test_odd_dimension_sources_render_even_yuv420p_h264(clips_pool, tmp_path, monkeypatch, key, crop_top):
    monkeypatch.setattr(settings, "source_crop_top_pct", crop_top)
    out = str(tmp_path / f"out_{key}_{crop_top}.mp4")
    FFmpegTransformer().transform(clips_pool[key], out, RECIPE, plan=_plan())
    _assert_valid_short(out)


@real_ffmpeg
@pytest.mark.parametrize("blur", [True, False])
def test_odd_landscape_source_with_crop_renders_canvas(clips_pool, tmp_path, monkeypatch, blur):
    monkeypatch.setattr(settings, "source_crop_top_pct", 22)
    out = str(tmp_path / f"wide_out_{int(blur)}.mp4")
    recipe = TransformRecipe(add_blur_background=blur, add_caption=False, width=1080, height=1920)
    FFmpegTransformer().transform(clips_pool["odd_wide"], out, recipe, plan=_plan())
    _assert_valid_short(out)


@real_ffmpeg
def test_tiny_source_is_upscaled_to_canvas(clips_pool, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "source_crop_top_pct", 0)
    out = str(tmp_path / "tiny_out.mp4")
    FFmpegTransformer().transform(clips_pool["tiny"], out, RECIPE, plan=_plan(end_card_text=""))
    _assert_valid_short(out)


@real_ffmpeg
def test_paths_with_spaces_apostrophe_and_unicode(clips_pool, tmp_path):
    weird_in_dir = tmp_path / "Ünï cödé 'dir' [1]"
    weird_in_dir.mkdir()
    weird_src = weird_in_dir / "my clip's #1.mp4"
    shutil.copy2(clips_pool["plain"], weird_src)

    weird_out = tmp_path / "O'Brien & Sons (été)" / "out put.mp4"
    FFmpegTransformer().transform(str(weird_src), str(weird_out), RECIPE, plan=_plan(caption_text="Hi there"))
    assert weird_out.is_file() and weird_out.stat().st_size > 1000
    _assert_valid_short(str(weird_out))


@real_ffmpeg
def test_rotated_phone_footage_is_cropped_in_displayed_orientation(clips_pool, tmp_path, monkeypatch):
    """A 960x540 stream tagged rotate=90 *displays* as 540x960; crop must follow displayed orientation."""
    from app.winproc import background_subprocess_kwargs

    monkeypatch.setattr(settings, "source_crop_top_pct", 22)
    rotated = str(tmp_path / "rotated.mp4")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-display_rotation:v:0", "90",
         "-i", clips_pool["landscape"], "-c", "copy", rotated],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
        **background_subprocess_kwargs(low_priority=True),
    )
    if proc.returncode != 0:
        pytest.skip(f"this ffmpeg cannot write a display matrix: {proc.stderr.strip()[:120]}")
    assert _probe_dims(rotated) == (540, 960)
    out = str(tmp_path / "rot_out.mp4")
    FFmpegTransformer().transform(rotated, out, RECIPE, plan=_plan())
    _assert_valid_short(out)


@real_ffmpeg
def test_very_short_clip_with_end_card_and_speed_up(clips_pool, tmp_path):
    out = str(tmp_path / "blink_out.mp4")
    FFmpegTransformer().transform(clips_pool["blink"], out, RECIPE, plan=_plan(speed=1.04))
    _assert_valid_short(out)


@real_ffmpeg
@pytest.mark.parametrize("speed", [0.5, 0.97, 2.5])
def test_audio_stays_in_sync_with_video_across_speeds(clips_pool, tmp_path, speed):
    from app.winproc import background_subprocess_kwargs

    out = str(tmp_path / f"sync_{speed}.mp4")
    FFmpegTransformer().transform(
        clips_pool["sync"], out, RECIPE, plan=_plan(speed=speed, end_card_text="", brand_text="")
    )
    durations = {}
    for kind in ("v", "a"):
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", f"{kind}:0", "-show_entries",
             "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", out],
            check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            **background_subprocess_kwargs(low_priority=True),
        ).stdout.strip()
        durations[kind] = float(raw)
    assert abs(durations["v"] - durations["a"]) < 0.35, durations
    assert abs(durations["v"] - 3.0 / speed) < 0.45, durations


@real_ffmpeg
def test_failed_render_preserves_existing_output_and_cleans_temp_artifacts(tmp_path):
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"original-render-bytes" * 100)
    before = existing.read_bytes()

    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"this is not a video")
    with pytest.raises(RuntimeError):
        FFmpegTransformer().transform(str(garbage), str(existing), RECIPE, plan=_plan(caption_text="x"))

    assert existing.read_bytes() == before
    assert not (tmp_path / "existing.mp4.tmp.mp4").exists()


# ---------------------------------------------------------------------------
# Fast (non-slow) unit tests: no real binaries, so they also run in the CI fast tier.
# ---------------------------------------------------------------------------
class _Proc:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.returncode = 0


def _fake_probe_json(width, height, **extra):
    stream = {"width": width, "height": height}
    stream.update(extra)
    return json.dumps({"streams": [stream]})


@pytest.mark.parametrize("extra, expected", [
    ({}, (960, 540)),
    ({"side_data_list": [{"rotation": 90}]}, (540, 960)),
    ({"side_data_list": [{"rotation": -90}]}, (540, 960)),
    ({"side_data_list": [{"rotation": 270}]}, (540, 960)),
    ({"side_data_list": [{"rotation": 180}]}, (960, 540)),
    ({"side_data_type_only": True, "side_data_list": [{"side_data_type": "Display Matrix"}]}, (960, 540)),
    ({"tags": {"rotate": "90"}}, (540, 960)),
    ({"tags": {"rotate": "garbage"}}, (960, 540)),
])
def test_probe_dims_reports_displayed_orientation(monkeypatch, extra, expected):
    from app.transformers import ffmpeg_transformer as ft

    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _Proc(_fake_probe_json(960, 540, **extra)))
    assert ft._probe_dims("x.mp4") == expected


@pytest.mark.parametrize("stdout", [
    "", "not json", "{}", '{"streams": []}', '{"streams": [1]}',
    _fake_probe_json(0, 540), _fake_probe_json(None, None),
])
def test_probe_dims_unreadable_payloads_are_zero(monkeypatch, stdout):
    from app.transformers import ffmpeg_transformer as ft

    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: _Proc(stdout))
    assert ft._probe_dims("x.mp4") == (0, 0)


def test_probe_timeout_is_retried_with_a_longer_budget_instead_of_dropping_audio(monkeypatch):
    from app.transformers import ffmpeg_transformer as ft

    timeouts: list[float] = []

    def flaky(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=kwargs["timeout"])
        return _Proc("audio\n")

    monkeypatch.setattr(ft.subprocess, "run", flaky)
    assert ft._has_audio("x.mp4") is True
    assert timeouts[0] < timeouts[1]


@pytest.mark.parametrize("probe, args", [
    ("_has_audio", ()), ("_probe_dims", ()), ("_probe_duration", ()),
])
def test_persistent_probe_timeout_raises_rather_than_degrading_the_render(monkeypatch, probe, args):
    from app.transformers import ffmpeg_transformer as ft

    def always_slow(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=kw["timeout"])

    monkeypatch.setattr(ft.subprocess, "run", always_slow)
    with pytest.raises(RuntimeError, match="timed out"):
        getattr(ft, probe)("x.mp4", *args)


def test_probe_failure_on_a_corrupt_file_still_degrades_quietly(monkeypatch):
    from app.transformers import ffmpeg_transformer as ft

    def bad(*a, **kw):
        raise subprocess.CalledProcessError(1, "ffprobe")

    monkeypatch.setattr(ft.subprocess, "run", bad)
    assert ft._has_audio("x.mp4") is False
    assert ft._probe_dims("x.mp4") == (0, 0)
    assert ft._probe_duration("x.mp4") == 0.0
