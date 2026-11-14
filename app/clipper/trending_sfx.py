"""Trending sound effects from Freesound into the local ``content/sfx/`` pack.

Only Creative Commons 0 sounds are kept, so clips need no attribution line and
the runtime `sfx` pack override picks them up unchanged. Token auth rides an
`Authorization` header (never the URL), searches are cached for 24 hours, and a
rate limit stops the job rather than burning quota. Without a key nothing is
downloaded; the synth still plays.
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

FREESOUND = "Freesound"
_SEARCH_URL = "https://freesound.org/apiv2/search/"
_RESULTS_PER_SEARCH = 15
_MAX_QUERY_CHARS = 100
_MAX_DOWNLOAD_BYTES = 25_000_000
_SEARCH_CACHE_SEC = 24 * 3600
_TIMEOUT = httpx.Timeout(60, connect=15)
_MAX_FAILED_DOWNLOADS = 2
# A hung CDN costs a minute per attempt, so give up across sounds too.
MAX_FAILED_FILES = 4
_CC0 = "Creative Commons 0"
_FIELDS = "id,name,username,license,previews,duration,type,url"
_FILTER = "type:(wav OR mp3 OR ogg OR flac)"


def scrub_secrets(text: object) -> str:
    """Redact the Freesound API key from any exception or log message."""
    cleaned = str(text or "")
    if settings.freesound_api_key:
        cleaned = cleaned.replace(settings.freesound_api_key, "[REDACTED]")
    return cleaned


def _safe_sfx_id(raw: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw or "")).strip("_")
    return cleaned or "sfx"


class SfxError(RuntimeError):
    """The SFX library could not be used for this lookup."""


class SearchBlocked(SfxError):
    """Every further lookup would fail the same way, so stop asking."""


class RateLimited(SearchBlocked):
    """Over the allowance. Further lookups would only make it worse."""


class BadKey(SearchBlocked):
    """The key was refused, or cannot even be sent."""


class DownloadsFailed(SfxError):
    """Searches work but the files will not download."""

    def __init__(self, message: str, files: int = _MAX_FAILED_DOWNLOADS):
        super().__init__(message)
        self.files = files


@dataclass
class SfxSession:
    """What one fetch run has learned about the SFX library so far."""

    stopped: bool = False
    failed_files: int = 0


@dataclass(frozen=True)
class Candidate:
    """One CC0 search result with a downloadable preview."""

    sfx_id: str
    link: str
    title: str = ""
    author: str = ""
    license: str = ""
    page_url: str = ""
    provider: str = FREESOUND


@dataclass(frozen=True)
class SfxClip:
    sfx_id: str
    path: str
    title: str = ""
    author: str = ""
    license: str = ""
    provider: str = FREESOUND


def provider() -> Optional[str]:
    """Freesound when there is a key."""
    return FREESOUND if settings.freesound_api_key else None


def available() -> bool:
    return bool(settings.clip_trending_sfx and provider())


def _headers() -> dict:
    return {"Authorization": f"Token {settings.freesound_api_key}"}


def _request(url: str, params: dict) -> dict:
    """GET the SFX library's JSON. Nothing raised from here may contain the key."""
    name = provider() or "the SFX library"
    try:
        response = httpx.get(url, params=params, headers=_headers(), timeout=_TIMEOUT)
    except (httpx.HTTPError, httpx.InvalidURL):
        raise SfxError(f"could not reach {name}") from None
    except SfxError:
        raise
    except Exception:
        raise SfxError(f"could not reach {name}") from None
    if response.status_code == 429:
        raise RateLimited(f"{name} rate limit reached")
    if response.status_code in (401, 403):
        raise BadKey(f"{name} refused the API key ({response.status_code})")
    if response.status_code != 200:
        raise SfxError(f"{name} returned {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise SfxError(f"{name} sent an unreadable reply") from None
    return body if isinstance(body, dict) else {}


def _sfx_results(data: object) -> list[Candidate]:
    """CC0 results with a preview first; Attribution sounds are dropped (no credit plumbing)."""
    found = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if str(item.get("license") or "") != _CC0:
            continue
        previews = item.get("previews") if isinstance(item.get("previews"), dict) else {}
        link = str(previews.get("preview-hq-mp3") or previews.get("preview-hq-ogg") or "")
        if not link:
            continue
        found.append(Candidate(f"freesound-{item['id']}", link, str(item.get("name") or ""),
                               str(item.get("username") or ""), _CC0,
                               str(item.get("url") or ""), FREESOUND))
    return found


def _search_cache(kind: str, query: str) -> Path:
    digest = hashlib.sha1(f"sfx|{kind}|{query.lower()}".encode()).hexdigest()[:20]
    return settings.content_path / "sfx" / f"freesound-{digest}.json"


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
        log.warning("sfx: could not cache a search: %s", exc)


def _lookup(query: str, sort: str, limit: int) -> list[Candidate]:
    if provider() is None:
        raise SfxError("no SFX API key is set")
    count = max(1, min(_RESULTS_PER_SEARCH, int(limit)))
    cache = _search_cache(sort, query or "trending")
    cached = _read_search_cache(cache)
    if cached is None:
        body = _request(_SEARCH_URL, {"query": query, "sort": sort, "filter": _FILTER,
                                      "fields": _FIELDS, "group_by_pack": "0",
                                      "page_size": count})
        cached = body.get("results")
        if not isinstance(cached, list):
            raise SfxError(f"{FREESOUND} sent an unreadable reply")
        _write_search_cache(cache, cached)
    return _sfx_results(cached)[:count]


def search(query: str, limit: int = _RESULTS_PER_SEARCH) -> list[Candidate]:
    return _lookup(query[:_MAX_QUERY_CHARS], "score", limit)


def trending(limit: int = _RESULTS_PER_SEARCH) -> list[Candidate]:
    """Most-downloaded short sounds: the closest thing to a trending SFX chart."""
    return _lookup("", "downloads_desc", limit)


def _download(link: str, path: Path) -> None:
    """Fetch to a temporary name, so a dropped connection never leaves a bad file in the pack."""
    partial = path.with_name(f"{path.name}.part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with httpx.stream("GET", link, timeout=_TIMEOUT, follow_redirects=True) as response:
            if response.status_code != 200:
                raise SfxError(f"download returned {response.status_code}")
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > _MAX_DOWNLOAD_BYTES:
                        raise SfxError("SFX file is too large")
                    handle.write(chunk)
        os.replace(partial, path)
    except SfxError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
        raise SfxError(f"download failed: {scrub_secrets(exc)}") from None
    finally:
        try:
            partial.unlink()
        except OSError:
            pass


def _playable(path: Path, seconds: float) -> bool:
    """Whether the file really decodes, so a 200 error page never lands in the pack."""
    try:
        probe(str(path))
        raw = read_ffmpeg(["-xerror", "-t", f"{seconds + 0.2:.3f}", "-i", str(path),
                           "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"])
    except MediaError as exc:
        log.warning("sfx: %s is not usable: %s", path.name, exc)
        return False
    return len(raw) // 4 >= int(48000 * min(seconds, 2.0))


def _local_copy(candidate: Candidate, seconds: float) -> Optional[Path]:
    """A checked file for this result, downloading it if needed. None if it will not play."""
    raw = candidate.link.split("?")[0].lower()
    suffix = next((e for e in (".mp3", ".ogg", ".wav", ".flac") if raw.endswith(e)), ".mp3")
    path = settings.content_path / "sfx" / f"{_safe_sfx_id(candidate.sfx_id)}{suffix}"
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
          choose: Optional[Callable[[list[Candidate]], list[Candidate]]] = None) -> Optional[SfxClip]:
    """A local CC0 SFX file for this slot, or None if nothing suits.

    An empty query means the most-downloaded pack, rotated through `exclude`.
    `choose` gets the results worth trying, best first.
    """
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    skip = set(exclude)
    try:
        found = search(query, _RESULTS_PER_SEARCH) if query else trending()
    except SearchBlocked:
        raise
    except SfxError:
        return None
    usable = [c for c in found if c.sfx_id not in skip and c.link]
    if choose is not None and usable:
        usable = choose(usable)
    failed = 0
    for candidate in usable:
        if failed >= _MAX_FAILED_DOWNLOADS:
            break
        try:
            path = _local_copy(candidate, seconds)
        except (SfxError, OSError) as exc:
            log.warning("sfx: %s", scrub_secrets(exc))
            failed += 1
            continue
        if path is None:
            failed += 1
            continue
        return SfxClip(sfx_id=candidate.sfx_id, path=str(path), title=candidate.title,
                       author=candidate.author, license=candidate.license, provider=candidate.provider)
    if failed:
        raise DownloadsFailed(f"{failed} SFX downloads failed", failed)
    return None
