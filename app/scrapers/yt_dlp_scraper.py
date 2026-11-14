"""yt-dlp-based scraper with real video download.

Each scraped item has its local_path set to the downloaded mp4 (when download=True).
Supports TikTok, Instagram Reels, YouTube Shorts, and Reddit video — all via yt-dlp extractors.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, List

from .base import ScrapedItem, Scraper

log = logging.getLogger(__name__)

_MAX_EPOCH_SEC = 10_000_000_000
_KNOWN_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "reddit.com",
    "redd.it",
    "instagram.com",
    "tiktok.com",
    "snapchat.com",
)


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_int(value: Any, default: int = 0) -> int:
    parsed = _safe_float(value)
    return int(parsed) if parsed is not None else default


def _safe_utc_datetime(raw_ts: Any, upload_date: Any = None) -> datetime | None:
    ts = _safe_int(raw_ts, default=0)
    if ts > _MAX_EPOCH_SEC:
        ts //= 1000
    if 0 < ts <= _MAX_EPOCH_SEC:
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass
    if isinstance(upload_date, str):
        digits = upload_date.strip()
        if len(digits) == 8 and digits.isdigit():
            try:
                return datetime.strptime(digits, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def _safe_stem(raw: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "", raw or "").lstrip(".")
    if cleaned and cleaned == raw and len(cleaned) <= 60:
        return cleaned
    digest = hashlib.sha1((raw or "").encode("utf-8")).hexdigest()[:10]
    prefix = (cleaned[:45] or fallback).rstrip(".")
    return f"{prefix}_{digest}"


def _shorts_match_filter(info: dict) -> str | None:
    if not isinstance(info, dict):
        return None
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        return "live_or_upcoming_stream"

    raw_dur = info.get("duration")
    if raw_dur is not None:
        dur = _safe_float(raw_dur)
        if dur is None or not (8 <= int(dur) <= 59):
            return f"duration={raw_dur}s_not_in_shorts_range"

    raw_height = info.get("height")
    if raw_height is not None:
        height = _safe_int(raw_height, default=-1)
        if height < 0 or (0 < height < 360):
            return f"resolution={raw_height}p_not_in_shorts_range"

    return None


class YtDlpScraper(Scraper):
    def __init__(self) -> None:
        from ..config import settings

        self.proxy_url = (settings.proxy_url or "").strip() or None
        self.content_dir = str(settings.content_path)
        self.min_file_bytes = max(
            1, int(getattr(settings, "min_file_size_bytes", 50_000) or 50_000)
        )
        self.cookies_file = (settings.cookies_file or "").strip() or None
        self._cookiesfrombrowser = self._parse_browser_cookies(
            settings.cookies_from_browser or ""
        )
        self._yt = None

    @staticmethod
    def _parse_browser_cookies(raw: str):
        """Parse 'edge' or 'edge,Default' into yt-dlp's cookie list helper shape."""
        if not isinstance(raw, str):
            return None
        raw = raw.strip()
        if not raw:
            return None
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            return None
        browser = parts[0]
        profile = parts[1] if len(parts) > 1 else None
        if profile:
            return [(browser, profile)]
        return [browser]

    def _client(self):
        if self._yt is None:
            try:
                import yt_dlp
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("yt-dlp is not installed") from e
            self._yt = yt_dlp
        return self._yt

    def _ydl_opts(
        self,
        *,
        download: bool,
        outtmpl: str | None = None,
        playlistend: int | None = None,
    ) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": not download,
            "ignoreerrors": True,
            "noplaylist": bool(download),
            "retries": 2,
            "fragment_retries": 2,
            "geo_bypass": True,
            "socket_timeout": 20,
            # Prefer vertical MP4s (Shorts format); no filesize cap - WeMo
            "format": "best[height<=1920][ext=mp4]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "match_filter": _shorts_match_filter,
        }
        if playlistend is not None and playlistend > 0:
            opts["playlistend"] = int(playlistend)
        if download and outtmpl:
            opts["outtmpl"] = outtmpl
            opts["extract_audio"] = False
        if self.proxy_url:
            opts["proxy"] = self.proxy_url
        if (
            self.cookies_file
            and os.path.isfile(self.cookies_file)
            and os.path.getsize(self.cookies_file) > 0
        ):
            opts["cookiefile"] = self.cookies_file
        elif getattr(self, "_cookiesfrombrowser", None):
            spec = self._cookiesfrombrowser
            opts["cookiesfrombrowser"] = (
                spec[0] if isinstance(spec[0], tuple) else tuple(spec)
            )
        return opts

    def _to_item(self, info: dict, source: str) -> ScrapedItem:
        uploader = str(info.get("uploader") or info.get("channel") or "").strip()
        uploader_id = str(info.get("uploader_id") or "").strip()
        raw_author = (
            uploader_id
            if (uploader_id and not uploader_id.isdigit())
            else (uploader or uploader_id)
        )
        author = raw_author.lstrip("@").strip()
        author_name = uploader.lstrip("@").strip() if uploader else author

        dur_f = _safe_float(info.get("duration"))
        duration_sec = max(0, round(dur_f)) if dur_f is not None else 0

        raw_tags = info.get("tags")
        keywords: list[str] = []
        if isinstance(raw_tags, list):
            keywords = list(
                dict.fromkeys(
                    t.strip() for t in raw_tags if isinstance(t, str) and t.strip()
                )
            )[:20]

        raw_filepath = info.get("filepath")
        local_path = (
            raw_filepath
            if isinstance(raw_filepath, str) and os.path.isfile(raw_filepath)
            else None
        )

        published_at = _safe_utc_datetime(
            info.get("timestamp") or info.get("release_timestamp"),
            upload_date=info.get("upload_date"),
        )

        return ScrapedItem(
            source=source,
            source_id=str(info.get("id") or "").strip(),
            source_url=str(info.get("webpage_url") or info.get("url") or "").strip(),
            author=author,
            author_name=author_name,
            title=str(info.get("title") or "").strip(),
            description=str(info.get("description") or "")[:4000],
            thumbnail_url=info.get("thumbnail") if isinstance(info.get("thumbnail"), str) else None,
            local_path=local_path,
            views=max(0, _safe_int(info.get("view_count"))),
            likes=max(0, _safe_int(info.get("like_count"))),
            comments=max(0, _safe_int(info.get("comment_count"))),
            shares=max(0, _safe_int(info.get("repost_count"))),
            duration_sec=duration_sec,
            width=max(0, _safe_int(info.get("width"))),
            height=max(0, _safe_int(info.get("height"))),
            language=str(info.get("language") or "").strip(),
            keywords=keywords,
            published_at=published_at,
            follower_count=max(
                0,
                _safe_int(info.get("channel_follower_count") or info.get("follower_count")),
            ),
            meta={
                "extractor": info.get("extractor_key"),
                "uploader_id": info.get("uploader_id"),
                "upload_date": info.get("upload_date"),
            },
        )

    def _enumerate(self, url: str, max_results: int, source: str) -> List[ScrapedItem]:
        """List videos at a URL (no download). Returns ScrapedItems."""
        if max_results <= 0 or not (url or "").strip():
            return []
        ydl = self._client()
        entry_budget = max(2, max_results * 2)
        opts = self._ydl_opts(download=False, playlistend=entry_budget)
        items: List[ScrapedItem] = []
        seen_ids: set[str] = set()

        def collect(entry: dict) -> bool:
            item = self._to_item(entry, source)
            if item.source_id and item.source_id not in seen_ids:
                seen_ids.add(item.source_id)
                items.append(item)
            return len(seen_ids) >= max_results

        try:
            with ydl.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=False)
                if not isinstance(info, dict):
                    return []
                if "entries" not in info:
                    if info.get("id"):
                        collect(info)
                else:
                    inspected = 0
                    for e in info.get("entries") or []:
                        inspected += 1
                        if inspected > entry_budget * 2:
                            break
                        if not isinstance(e, dict):
                            continue
                        if "entries" in e:
                            for sub in e.get("entries") or []:
                                inspected += 1
                                if isinstance(sub, dict) and collect(sub):
                                    break
                                if inspected > entry_budget * 2:
                                    break
                            if len(seen_ids) >= max_results or inspected > entry_budget * 2:
                                break
                        elif collect(e):
                            break
        except Exception as e:
            log.error("yt-dlp enumerate failed for %r: %s", url, e)
        return self._dedup(items)[:max_results]

    @staticmethod
    def _resolve_downloaded_path(info: dict) -> str | None:
        for dl in info.get("requested_downloads") or []:
            if isinstance(dl, dict):
                fp = dl.get("filepath")
                if isinstance(fp, str) and os.path.isfile(fp):
                    return fp
        for key in ("filepath", "_filename"):
            fp = info.get(key)
            if isinstance(fp, str) and os.path.isfile(fp):
                return fp
        return None

    def _download(self, item: ScrapedItem) -> str | None:
        """Download a single video. Returns the local file path or None."""
        if not item or not (item.source_url or "").strip() or not (item.source_id or "").strip():
            return None
        os.makedirs(self.content_dir, exist_ok=True)
        safe_source = re.sub(r"[^A-Za-z0-9_-]", "", item.source or "") or "video"
        safe_id = _safe_stem(item.source_id, fallback="clip")
        cached_mp4 = os.path.join(self.content_dir, f"{safe_source}_{safe_id}.mp4")
        if os.path.isfile(cached_mp4) and os.path.getsize(cached_mp4) >= self.min_file_bytes:
            return cached_mp4

        ydl = self._client()
        outtmpl = os.path.join(self.content_dir, f"{safe_source}_{safe_id}.%(ext)s")
        opts = self._ydl_opts(download=True, outtmpl=outtmpl)
        try:
            with ydl.YoutubeDL(opts) as y:
                info = y.extract_info(item.source_url, download=True)
                if not isinstance(info, dict):
                    return None
                path = self._resolve_downloaded_path(info)
                if not path:
                    return None
                # Reject tiny stubs (some platforms serve HTML/redirect pages
                # that yt-dlp writes as 5-12 KB pseudo-mp4 files)
                size = os.path.getsize(path)
                if size < self.min_file_bytes:
                    log.warning(
                        "Rejected stub download for %r (only %d bytes)",
                        item.source_url, size,
                    )
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    return None
                return path
        except Exception as e:
            log.error("yt-dlp download failed for %r: %s", item.source_url, e)
            return None

    # ---------- Public scraper interface ----------
    def scrape_handles(self, handles: list[str], max_per_run: int,
                       download: bool = True) -> list[ScrapedItem]:
        if max_per_run <= 0:
            return []
        clean_handles = list(
            dict.fromkeys(
                h.strip() for h in (handles or []) if isinstance(h, str) and h.strip()
            )
        )
        if not clean_handles:
            return []
        out: list[ScrapedItem] = []
        for idx, h in enumerate(clean_handles):
            remaining = max_per_run - len(out)
            if remaining <= 0:
                break
            left = len(clean_handles) - idx
            per = max(1, (remaining + left - 1) // left)
            url = h if h.lower().startswith(("http://", "https://")) else self._handle_url(h)
            if not url:
                continue
            items = self._enumerate(url, per, source=self._platform_for_handle(h))
            if download:
                for it in items:
                    path = self._download(it)
                    if path:
                        it.local_path = path
            out.extend(items)
        return self._dedup(out)[:max_per_run]

    def scrape_hashtags(self, hashtags: list[str], max_per_run: int,
                        download: bool = True) -> list[ScrapedItem]:
        if max_per_run <= 0:
            return []
        clean_tags = list(
            dict.fromkeys(
                t.strip().lstrip("#").strip()
                for t in (hashtags or [])
                if isinstance(t, str) and t.strip().lstrip("#").strip()
            )
        )
        if not clean_tags:
            return []
        out: list[ScrapedItem] = []
        for idx, t in enumerate(clean_tags):
            remaining = max_per_run - len(out)
            if remaining <= 0:
                break
            left = len(clean_tags) - idx
            per = max(1, (remaining + left - 1) // left)
            # Use yt-dlp's search for cross-platform hashtag discovery.
            items = self._enumerate(f"ytsearch{per}:#{t} short", per, source="youtube")
            if download:
                for it in items:
                    path = self._download(it)
                    if path:
                        it.local_path = path
            out.extend(items)
        return self._dedup(out)[:max_per_run]

    # ---------- Platform helpers ----------
    def _platform_for_handle(self, handle: str) -> str:
        h = (handle if isinstance(handle, str) else "").strip().lower()
        if "instagram.com" in h or h.startswith("@"):
            return "instagram"
        if "snapchat.com" in h:
            return "snapchat"
        if "reddit.com" in h or "redd.it" in h or h.startswith(("/r/", "r/")):
            return "reddit"
        if "tiktok.com" in h:
            return "tiktok"
        return "youtube"

    def _handle_url(self, handle: str) -> str:
        raw = (handle if isinstance(handle, str) else "").strip()
        if not raw:
            return ""
        if raw.lower().startswith(("http://", "https://", "ytsearch")):
            return raw
        stripped = raw.strip("/")
        if stripped.startswith("@"):
            # Instagram/TikTok direct profiles are often blocked; use ytsearch
            name = stripped.lstrip("@").strip()
            return f"ytsearch5:{name} reels shorts" if name else ""
        if stripped.lower().startswith("r/"):
            sub = stripped[2:].strip("/")
            return f"https://www.reddit.com/r/{sub}" if sub else ""
        first_host = stripped.split("/", 1)[0].lower()
        is_domain = any(d in first_host for d in _KNOWN_DOMAINS) or (
            "." in first_host and "/" in stripped and " " not in stripped
        )
        if is_domain:
            return f"https://{stripped}"
        return f"ytsearch5:{stripped} reels shorts"
