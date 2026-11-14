"""Scraper interface."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional


@dataclass
class ScrapedItem:
    source: str            # snapchat | instagram | youtube | mock
    source_id: str
    source_url: str        # canonical, human-viewable page URL
    author: str = ""       # platform handle, no leading '@'
    author_name: str = ""  # display name
    title: str = ""
    description: str = ""
    thumbnail_url: Optional[str] = None
    media_url: Optional[str] = None   # direct CDN URL for the video bytes
    local_path: Optional[str] = None
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    duration_sec: int = 0
    width: int = 0
    height: int = 0
    language: str = ""
    keywords: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None
    follower_count: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def aspect_ratio(self) -> float:
        if self.width and self.height and self.width > 0 and self.height > 0:
            return self.width / self.height
        return 0.0

    @property
    def is_vertical(self) -> bool:
        r = self.aspect_ratio
        return 0.0 < r < 0.95


class Scraper:
    """Base scraper interface."""

    def scrape_handles(self, handles: list[str], max_per_run: int,
                       download: bool = True) -> list[ScrapedItem]:
        raise NotImplementedError

    def scrape_hashtags(self, hashtags: list[str], max_per_run: int,
                        download: bool = True) -> list[ScrapedItem]:
        raise NotImplementedError

    def close(self) -> None:
        """Release any held HTTP/browser resources."""
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def now_iso() -> datetime:
        return datetime.now(timezone.utc)

    def _dedup(self, items: Iterable[ScrapedItem]) -> list[ScrapedItem]:
        seen = set()
        out: list[ScrapedItem] = []
        for it in items:
            if it is None:
                continue
            source = (it.source or "").strip()
            source_id = (it.source_id or "").strip()
            if not source or not source_id:
                continue
            key = (source, source_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out
