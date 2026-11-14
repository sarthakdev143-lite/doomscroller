"""ffmpeg and ffprobe helpers shared by the clipper."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass

from app.winproc import background_subprocess_kwargs


class MediaError(RuntimeError):
    """ffmpeg, ffprobe or the input file failed."""


class Interrupted(Exception):
    """The app is shutting down and the signal reached ffmpeg too.

    Deliberately not a MediaError: nothing is wrong with the video, so the job
    must go back in the queue rather than be recorded as a failure.
    """


# ffmpeg exits 255 when it handles SIGTERM or SIGINT, whatever else it printed.
# -9 stays out: a group SIGKILL would kill Python too, so a -9 seen here means
# ffmpeg died alone, for example to the OOM killer, and retrying would loop.
_FFMPEG_INTERRUPTED = {255, -15, -2, -1}
_FFPROBE_INTERRUPTED = {-15, -2, -1}
_MIN_VIDEO_SEC = 1.0
_CONTENT_SAMPLE_BYTES = 64 * 1024


def content_id(path: str | os.PathLike) -> str:
    """Identifies a file's bytes, so a new mtime or a copy is not a new file.

    Runs on every dashboard refresh, so it reads only the ends, which is where a different encode shows.
    """
    size = os.path.getsize(path)
    digest = hashlib.sha1(str(size).encode())
    with open(path, "rb") as handle:
        digest.update(handle.read(_CONTENT_SAMPLE_BYTES))
        handle.seek(max(0, size - _CONTENT_SAMPLE_BYTES))
        digest.update(handle.read(_CONTENT_SAMPLE_BYTES))
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def require_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise MediaError(f"{' and '.join(missing)} not found on PATH")


def _frame_rate(raw: object) -> float:
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        top, has_slash, bottom = str(raw).strip().partition("/")
        num = float(top)
        den = (float(bottom) if bottom else 1.0) if has_slash else 1.0
    except ValueError:
        return 0.0
    if not math.isfinite(num) or not math.isfinite(den) or num <= 0 or den <= 0:
        return 0.0
    fps = num / den
    return fps if math.isfinite(fps) and fps > 0 else 0.0


def _is_sideways(stream: dict) -> bool:
    """Phones store portrait video as rotated landscape; ffmpeg rotates on decode."""
    rotation = (stream.get("tags") or {}).get("rotate", 0)
    for side in stream.get("side_data_list") or []:
        rotation = side.get("rotation", rotation)
    try:
        return abs(int(float(rotation))) % 180 == 90
    except (TypeError, ValueError):
        return False


def _pixel_shape(stream: dict) -> tuple[int, int]:
    """Sample aspect ratio from ffprobe; "0:1", "N/A" or nothing all mean square."""
    top, _, bottom = str(stream.get("sample_aspect_ratio") or "").partition(":")
    try:
        num, den = int(top), int(bottom)
    except ValueError:
        return 1, 1
    return (num, den) if num > 0 and den > 0 else (1, 1)


def _seconds(raw: object) -> float:
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val if math.isfinite(val) and val > 0 else 0.0


def probe(path: str) -> MediaInfo:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=120,
            **background_subprocess_kwargs(low_priority=True),
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffprobe timed out") from exc
    except OSError as exc:
        raise MediaError(f"ffprobe could not start: {exc}") from exc
    if proc.returncode in _FFPROBE_INTERRUPTED:
        raise Interrupted("ffprobe was stopped")
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed: {proc.stderr[-400:]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise MediaError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(data, dict):
        raise MediaError(f"ffprobe returned invalid JSON for {path}")
    streams = data.get("streams") or []
    # Cover art in an audio file reports itself as a video stream.
    video = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise MediaError(f"no video stream in {path}")
    try:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    except (TypeError, ValueError):
        width, height = 0, 0
    num, den = _pixel_shape(video)
    if _is_sideways(video):
        # ffmpeg's autorotate transposes the pixel shape along with the frame.
        width, height, num, den = height, width, den, num
    # Display width: the same arithmetic as trunc(iw*sar/2)*2 in the render graph.
    width = int(width * (num / den) / 2) * 2
    if width <= 0 or height <= 0:
        raise MediaError(f"invalid video dimensions ({width}x{height}) in {path}")
    # A clip can only be as long as its picture, and audio can outlast it.
    format_info = data.get("format") if isinstance(data.get("format"), dict) else {}
    container = _seconds(format_info.get("duration"))
    stream = _seconds(video.get("duration"))
    duration = min(container, stream) if container and stream else (container or stream)
    if duration <= 0:
        raise MediaError(f"could not read the duration of {path}")
    if duration < _MIN_VIDEO_SEC:
        raise MediaError(f"video is too short: {duration:.2f}s in {path}")
    fps = _frame_rate(video.get("avg_frame_rate")) or _frame_rate(video.get("r_frame_rate"))
    return MediaInfo(
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_audio=any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams),
    )


def _thread_global_args() -> list[str]:
    from app.config import settings

    threads = int(getattr(settings, "ffmpeg_threads", 0) or 0)
    if threads <= 0:
        return []
    return ["-filter_threads", str(threads), "-filter_complex_threads", str(threads)]


def _with_output_threads(args: list[str]) -> list[str]:
    from app.config import settings

    threads = int(getattr(settings, "ffmpeg_threads", 0) or 0)
    if threads <= 0 or not args or args[-1] == "-" or "-threads" in args:
        return list(args)
    return [*args[:-1], "-threads", str(threads), args[-1]]


def ffmpeg_command(args: list[str], *, overwrite: bool = True) -> list[str]:
    base = ["ffmpeg", "-nostdin"]
    if overwrite:
        base.append("-y")
    base.extend(["-hide_banner", "-loglevel", "error", *_thread_global_args()])
    return [*base, *(_with_output_threads(args) if overwrite else args)]


def run_ffmpeg(args: list[str], *, cwd: str | None = None, timeout: int = 3600) -> None:
    # ffmpeg treats stdin as keyboard commands: a stray "q" ends a render early
    # and still exits 0, and a background job stops on tty access.
    safe_timeout = max(1, int(timeout))
    cmd = ffmpeg_command(args, overwrite=True)
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
            cwd=cwd, timeout=safe_timeout,
            **background_subprocess_kwargs(low_priority=True),
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffmpeg timed out after {safe_timeout}s") from exc
    except OSError as exc:
        raise MediaError(f"ffmpeg could not start: {exc}") from exc
    if proc.returncode in _FFMPEG_INTERRUPTED:
        raise Interrupted("ffmpeg was stopped")
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg failed with exit code {proc.returncode}: {proc.stderr[-800:]}")


def read_ffmpeg(args: list[str], *, timeout: int = 3600) -> bytes:
    """Run ffmpeg with ``-`` as its output and return the raw bytes."""
    safe_timeout = max(1, int(timeout))
    cmd = ffmpeg_command(args, overwrite=False)
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=safe_timeout,
            **background_subprocess_kwargs(low_priority=True),
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffmpeg timed out after {safe_timeout}s") from exc
    except OSError as exc:
        raise MediaError(f"ffmpeg could not start: {exc}") from exc
    if proc.returncode in _FFMPEG_INTERRUPTED:
        raise Interrupted("ffmpeg was stopped")
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-800:]}")
    return proc.stdout
