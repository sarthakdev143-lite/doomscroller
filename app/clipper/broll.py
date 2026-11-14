"""Stock footage for cutaways, so a clip is not one person in a chair throughout.

Everything here is optional. No key, no results, a rate limit or a failed download
all just mean fewer cutaways, because the clip works without them.

Two libraries, whichever has a key: Pexels has true portrait clips, but had paused new
API keys as of late 2026. Pixabay's are mostly landscape and get centre-cropped by the render.
Both ask for a credit, which goes in the clip's description. Pixabay requires searches
to be cached for 24 hours, and Pexels treats working around its rate limit as grounds
for losing access, so a 429 stops the searches instead of retrying.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import httpx

from app.config import settings

from .media import MediaError, probe, read_ffmpeg

log = logging.getLogger(__name__)

# httpx logs every URL at INFO, and Pixabay's key travels in the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

PEXELS, PIXABAY = "Pexels", "Pixabay"
_PEXELS_URL = "https://api.pexels.com/videos/search"
_PIXABAY_URL = "https://pixabay.com/api/videos/"
_RESULTS_PER_SEARCH = 15
_MAX_QUERY_CHARS = 100
_MIN_FILE_WIDTH = 720
_IDEAL_FILE_WIDTH = 1080
# A 9:16 crop of 1080p keeps only 607 pixels of width, so 4K is worth having when it is small.
_MAX_PREFERRED_BYTES = 120_000_000
_MIN_LANDSCAPE_HEIGHT = 720
_MAX_DOWNLOAD_BYTES = 200_000_000
_SEARCH_CACHE_SEC = 24 * 3600
_TIMEOUT = httpx.Timeout(60, connect=15)
# Skip the first moment of a stock clip, which often has a fade or a shaky start.
_LEAD_IN_SEC = 0.4
_MAX_FAILED_DOWNLOADS = 2
# A hung CDN costs a minute per attempt, so give up across cutaways too. This counts
# files, because a cutaway may only have one to try once the AI has chosen.
MAX_FAILED_FILES = 4
_URL_KEY_RE = re.compile(r"([?&](?:key|api_key|apikey|token)=)[^&\s\"')]+", re.IGNORECASE)


def scrub_secrets(text: object) -> str:
    """Redact stock video API keys from any exception or log message."""
    cleaned = str(text or "")
    for secret in (settings.pixabay_api_key, settings.pexels_api_key):
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return _URL_KEY_RE.sub(r"\1[REDACTED]", cleaned)


def _safe_video_id(raw: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw or "")).strip("_")
    return cleaned or "stock"


class StockError(RuntimeError):
    """The stock library could not be used for this search."""


class SearchBlocked(StockError):
    """Every further search would fail the same way, so stop asking."""


class RateLimited(SearchBlocked):
    """Over the allowance. Further searches would only make it worse."""


class BadKey(SearchBlocked):
    """The key was refused, or cannot even be sent."""


class DownloadsFailed(StockError):
    """Searches work but the files will not download."""

    def __init__(self, message: str, files: int = _MAX_FAILED_DOWNLOADS):
        super().__init__(message)
        self.files = files


@dataclass
class StockSession:
    """What one job has learned about the stock library so far."""

    stopped: bool = False
    failed_files: int = 0
    picking_failed: bool = False


@dataclass(frozen=True)
class Candidate:
    """One search result, in the same shape from either library."""

    video_id: str
    link: str
    duration: float
    author: str = ""
    page_url: str = ""
    provider: str = PEXELS
    thumbnail: str = ""


@dataclass(frozen=True)
class StockClip:
    video_id: str
    path: str
    duration: float
    author: str = ""
    page_url: str = ""
    offset: float = 0.0
    provider: str = PEXELS


def provider() -> Optional[str]:
    """Pexels when there is a key, because its clips are already vertical."""
    if settings.pexels_api_key:
        return PEXELS
    return PIXABAY if settings.pixabay_api_key else None


def available() -> bool:
    return bool(settings.clip_broll and provider())


def best_file(video: dict) -> Optional[dict]:
    """The portrait MP4 nearest full HD. HLS playlists and small files are no use here."""
    usable = []
    files = video.get("video_files")
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict) or not item.get("link"):
            continue
        width, height = item.get("width"), item.get("height")
        if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
            continue
        if item.get("file_type") != "video/mp4" or width < _MIN_FILE_WIDTH or height <= width:
            continue
        usable.append(item)
    return min(usable, key=lambda f: abs((f.get("width") or 0) - _IDEAL_FILE_WIDTH), default=None)


def _number(raw: object) -> float:
    if isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value >= 0 else 0.0


def _pexels_candidates(videos: object) -> list[Candidate]:
    found = []
    for video in videos if isinstance(videos, list) else []:
        if not isinstance(video, dict) or not video.get("id"):
            continue
        chosen = best_file(video)
        if chosen is None:
            continue
        user = video.get("user") if isinstance(video.get("user"), dict) else {}
        found.append(Candidate(str(video["id"]), str(chosen["link"]), _number(video.get("duration")),
                               str(user.get("name") or ""), str(video.get("url") or ""), PEXELS,
                               str(video.get("image") or "")))
    return found


def best_rendition(hit: dict) -> Optional[dict]:
    """The sharpest Pixabay size that is not huge. The render crops it to vertical."""
    videos = hit.get("videos") if isinstance(hit.get("videos"), dict) else {}
    usable = []
    for name in ("large", "medium", "small", "tiny"):
        item = videos.get(name)
        if not isinstance(item, dict) or not item.get("url"):
            continue
        height, size = _number(item.get("height")), _number(item.get("size"))
        if height >= _MIN_LANDSCAPE_HEIGHT and size <= _MAX_PREFERRED_BYTES:
            usable.append(item)
    return usable[0] if usable else None


def _with_download_flag(link: str) -> str:
    return f"{link}{'&' if '?' in link else '?'}download=1"


def _smallest_thumbnail(hit: dict, chosen: dict) -> str:
    videos = hit.get("videos") if isinstance(hit.get("videos"), dict) else {}
    tiny = videos.get("tiny")
    link = tiny.get("thumbnail") if isinstance(tiny, dict) else None
    return str(link or chosen.get("thumbnail") or "")


def _pixabay_candidates(hits: object) -> list[Candidate]:
    found = []
    for hit in hits if isinstance(hits, list) else []:
        if not isinstance(hit, dict) or not hit.get("id") or hit.get("isLowQuality") or hit.get("isAiGenerated"):
            continue
        chosen = best_rendition(hit)
        if chosen is None:
            continue
        found.append(Candidate(f"pixabay-{hit['id']}", _with_download_flag(str(chosen["url"])),
                               _number(hit.get("duration")), str(hit.get("user") or ""),
                               str(hit.get("pageURL") or ""), PIXABAY, _smallest_thumbnail(hit, chosen)))
    return found


def _request(url: str, params: dict, headers: Optional[dict] = None) -> dict:
    """GET a library's JSON. Nothing raised from here may contain the key."""
    name = provider() or "the stock library"
    try:
        response = httpx.get(url, params=params, headers=headers or {}, timeout=_TIMEOUT)
    except UnicodeEncodeError:
        raise BadKey(f"the {name} API key has a stray character in it, so paste it again") from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise StockError(f"could not reach {name}") from None
    except StockError:
        raise
    except Exception:
        raise StockError(f"could not reach {name}") from None
    if response.status_code == 429:
        raise RateLimited(f"{name} rate limit reached")
    # Pixabay answers 400 for a wrong key.
    if response.status_code in (400, 401, 403):
        raise BadKey(f"{name} refused the API key ({response.status_code})")
    if response.status_code != 200:
        raise StockError(f"{name} returned {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise StockError(f"{name} sent an unreadable reply") from None


def _search_cache(library: str, query: str) -> Path:
    digest = hashlib.sha1(f"{library}|{query.lower()}".encode()).hexdigest()[:20]
    return settings.content_path / "broll" / f"search-{digest}.json"


def _read_search_cache(path: Path) -> Optional[object]:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(cached["at"]) < _SEARCH_CACHE_SEC:
            return cached["results"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_search_cache(path: Path, results: object) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "results": results}), encoding="utf-8")
    except OSError as exc:
        log.warning("broll: could not cache a search: %s", exc)


def search(query: str) -> list[Candidate]:
    library = provider()
    if library is None:
        raise StockError("no stock video API key is set")
    query = query[:_MAX_QUERY_CHARS]
    cache = _search_cache(library, query)
    cached = _read_search_cache(cache)
    if library == PEXELS:
        if cached is None:
            body = _request(_PEXELS_URL, {"query": query, "orientation": "portrait", "size": "medium",
                                          "per_page": _RESULTS_PER_SEARCH},
                            {"Authorization": settings.pexels_api_key})
            cached = body.get("videos") if isinstance(body, dict) else None
            if not isinstance(cached, list):
                raise StockError("Pexels sent an unreadable reply")
            _write_search_cache(cache, cached)
        return _pexels_candidates(cached)
    if cached is None:
        body = _request(_PIXABAY_URL, {"key": settings.pixabay_api_key, "q": query, "video_type": "film",
                                       "safesearch": "true", "per_page": _RESULTS_PER_SEARCH})
        cached = body.get("hits") if isinstance(body, dict) else None
        if not isinstance(cached, list):
            raise StockError("Pixabay sent an unreadable reply")
        _write_search_cache(cache, cached)
    return _pixabay_candidates(cached)


def _download(link: str, path: Path) -> None:
    """Fetch to a temporary name, so a dropped connection never leaves a bad clip in the cache."""
    partial = path.with_name(f"{path.name}.part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with httpx.stream("GET", link, timeout=_TIMEOUT, follow_redirects=True) as response:
            if response.status_code != 200:
                raise StockError(f"download returned {response.status_code}")
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > _MAX_DOWNLOAD_BYTES:
                        raise StockError("stock clip is too large")
                    handle.write(chunk)
        os.replace(partial, path)
    except StockError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
        raise StockError(f"download failed: {scrub_secrets(exc)}") from None
    finally:
        try:
            partial.unlink()
        except OSError:
            pass


def _playable(path: Path, seconds: float) -> bool:
    """Whether the part of the file a cutaway will show really decodes.

    A 200 response can carry an error page, and a dropped connection can leave a header
    that promises frames it does not have, so looking at the header is not enough.
    """
    try:
        probe(str(path))
        raw = read_ffmpeg(["-xerror", "-ss", f"{_LEAD_IN_SEC:.3f}", "-t", f"{seconds + 0.2:.3f}", "-i", str(path),
                           "-map", "0:v:0", "-an", "-vf", "fps=4,scale=8:8,format=gray", "-f", "rawvideo", "-"])
    except MediaError as exc:
        log.warning("broll: %s is not usable: %s", path.name, exc)
        return False
    return len(raw) // 64 >= int(seconds * 4)


def _local_copy(candidate: Candidate, seconds: float) -> Optional[Path]:
    """A checked file for this result, downloading it if needed. None if it will not play."""
    path = settings.content_path / "broll" / f"{_safe_video_id(candidate.video_id)}.mp4"
    if path.exists() and not _playable(path, seconds):
        path.unlink()
    if path.exists():
        os.utime(path, None)  # cleanup ages these by mtime
        return path
    _download(candidate.link, path)
    if _playable(path, seconds):
        return path
    path.unlink()
    return None


def fetch(query: str, seconds: float, *, exclude: Iterable[str] = (),
          choose: Optional[Callable[[list[Candidate]], list[Candidate]]] = None) -> Optional[StockClip]:
    """A local stock clip long enough for this cutaway, or None if nothing suits.

    `choose` gets the results long enough to use, and returns those worth trying, best first.
    """
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    skip = set(exclude)
    usable = [c for c in search(query) if c.video_id not in skip and c.duration >= seconds + _LEAD_IN_SEC]
    if choose is not None and usable:
        usable = choose(usable)
    failed = 0
    for candidate in usable:
        if failed >= _MAX_FAILED_DOWNLOADS:
            break
        try:
            path = _local_copy(candidate, seconds)
        except (StockError, OSError) as exc:
            log.warning("broll: %s", scrub_secrets(exc))
            failed += 1
            continue
        if path is None:
            failed += 1
            continue
        return StockClip(video_id=candidate.video_id, path=str(path), duration=candidate.duration,
                         author=candidate.author, page_url=candidate.page_url, offset=_LEAD_IN_SEC,
                         provider=candidate.provider)
    if failed:
        raise DownloadsFailed(f"{failed} stock downloads failed", failed)
    return None


def credit_line(authors: Iterable[str], library: str = PEXELS) -> str:
    """What the description says, as both libraries ask of API users."""
    names = list(dict.fromkeys(a for a in authors if a))
    return f"Stock video from {library}{': ' + ', '.join(names) if names else ''}."
