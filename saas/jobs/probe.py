"""Metadata without downloading: yt-dlp for links, ffprobe for uploads. Both run as subprocesses.

yt-dlp runs out of process so a hung extractor can be killed at the timeout and never holds the web
server. It runs with `--ignore-config` (no user config can add options), `--use-extractors
default,-generic` (the generic extractor would fetch any URL a page points at), no playlists, no
download, and the URL after `--` so it can never be read as an option. yt-dlp refuses file:// unless
told otherwise, and the URL was already validated by ssrf.validate_url.

ffprobe runs with `-protocol_whitelist file` and a demuxer whitelist, so an uploaded HLS/concat
playlist cannot make it open network URLs or other local files.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..auth.errors import ApiError
from ..config import settings
from . import ssrf

log = logging.getLogger("saas.jobs.probe")

FFPROBE_FORMATS = "mov,mp4,matroska,webm,avi"
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def background_kwargs() -> dict:
    """No console window and low priority on Windows; stdin always closed."""
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    return kw


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, **background_kwargs())


class ProbeError(ApiError):
    pass


def ytdlp_metadata(url: str) -> dict:
    """{title, duration_s, thumbnail, webpage_url, extractor, uploader} for one video, or ProbeError."""
    safe = ssrf.validate_url(url)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(safe)
        if hit and hit[0] > now:
            return dict(hit[1])
    cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-cache-dir", "--dump-single-json",
           "--skip-download", "--no-playlist", "--flat-playlist", "--playlist-items", "1", "--no-warnings",
           "--socket-timeout", "10", "--retries", "1", "--use-extractors", "default,-generic",
           "--ignore-no-formats-error", "--no-progress"]
    if settings.ytdlp_proxy:
        cmd += ["--proxy", settings.ytdlp_proxy]
    cmd += ["--", safe]
    try:
        proc = _run(cmd, settings.ytdlp_timeout_s)
    except subprocess.TimeoutExpired:
        raise ProbeError(504, "source_timeout", "That site took too long to answer. Try again in a minute.")
    if proc.returncode != 0 or not proc.stdout.strip():
        err = proc.stderr.decode("utf-8", "replace").lower()
        if "unsupported url" in err or "no suitable" in err:
            raise ProbeError(400, "source_not_allowed", "That link isn't a video page we can read.")
        if "private" in err or "sign in" in err or "login" in err or "members" in err:
            raise ProbeError(400, "source_private", "That video is private or needs a login. Use a public link or upload the file.")
        if "not available" in err or "removed" in err or "404" in err:
            raise ProbeError(400, "source_unavailable", "That video isn't available.")
        log.info("yt-dlp metadata failed (%s): %s", proc.returncode, err[-300:])
        raise ProbeError(400, "source_unreadable", "We couldn't read that video. Check the link, or upload the file.")
    try:
        info = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        raise ProbeError(400, "source_unreadable", "We couldn't read that video.")
    meta = parse_info(info)
    with _cache_lock:
        if len(_cache) > 2000:
            _cache.clear()
        _cache[safe] = (now + settings.estimate_cache_s, meta)
    return dict(meta)


def parse_info(info: Any) -> dict:
    if not isinstance(info, dict):
        raise ProbeError(400, "source_unreadable", "We couldn't read that video.")
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        raise ProbeError(400, "source_playlist", "That's a playlist or channel. Paste a link to one video.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise ProbeError(400, "source_live", "Live streams aren't supported. Try again once the recording is published.")
    page = info.get("webpage_url") or info.get("original_url")
    if isinstance(page, str) and page.startswith(("http://", "https://")):
        try:
            ssrf.validate_url(page, resolve=False)
        except ApiError:
            raise ProbeError(400, "source_not_allowed", "That link redirects somewhere we don't support.")
    try:
        duration = float(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ProbeError(400, "duration_unknown", "We couldn't tell how long that video is.")
    thumb = info.get("thumbnail")
    return {"title": str(info.get("title") or "")[:300], "duration_s": round(duration, 2),
            "thumbnail": thumb if isinstance(thumb, str) and thumb.startswith("https://") else None,
            "webpage_url": page if isinstance(page, str) else None,
            "extractor": str(info.get("extractor_key") or "")[:40],
            "uploader": str(info.get("uploader") or info.get("channel") or "")[:120]}


def ffprobe(path: Path) -> dict:
    """{duration_s, format_name, width, height, has_video, has_audio} or ProbeError."""
    cmd = [settings.ffprobe_bin, "-hide_banner", "-v", "error", "-protocol_whitelist", "file",
           "-format_whitelist", FFPROBE_FORMATS,
           "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,width,height",
           "-of", "json", str(path)]
    try:
        proc = _run(cmd, 60)
    except subprocess.TimeoutExpired:
        raise ProbeError(400, "invalid_media", "That file took too long to inspect.")
    except FileNotFoundError:
        log.error("ffprobe not found (%s)", settings.ffprobe_bin)
        raise ProbeError(503, "media_tools_unavailable", "Uploads are temporarily unavailable.")
    if proc.returncode != 0:
        raise ProbeError(415, "invalid_media", "That file isn't a video we can read.")
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        raise ProbeError(415, "invalid_media", "That file isn't a video we can read.")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png")]
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    width = int((video[0].get("width") if video else 0) or 0)
    height = int((video[0].get("height") if video else 0) or 0)
    return {"duration_s": round(duration, 2), "format_name": str(fmt.get("format_name") or ""),
            "width": width, "height": height, "has_video": bool(video),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
