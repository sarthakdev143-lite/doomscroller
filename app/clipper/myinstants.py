"""MyInstants public soundboard SFX fetcher with ffprobe duration gate (0.15s..6.0s).

Keeps zero numpy dependency at import time so idle server startup stays light.
Stops cleanly (raises RuntimeError) if a CAPTCHA or bot challenge is encountered.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.winproc import background_subprocess_kwargs

log = logging.getLogger(__name__)

BASE_URL = "https://www.myinstants.com"
INDEX_URLS = {
    "in": f"{BASE_URL}/en/index/in/",
    "us": f"{BASE_URL}/en/index/us/",
}
SEARCH_URL = f"{BASE_URL}/en/search/"

EN_QUERIES: tuple[str, ...] = (
    "vine boom",
    "bruh",
    "emotional damage",
    "metal pipe",
    "record scratch",
    "cartoon slip",
    "sad violin",
    "whoosh",
    "wow",
    "suspense",
)
HI_QUERIES: tuple[str, ...] = (
    "aayein",
    "are baap re",
    "kuch bhi",
    "control uday",
    "bhai kya kar raha",
    "yeh kya ho raha hai",
    "khatam tata bye bye",
    "paisa hi paisa",
    "moye moye",
    "system",
)

MIN_DURATION_SEC = 0.15
MAX_DURATION_SEC = 6.0
_MIN_BYTES = 500
_MAX_BYTES = 5_000_000
_TIMEOUT = httpx.Timeout(25.0, connect=10.0)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_CHALLENGE_MARKERS = (
    "cf-challenge",
    "cf-turnstile",
    "g-recaptcha",
    "hcaptcha",
    "verify you are human",
    "attention required! | cloudflare",
    "<title>just a moment",
)

_BUTTON_RE = re.compile(r"<button\b([^>]*)>", re.IGNORECASE | re.DOTALL)
_ONCLICK_PLAY_RE = re.compile(
    r"""play\(\s*(['"])(/media/sounds/[^'"\s)]+\.(?:mp3|wav))(?:\?[^'"]*)?\1(?:\s*,\s*(['"])[^'"]*\3\s*(?:,\s*(['"])([^'"]*)\4)?)?""",
    re.IGNORECASE,
)
_TITLE_ATTR_RE = re.compile(r"""\btitle\s*=\s*(['"])(.*?)\1""", re.IGNORECASE | re.DOTALL)


def _pack_root(pack_root: Optional[Path] = None) -> Path:
    if pack_root is not None:
        return Path(pack_root)
    configured = (settings.clip_sfx_pack_dir or "").strip()
    return Path(configured).expanduser() if configured else settings.content_path / "sfx"


def _check_challenge(status_code: int, body: str) -> None:
    low = (body or "").lower()
    if status_code == 200 and "/media/sounds/" in low:
        return
    if any(marker in low for marker in _CHALLENGE_MARKERS):
        raise RuntimeError("MyInstants CAPTCHA/challenge page detected; stopping cleanly")
    if status_code in (403, 429, 503) and ("captcha" in low or "cloudflare" in low or "challenge" in low):
        raise RuntimeError(f"MyInstants blocked request with HTTP {status_code}; stopping cleanly")


def _clean_title(raw_title: str, fallback_slug: str, sound_path: str) -> str:
    text = html.unescape(raw_title or "").strip()
    text = re.sub(r"^play\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+sound$", "", text, flags=re.IGNORECASE).strip()
    if text:
        return text
    if fallback_slug:
        return re.sub(r"-\d+$", "", fallback_slug).replace("-", " ").strip()
    return Path(sound_path).stem


def parse_buttons(page_html: str) -> list[tuple[str, str]]:
    """Extract unique ``(title, '/media/sounds/...')`` pairs from MyInstants HTML."""
    results: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for match in _BUTTON_RE.finditer(page_html or ""):
        attrs = match.group(1)
        play_m = _ONCLICK_PLAY_RE.search(attrs)
        if not play_m:
            continue
        sound_path = play_m.group(2).strip()
        if sound_path in seen_paths:
            continue
        title_m = _TITLE_ATTR_RE.search(attrs)
        raw_title = title_m.group(2) if title_m else ""
        slug_arg = play_m.group(5) or ""
        title = _clean_title(raw_title, slug_arg, sound_path)
        seen_paths.add(sound_path)
        results.append((title, sound_path))
    return results


def slugify_stem(title: str, sound_path: str = "") -> str:
    """Normalize a button title into a safe lowercase filename stem."""
    raw = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if not raw and sound_path:
        raw = re.sub(r"[^a-z0-9]+", "-", Path(sound_path).stem.lower()).strip("-")
    raw = re.sub(r"-{2,}", "-", raw)[:60].strip("-")
    return raw or "sfx"


def probe_audio_duration(path: Path) -> Optional[float]:
    """Return audio duration in seconds via ffprobe, or None if invalid/unreadable."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            **background_subprocess_kwargs(low_priority=True),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    streams = payload.get("streams") or []
    if not any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams):
        return None
    raw_dur = (payload.get("format") or {}).get("duration")
    try:
        dur = float(raw_dur)
    except (TypeError, ValueError):
        return None
    return dur if dur > 0 else None


def fetch_page_sounds(
    url: str,
    *,
    params: Optional[dict] = None,
    client: Optional[httpx.Client] = None,
) -> list[tuple[str, str]]:
    """Fetch a MyInstants index or search page and extract sound buttons."""
    owns_client = client is None
    http = client or httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
    try:
        resp = http.get(url, params=params)
        _check_challenge(resp.status_code, resp.text)
        if resp.status_code != 200:
            log.warning("myinstants: %s returned HTTP %d", url, resp.status_code)
            return []
        return parse_buttons(resp.text)
    except RuntimeError:
        raise
    except httpx.HTTPError as exc:
        log.warning("myinstants: request error for %s: %s", url, exc)
        return []
    finally:
        if owns_client:
            http.close()


def _sync_copy(src: Path, sync_dir: Optional[Path]) -> None:
    if sync_dir is None or not src.is_file():
        return
    sync_dir.mkdir(parents=True, exist_ok=True)
    target = sync_dir / src.name
    if target.is_file() and target.stat().st_size == src.stat().st_size:
        return
    tmp = sync_dir / f".{src.name}.part"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def download_sound(
    sound_path: str,
    title: str,
    dest_dir: Path,
    *,
    sync_dir: Optional[Path] = None,
    client: Optional[httpx.Client] = None,
) -> Optional[Path]:
    """Download one clip, verify 0.15s <= duration <= 6.0s via ffprobe, and store cleanly."""
    suffix = ".wav" if sound_path.lower().endswith(".wav") else ".mp3"
    filename = f"{slugify_stem(title, sound_path)}{suffix}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if dest.is_file() and dest.stat().st_size >= _MIN_BYTES:
        _sync_copy(dest, sync_dir)
        return dest

    url = sound_path if sound_path.startswith(("http://", "https://")) else urljoin(BASE_URL, sound_path)
    part = dest_dir / f".{filename}.part"
    owns_client = client is None
    http = client or httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
    try:
        written = 0
        with http.stream("GET", url) as resp:
            if resp.status_code in (403, 429, 503):
                body = resp.read().decode("utf-8", errors="replace")
                _check_challenge(resp.status_code, body)
            if resp.status_code != 200:
                return None
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype.startswith("text/html"):
                _check_challenge(resp.status_code, resp.read().decode("utf-8", errors="replace"))
                return None
            with open(part, "wb") as handle:
                for chunk in resp.iter_bytes():
                    written += len(chunk)
                    if written > _MAX_BYTES:
                        return None
                    handle.write(chunk)
        if written < _MIN_BYTES:
            return None
        dur = probe_audio_duration(part)
        if dur is None or not (MIN_DURATION_SEC <= dur <= MAX_DURATION_SEC):
            log.info("myinstants: rejected %s (duration=%s)", filename, dur)
            return None
        os.replace(part, dest)
        _sync_copy(dest, sync_dir)
        return dest
    except RuntimeError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        log.warning("myinstants: download failed for %s: %s", url, exc)
        return None
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        if owns_client:
            http.close()


def fetch_bucket(
    lang: str,
    *,
    limit: int = 15,
    extra_searches: Sequence[str] = (),
    pack_root: Optional[Path] = None,
    client: Optional[httpx.Client] = None,
) -> dict:
    """Populate ``content/sfx/<lang>/`` (and ``trending/`` for English) up to ``limit`` files."""
    code = "hi" if str(lang).strip().lower().startswith("hi") else "en"
    root = _pack_root(pack_root)
    dest_dir = root / code
    sync_dir = (root / "trending") if code == "en" else None
    index_url = INDEX_URLS["in" if code == "hi" else "us"]
    queries = list(HI_QUERIES if code == "hi" else EN_QUERIES) + [q for q in extra_searches if q]

    owns_client = client is None
    http = client or httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
    downloaded: list[str] = []
    skipped = 0
    seen_paths: set[str] = set()
    candidates: list[tuple[str, str]] = []
    try:
        for query in queries:
            for item in fetch_page_sounds(SEARCH_URL, params={"name": query}, client=http):
                if item[1] not in seen_paths:
                    seen_paths.add(item[1])
                    candidates.append(item)
                    break  # top matching sound per curated query first
        for item in fetch_page_sounds(index_url, client=http):
            if item[1] not in seen_paths:
                seen_paths.add(item[1])
                candidates.append(item)

        for title, sound_path in candidates:
            if len(downloaded) >= limit:
                break
            saved = download_sound(sound_path, title, dest_dir, sync_dir=sync_dir, client=http)
            if saved is None:
                skipped += 1
            else:
                downloaded.append(str(saved))
    finally:
        if owns_client:
            http.close()

    return {
        "lang": code,
        "dir": str(dest_dir),
        "downloaded": len(downloaded),
        "skipped": skipped,
        "files": [Path(p).name for p in downloaded],
    }


def fetch_all(
    *,
    limit: int = 15,
    lang: str = "both",
    searches: Sequence[str] = (),
    pack_root: Optional[Path] = None,
    client: Optional[httpx.Client] = None,
) -> dict:
    """Fetch English and/or Hindi meme SFX from MyInstants into ``content/sfx/``."""
    if not 1 <= limit <= 50:
        raise ValueError(f"limit must be 1..50, got {limit}")
    norm_lang = (lang or "both").strip().lower()
    if norm_lang not in ("en", "hi", "both"):
        raise ValueError(f"lang must be en, hi, or both, got {lang!r}")
    langs = ("en", "hi") if norm_lang == "both" else (norm_lang,)
    buckets = [
        fetch_bucket(code, limit=limit, extra_searches=searches, pack_root=pack_root, client=client)
        for code in langs
    ]
    all_files = sorted({f for b in buckets for f in b["files"]})
    return {
        "ok": True,
        "provider": "MyInstants",
        "lang": norm_lang,
        "downloaded": sum(b["downloaded"] for b in buckets),
        "skipped": sum(b["skipped"] for b in buckets),
        "files": all_files,
        "buckets": {b["lang"]: b for b in buckets},
    }
