"""Turn a link or a local path into a video file on disk."""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.config import settings
from app.scrapers.yt_dlp_scraper import YtDlpScraper

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    """The source could not be turned into a local file."""


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


def is_url(source: str) -> bool:
    return source.strip().lower().startswith(("http://", "https://"))


def _from_file(raw: str) -> SourceVideo:
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise IngestError(f"file not found: {path}")
    stat = path.stat()
    fingerprint = f"{path}|{stat.st_size}|{int(stat.st_mtime)}".encode()
    return SourceVideo(
        path=str(path),
        title=path.stem.replace("_", " ").replace("-", " ").strip(),
        source="local",
        source_id=hashlib.sha1(fingerprint).hexdigest()[:16],
    )


def _ydl_options(out_dir: Path) -> dict:
    height = settings.clip_source_max_height
    options: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(extractor_key)s_%(id)s.%(ext)s"),
    }
    if settings.proxy_url:
        options["proxy"] = settings.proxy_url
    cookies = YtDlpScraper._parse_browser_cookies(settings.cookies_from_browser or "")
    if cookies:
        options["cookiesfrombrowser"] = cookies[0] if isinstance(cookies[0], tuple) else (cookies[0],)
    return options


def _downloaded_path(info: dict) -> Optional[str]:
    for item in info.get("requested_downloads") or []:
        path = item.get("filepath")
        if path and os.path.exists(path):
            return path
    path = info.get("filepath") or info.get("_filename")
    return path if path and os.path.exists(path) else None


def _from_url(url: str) -> SourceVideo:
    import yt_dlp

    out_dir = settings.content_path / "long"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Look before downloading: noplaylist only covers watch?v=...&list= links, so a
    # channel link would otherwise download every video before being refused.
    options = {**_ydl_options(out_dir), "extract_flat": "in_playlist", "playlist_items": "1"}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise IngestError("download returned nothing")
            if info.get("entries") is not None:
                raise IngestError("that link is a playlist or channel; paste one video")
            if info.get("is_live"):
                raise IngestError("live streams are not supported")
            info = ydl.process_ie_result(info, download=True)
    except IngestError:
        raise
    except Exception as exc:
        if "confirm you" or "--cookies" in str(exc):
            raise IngestError(
                "YouTube wants a signed-in session for this download. Set "
                "COOKIES_FROM_BROWSER=chrome (or firefox) in .env, or download the video and pass its file path."
            ) from exc
        raise IngestError(f"download failed: {exc}") from exc
    path = _downloaded_path(info)
    if path is None:
        raise IngestError("download finished but the file is missing")
    # Cleanup ages downloads by mtime, and a reused file keeps its old one.
    os.utime(path, None)
    return SourceVideo(
        path=path,
        title=info.get("title") or "",
        description=(info.get("description") or "")[:4000],
        author=info.get("uploader_id") or info.get("channel_id") or "",
        author_name=info.get("uploader") or info.get("channel") or "",
        url=info.get("webpage_url") or url,
        source=(info.get("extractor_key") or "web").lower(),
        source_id=str(info.get("id") or hashlib.sha1(url.encode()).hexdigest()[:16]),
        keywords=list(info.get("tags") or [])[:20],
    )


def fetch(source: str) -> SourceVideo:
    source = source.strip()
    if not source:
        raise IngestError("no link or file given")
    return _from_url(source) if is_url(source) else _from_file(source)
