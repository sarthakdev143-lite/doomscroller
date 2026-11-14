"""Trending meme/GIF overlays, so a clip can flash a reaction shot on a punch beat.

Everything here is optional. No key, no results, a rate limit or a failed
download all just mean fewer overlays, because the clip works without them.

Tenor is preferred and Giphy is the fallback; both hand back an MP4 rendition
(ffmpeg overlays those with no loop handling, unlike GIFs). Searches are cached
for 24 hours like stock footage, and the key travels in the URL, so the httpx
logger stays at WARNING and errors must never include the URL.
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

# httpx logs every URL at INFO, and the meme keys travel in the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)

TENOR, GIPHY = "Tenor", "Giphy"
_TENOR_FEATURED_URL = "https://tenor.googleapis.com/v2/featured"
_TENOR_SEARCH_URL = "https://tenor.googleapis.com/v2/search"
_GIPHY_TRENDING_URL = "https://api.giphy.com/v1/gifs/trending"
_GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
_RESULTS_PER_SEARCH = 15
_MAX_QUERY_CHARS = 100
_MAX_DOWNLOAD_BYTES = 25_000_000
_SEARCH_CACHE_SEC = 24 * 3600
_TIMEOUT = httpx.Timeout(60, connect=15)
_MAX_FAILED_DOWNLOADS = 2
# A hung CDN costs a minute per attempt, so give up across overlays too.
MAX_FAILED_FILES = 4
_URL_KEY_RE = re.compile(r"([?&](?:key|api_key|apikey|token)=)[^&\s\"')]+", re.IGNORECASE)


def scrub_secrets(text: object) -> str:
    """Redact meme API keys from any exception or log message."""
    cleaned = str(text or "")
    for secret in (settings.tenor_api_key, settings.giphy_api_key):
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return _URL_KEY_RE.sub(r"\1[REDACTED]", cleaned)


def _safe_meme_id(raw: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw or "")).strip("_")
    return cleaned or "meme"


class MemeError(RuntimeError):
    """The meme library could not be used for this lookup."""


class SearchBlocked(MemeError):
    """Every further lookup would fail the same way, so stop asking."""


class RateLimited(SearchBlocked):
    """Over the allowance. Further lookups would only make it worse."""


class BadKey(SearchBlocked):
    """The key was refused, or cannot even be sent."""


class DownloadsFailed(MemeError):
    """Searches work but the files will not download."""

    def __init__(self, message: str, files: int = _MAX_FAILED_DOWNLOADS):
        super().__init__(message)
        self.files = files


@dataclass
class MemeSession:
    """What one job has learned about the meme library so far."""

    stopped: bool = False
    failed_files: int = 0


@dataclass(frozen=True)
class Candidate:
    """One search result, in the same shape from either library."""

    meme_id: str
    link: str
    title: str = ""
    page_url: str = ""
    provider: str = TENOR


@dataclass(frozen=True)
class MemeClip:
    meme_id: str
    path: str
    title: str = ""
    provider: str = TENOR


def provider() -> Optional[str]:
    """Tenor when there is a key, else Giphy."""
    if settings.tenor_api_key:
        return TENOR
    return GIPHY if settings.giphy_api_key else None


def available() -> bool:
    return bool(settings.clip_memes and provider())


def _tenor_memes(results: object) -> list[Candidate]:
    found = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        formats = item.get("media_formats") if isinstance(item.get("media_formats"), dict) else {}
        link = ""
        for name in ("mp4", "tinymp4", "gif", "tinygif", "nanogif"):
            entry = formats.get(name)
            if isinstance(entry, dict) and entry.get("url"):
                link = str(entry["url"])
                break
        if not link:
            continue
        title = str(item.get("title") or item.get("content_description") or "")
        found.append(Candidate(f"tenor-{item['id']}", link, title,
                               str(item.get("itemurl") or ""), TENOR))
    return found


def _giphy_memes(data: object) -> list[Candidate]:
    found = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        images = item.get("images") if isinstance(item.get("images"), dict) else {}
        link = ""
        for name in ("original_mp4", "downsized_small_mp4"):
            entry = images.get(name)
            if isinstance(entry, dict) and entry.get("mp4"):
                link = str(entry["mp4"])
                break
        if not link:
            for name in ("original", "downsized", "fixed_height"):
                entry = images.get(name)
                if isinstance(entry, dict) and entry.get("url"):
                    link = str(entry["url"])
                    break
        if not link:
            continue
        found.append(Candidate(f"giphy-{item['id']}", link, str(item.get("title") or ""), "", GIPHY))
    return found


def _request(url: str, params: dict) -> dict:
    """GET a meme library's JSON. Nothing raised from here may contain the key."""
    name = provider() or "the meme library"
    try:
        response = httpx.get(url, params=params, timeout=_TIMEOUT)
    except UnicodeEncodeError:
        raise BadKey(f"the {name} API key has a stray character in it, so paste it again") from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise MemeError(f"could not reach {name}") from None
    except MemeError:
        raise
    except Exception:
        raise MemeError(f"could not reach {name}") from None
    if response.status_code == 429:
        raise RateLimited(f"{name} rate limit reached")
    if response.status_code in (400, 401, 403):
        raise BadKey(f"{name} refused the API key ({response.status_code})")
    if response.status_code != 200:
        raise MemeError(f"{name} returned {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise MemeError(f"{name} sent an unreadable reply") from None
    return body if isinstance(body, dict) else {}


def _search_cache(library: str, query: str) -> Path:
    digest = hashlib.sha1(f"meme|{library}|{query.lower()}".encode()).hexdigest()[:20]
    return settings.content_path / "memes" / f"meme-{digest}.json"


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
        log.warning("memes: could not cache a search: %s", exc)


def search(query: str) -> list[Candidate]:
    library = provider()
    if library is None:
        raise MemeError("no meme API key is set")
    query = query[:_MAX_QUERY_CHARS]
    cache = _search_cache(library, f"q:{query}")
    cached = _read_search_cache(cache)
    if cached is None:
        if library == TENOR:
            body = _request(_TENOR_SEARCH_URL, {"key": settings.tenor_api_key, "client_key": "doomscroller",
                                                "q": query, "limit": _RESULTS_PER_SEARCH,
                                                "media_filter": "mp4,tinymp4,gif", "contentfilter": "high"})
            cached = body.get("results")
        else:
            body = _request(_GIPHY_SEARCH_URL, {"api_key": settings.giphy_api_key, "q": query,
                                               "limit": _RESULTS_PER_SEARCH, "rating": "pg"})
            cached = body.get("data")
        if not isinstance(cached, list):
            raise MemeError(f"{library} sent an unreadable reply")
        _write_search_cache(cache, cached)
    return _tenor_memes(cached) if library == TENOR else _giphy_memes(cached)


def trending(limit: int = _RESULTS_PER_SEARCH) -> list[Candidate]:
    """The current featured/trending memes, fresh through the day."""
    library = provider()
    if library is None:
        raise MemeError("no meme API key is set")
    count = max(1, min(_RESULTS_PER_SEARCH, int(limit)))
    cache = _search_cache(library, "trending")
    cached = _read_search_cache(cache)
    if cached is None:
        if library == TENOR:
            body = _request(_TENOR_FEATURED_URL, {"key": settings.tenor_api_key, "client_key": "doomscroller",
                                                 "limit": count, "media_filter": "mp4,tinymp4,gif",
                                                 "contentfilter": "high"})
            cached = body.get("results")
        else:
            body = _request(_GIPHY_TRENDING_URL, {"api_key": settings.giphy_api_key,
                                                 "limit": count, "rating": "pg"})
            cached = body.get("data")
        if not isinstance(cached, list):
            raise MemeError(f"{library} sent an unreadable reply")
        _write_search_cache(cache, cached)
    found = _tenor_memes(cached) if library == TENOR else _giphy_memes(cached)
    return found[:count]


def _download(link: str, path: Path) -> None:
    """Fetch to a temporary name, so a dropped connection never leaves a bad file in the cache."""
    partial = path.with_name(f"{path.name}.part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with httpx.stream("GET", link, timeout=_TIMEOUT, follow_redirects=True) as response:
            if response.status_code != 200:
                raise MemeError(f"download returned {response.status_code}")
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > _MAX_DOWNLOAD_BYTES:
                        raise MemeError("meme file is too large")
                    handle.write(chunk)
        os.replace(partial, path)
    except MemeError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
        raise MemeError(f"download failed: {scrub_secrets(exc)}") from None
    finally:
        try:
            partial.unlink()
        except OSError:
            pass


def _playable(path: Path, seconds: float) -> bool:
    """Whether the part of the file an overlay will show really decodes.

    A 200 response can carry an error page, and a dropped connection can leave a header
    that promises frames it does not have, so looking at the header is not enough.
    """
    try:
        probe(str(path))
        raw = read_ffmpeg(["-xerror", "-t", f"{seconds + 0.2:.3f}", "-i", str(path),
                           "-map", "0:v:0", "-an", "-vf", "fps=4,scale=8:8,format=gray",
                           "-f", "rawvideo", "-"])
    except MediaError as exc:
        log.warning("memes: %s is not usable: %s", path.name, exc)
        return False
    return len(raw) // 64 >= int(seconds * 4)


def _local_copy(candidate: Candidate, seconds: float) -> Optional[Path]:
    """A checked file for this result, downloading it if needed. None if it will not play."""
    suffix = ".mp4" if candidate.link.split("?")[0].lower().endswith(".mp4") else ".gif"
    path = settings.content_path / "memes" / f"{_safe_meme_id(candidate.meme_id)}{suffix}"
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
          choose: Optional[Callable[[list[Candidate]], list[Candidate]]] = None) -> Optional[MemeClip]:
    """A local meme file for this overlay, or None if nothing suits.

    An empty query means the trending pack, rotated through `exclude`.
    `choose` gets the results worth trying, best first.
    """
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    skip = set(exclude)
    try:
        found = search(query) if query else trending()
    except SearchBlocked:
        raise
    except MemeError:
        return None
    usable = [c for c in found if c.meme_id not in skip and c.link]
    if choose is not None and usable:
        usable = choose(usable)
    failed = 0
    for candidate in usable:
        if failed >= _MAX_FAILED_DOWNLOADS:
            break
        try:
            path = _local_copy(candidate, seconds)
        except (MemeError, OSError) as exc:
            log.warning("memes: %s", scrub_secrets(exc))
            failed += 1
            continue
        if path is None:
            failed += 1
            continue
        return MemeClip(meme_id=candidate.meme_id, path=str(path),
                        title=candidate.title, provider=candidate.provider)
    if failed:
        raise DownloadsFailed(f"{failed} meme downloads failed", failed)
    return None


def credit_line(titles: Iterable[str], library: str = TENOR) -> str:
    """What the description says, as both libraries ask of API users."""
    names = list(dict.fromkeys(t for t in titles if t))[:3]
    return f"Meme from {library}{': ' + ', '.join(names) if names else '.'}"
