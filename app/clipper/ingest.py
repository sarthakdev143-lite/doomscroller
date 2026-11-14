"""Turn a link or a local path into a video file on disk."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import settings
from app.scrapers.yt_dlp_scraper import YtDlpScraper

from .media import content_id

log = logging.getLogger(__name__)


# How yt-dlp reports a video that nobody can fetch, as opposed to this host being refused.
_GONE = ("private video", "video is private", "video unavailable", "video is unavailable", "has been removed",
         "no longer available", "been terminated", "premieres in", "will begin in")
_NOT_GONE = ("try again later", "in your country")


class IngestError(RuntimeError):
    """The source could not be turned into a local file. `permanent` means trying again cannot help."""

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class SourceVideo:
    path: str
    title: str = ""
    description: str = ""
    author: str = ""
    author_name: str = ""
    url: str = ""
    source: str = "local"
    source_id: str = ""
    keywords: list = field(default_factory=list)
    title_from_filename: bool = False


_PLAYLIST_PARAMS = {"list", "index", "start_radio", "playnext", "pp"}
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be",
}
_PROXY_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@")


def is_url(source: str) -> bool:
    return source.strip().lower().startswith(("http://", "https://"))


def _normalise_url(url: str) -> str:
    """Strip playlist context parameters when a YouTube URL already names one concrete video."""
    cleaned = url.strip()
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return cleaned
    host = (parts.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS or not parts.query:
        return cleaned
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.lower() in _PLAYLIST_PARAMS for k, _ in pairs):
        return cleaned
    if host in ("youtu.be", "www.youtu.be"):
        slug = parts.path.strip("/")
        single_video = bool(slug) and "/" not in slug and slug.lower() not in {
            "playlist", "watch", "channel", "c", "user",
        }
    elif parts.path == "/watch":
        single_video = any(k.lower() == "v" and v.strip() for k, v in pairs)
    elif parts.path.startswith(("/shorts/", "/live/", "/embed/")):
        segments = [seg for seg in parts.path.split("/") if seg]
        single_video = len(segments) >= 2 and bool(segments[1].strip())
    else:
        single_video = False
    if not single_video:
        return cleaned
    kept = [(k, v) for k, v in pairs if k.lower() not in _PLAYLIST_PARAMS]
    netloc = "www.youtube.com" if host == "music.youtube.com" else parts.netloc
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(kept, doseq=True), parts.fragment))


def _scrub_secrets(message: object) -> str:
    text = str(message or "")
    if settings.proxy_url:
        text = text.replace(settings.proxy_url, _PROXY_CRED_RE.sub(r"\1[REDACTED]@", settings.proxy_url))
    return _PROXY_CRED_RE.sub(r"\1[REDACTED]@", text)


def _safe_source_name(raw: object) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", str(raw or "").lower()).strip("_")
    return cleaned or "web"


def _safe_source_id(raw: object, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(raw or ""))
    cleaned = re.sub(r"\.{2,}", "_", cleaned).strip("._-")
    return cleaned or fallback


def file_fingerprint(path: Path) -> str:
    """Which bytes a path holds. New contents are a new source, and a new mtime or a copy is the same one."""
    return content_id(path.expanduser().resolve())


def _from_file(raw: str) -> SourceVideo:
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise IngestError(f"file not found: {path}")
    return SourceVideo(
        path=str(path),
        title=path.stem.replace("_", " ").replace("-", " ").strip(),
        source="local",
        source_id=file_fingerprint(path),
        title_from_filename=True,
    )


def network_options() -> dict:
    """The proxy and cookies that get yt-dlp past a block on this host."""
    options: dict = {}
    if settings.proxy_url:
        options["proxy"] = settings.proxy_url
    # YouTube now hides its video formats behind a JavaScript challenge. yt-dlp solves it with a
    # JavaScript runtime and its own solver script, which it fetches from yt-dlp's GitHub releases.
    runtimes = {name: {"path": path} for name in ("deno", "node", "bun") if (path := shutil.which(name))}
    if runtimes:
        options["js_runtimes"] = runtimes
        options["remote_components"] = {"ejs:github"}
    cookie_file = str(Path(settings.cookies_file.strip()).expanduser()) if settings.cookies_file and settings.cookies_file.strip() else ""
    if cookie_file and os.path.isfile(cookie_file) and os.path.getsize(cookie_file):
        options["cookiefile"] = cookie_file
        return options
    cookies = YtDlpScraper._parse_browser_cookies(settings.cookies_from_browser or "")
    if cookies:
        options["cookiesfrombrowser"] = cookies[0] if isinstance(cookies[0], tuple) else (cookies[0],)
    return options


def _ydl_options(out_dir: Path) -> dict:
    height = settings.clip_source_max_height
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        # Big channels ship a dozen dubbed tracks; the original is the one the captions are for.
        # Prefer H.264 (avc1) ahead of AV1/VP9 so older CPUs without AVX2/AV1 hardware decode 3-5x faster.
        "format": (
            f"bv*[vcodec^=avc1][height<={height}]+ba[format_note*=original]/"
            f"bv*[vcodec^=avc1][height<={height}]+ba/"
            f"bv*[height<={height}]+ba[format_note*=original]/"
            f"bv*[height<={height}]+ba/b[height<={height}]/b"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(extractor_key)s_%(id)s.%(ext)s"),
        **network_options(),
    }


def _downloaded_path(info: dict) -> Optional[str]:
    for item in info.get("requested_downloads") or []:
        if not isinstance(item, dict):
            continue
        path = item.get("filepath")
        if path and os.path.isfile(path):
            return path
    path = info.get("filepath") or info.get("_filename")
    return path if path and os.path.isfile(path) else None


def _is_gone(message: str) -> bool:
    lowered = message.lower()
    return any(mark in lowered for mark in _GONE) and not any(mark in lowered for mark in _NOT_GONE)


def _validate_single_video(info: Optional[dict]) -> dict:
    if not info or not isinstance(info, dict):
        raise IngestError("download returned nothing")
    if info.get("entries") is not None or info.get("_type") in ("playlist", "multi_video"):
        raise IngestError("that link is a playlist or channel; paste one video", permanent=True)
    if info.get("is_live"):
        raise IngestError("live streams are not supported", permanent=True)
    if info.get("live_status") == "is_upcoming":
        raise IngestError("this video has not aired yet", permanent=True)
    return info


def _from_url(url: str) -> SourceVideo:
    import yt_dlp

    url = _normalise_url(url)
    out_dir = settings.content_path / "long"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Look before downloading: noplaylist only covers watch?v=...&list= links, so a
    # channel link would otherwise download every video before being refused.
    options = {**_ydl_options(out_dir), "extract_flat": "in_playlist", "playlist_items": "1"}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = _validate_single_video(ydl.extract_info(url, download=False))
            info = _validate_single_video(ydl.process_ie_result(info, download=True))
    except IngestError:
        raise
    except Exception as exc:
        detail = _scrub_secrets(exc)
        # A private video's message also mentions --cookies, so this comes first.
        if _is_gone(detail):
            raise IngestError(f"source is not available: {detail}", permanent=True) from None
        if "confirm you" in detail or "--cookies" in detail:
            raise IngestError(
                "YouTube wants a signed-in session for this download. Set "
                "COOKIES_FROM_BROWSER=chrome (or firefox) in .env, or download the video and pass its file path."
            ) from None
        raise IngestError(f"download failed: {detail}") from None
    path = _downloaded_path(info)
    if path is None:
        raise IngestError("download finished but the file is missing")
    # Cleanup ages downloads by mtime, and a reused file keeps its old one.
    os.utime(path, None)
    fallback_id = hashlib.sha1(url.encode()).hexdigest()[:16]
    return SourceVideo(
        path=path,
        title=info.get("title") or "",
        description=(info.get("description") or "")[:4000],
        author=info.get("uploader_id") or info.get("channel_id") or "",
        author_name=info.get("uploader") or info.get("channel") or "",
        url=info.get("webpage_url") or url,
        source=_safe_source_name(info.get("extractor_key") or "web"),
        source_id=_safe_source_id(info.get("id"), fallback_id),
        keywords=list(info.get("tags") or [])[:20],
    )


def fetch(source: str) -> SourceVideo:
    source = source.strip()
    if not source:
        raise IngestError("no link or file given")
    return _from_url(source) if is_url(source) else _from_file(source)
