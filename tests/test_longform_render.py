"""Slow real-ffmpeg end-to-end tests for native 16:9 1080p compilation render & thumbnails."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image
import pytest

from app.clipper.media import probe
from app.longform import planner, render as lf_render, thumbnails

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
        reason="ffmpeg/ffprobe not on PATH",
    ),
]


def _make_source(path: Path, *, seconds: int = 12, freq: int = 330) -> str:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return str(path)


def test_real_ffmpeg_longform_1080p_render_chapters_and_3_thumbnails(tmp_path):
    s1 = _make_source(tmp_path / "c1.mp4", seconds=12, freq=330)
    s2 = _make_source(tmp_path / "c2.mp4", seconds=12, freq=440)
    seg1 = planner.SegmentPlan(s1, "https://youtu.be/1", "1", "@CreatorOne", "Creator One", "Clip 1", 1.0, 7.0, 88.0, "Crazy Open", "en")
    seg2 = planner.SegmentPlan(s2, "https://youtu.be/2", "2", "@CreatorTwo", "Creator Two", "Clip 2", 1.0, 8.0, 96.0, "Big Finale", "en")
    cold = planner.pick_cold_open([seg1, seg2])

    plan = planner.CompilationPlan(
        format_kind="vs",
        channel_slug="default",
        part_number=None,
        targets=("@CreatorOne", "@CreatorTwo"),
        theme="",
        title="CreatorOne vs CreatorTwo Biggest Moments",
        description="Highlight reel",
        tags=("compilation", "vs"),
        language="en",
        target_seconds=360.0,
        cold_open=cold,
        segments=(seg1, seg2),
    )

    out_mp4 = tmp_path / "compiled_1080p.mp4"
    res = lf_render.render_compilation(plan, str(out_mp4), brand_name="@TestBrand", include_end_screen=True)

    info = probe(res.output_path)
    assert (info.width, info.height) == (1920, 1080)
    assert info.fps == pytest.approx(30.0, abs=0.5)
    assert info.has_audio is True
    assert info.duration >= 18.0
    assert "0:00 Intro / Preview" in res.chapter_text
    assert len(res.chapters) == 3

    ch = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0", res.output_path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert int(ch) == 2

    thumbs = thumbnails.generate_thumbnails(
        res.output_path,
        out_dir=tmp_path / "thumbs",
        title=plan.title,
        format_kind=plan.format_kind,
        chapters=res.chapters,
        timestamps=res.timestamps_for_thumbs,
        creator_labels=plan.targets,
    )
    assert len(thumbs) == 3
    for t in thumbs:
        p = Path(t["path"])
        assert p.is_file() and 0 < p.stat().st_size < 2_000_000
        with Image.open(p) as im:
            assert im.format == "JPEG" and im.size == (1280, 720)
