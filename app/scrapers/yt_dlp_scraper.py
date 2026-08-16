"""yt-dlp-based scraper with real video download.

Each scraped item has its local_path set to the downloaded mp4 (when download=True).
Supports TikTok, Instagram Reels, and Reddit video — all via yt-dlp extractors.
"""
from __future__ import annotations

import logging
import os
from typing import List

from .base import ScrapedItem, Scraper

log = logging.getLogger(__name__)


class YtDlpScraper(Scraper):
    def __init__(self) -> None:
        from ..config import settings
        self.proxy_url = settings.proxy_url or None
        self.content_dir = settings.content_path
        self._yt = None

    def _client(self):
        if self._yt is None:
            try:
                import yt_dlp
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("yt-dlp is not installed") from e
            self._yt = yt_dlp
        return self._yt

    def _ydl_opts(self, *, download: bool, outtmpl: str | None = None) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": not download,
            "ignoreerrors": True,
            "retries": 1,
            "fragment_retries": 1,
            "geo_bypass": True,
            "socket_timeout": 20,
            # Prefer vertical MP4s (Shorts format); no filesize cap - WeMo
            "format": "best[height<=1920][ext=mp4]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
        }
        if download and outtmpl:
            opts["outtmpl"] = outtmpl
            opts["extract_audio"] = False
        if self.proxy_url:
            opts["proxy"] = self.proxy_url
        return opts

    def _to_item(self, info: dict, source: str) -> ScrapedItem:
        return ScrapedItem(
            source=source,
            source_id=str(info.get("id") or ""),
            source_url=info.get("webpage_url") or info.get("url") or "",
            author=info.get("uploader") or info.get("channel") or "",
            title=info.get("title") or "",
            description=(info.get("description") or "")[:4000],
            thumbnail_url=info.get("thumbnail"),
            local_path=info.get("filepath"),  # set when download=True
            views=int(info.get("view_count") or 0),
            likes=int(info.get("like_count") or 0),
            comments=int(info.get("comment_count") or 0),
            shares=int(info.get("repost_count") or 0),
            duration_sec=int(info.get("duration") or 0),
            meta={
                "extractor": info.get("extractor_key"),
                "uploader_id": info.get("uploader_id"),
                "upload_date": info.get("upload_date"),
            },
        )

    def _enumerate(self, url: str, max_results: int, source: str) -> List[ScrapedItem]:
        """List videos at a URL (no download). Returns ScrapedItems."""
        ydl = self._client()
        opts = self._ydl_opts(download=False)
        items: List[ScrapedItem] = []
        try:
            with ydl.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=False)
                entries = (info or {}).get("entries") or []
                for e in entries:
                    if not e:
                        continue
                    if "entries" in e:
                        for sub in e["entries"]:
                            if sub:
                                items.append(self._to_item(sub, source))
                    else:
                        items.append(self._to_item(e, source))
        except Exception as e:
            log.error("yt-dlp enumerate failed for %r: %s", url, e)
        return self._dedup(items)[:max_results]

    def _download(self, item: ScrapedItem) -> str | None:
        """Download a single video. Returns the local file path or None."""
        ydl = self._client()
        outtmpl = os.path.join(self.content_dir, f"{item.source}_{item.source_id}.%(ext)s")
        opts = self._ydl_opts(download=True, outtmpl=outtmpl)
        try:
            with ydl.YoutubeDL(opts) as y:
                info = y.extract_info(item.source_url, download=True)
                if not info:
                    return None
                path = info.get("requested_downloads", [{}])[0].get("filepath") if info.get("requested_downloads") else info.get("filepath")
                if not path:
                    return None
                # Reject tiny stubs (some platforms serve HTML/redirect pages
                # that yt-dlp writes as 5-12 KB pseudo-mp4 files)
                if os.path.exists(path) and os.path.getsize(path) < 50_000:
                    log.warning(
                        "Rejected stub download for %r (only %d bytes)",
                        item.source_url, os.path.getsize(path),
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
        out: list[ScrapedItem] = []
        handles = [h for h in handles if h]
        if not handles:
            return out
        per = max(1, max_per_run // len(handles))
        for h in handles:
            url = h if h.startswith("http") else self._handle_url(h)
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
        out: list[ScrapedItem] = []
        hashtags = [t for t in hashtags if t]
        if not hashtags:
            return out
        per = max(1, max_per_run // len(hashtags))
        for tag in hashtags:
            t = tag.lstrip("#")
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
        h = handle.lower()
        if "tiktok.com" in h or (h.startswith("@") and "instagram" not in h):
            return "tiktok"
        if "instagram.com" in h:
            return "instagram"
        if "reddit.com" in h or h.startswith("/r/"):
            return "reddit"
        return "youtube"

    def _handle_url(self, handle: str) -> str:
        h = handle.lstrip("/")
        if h.startswith("@"):
            # TikTok disabled its public username endpoint; use ytsearch instead
            name = h.lstrip("@")
            return f"ytsearch5:{name} viral shorts"
        if h.startswith("/r/"):
            return f"https://www.reddit.com{h}"
        if h.startswith("r/"):
            return f"https://www.reddit.com/r/{h[2:]}"
        return h

    
