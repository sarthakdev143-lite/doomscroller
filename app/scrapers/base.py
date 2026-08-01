"""Scraper interface."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional


@dataclass
class ScrapedItem:
    source: str            # tiktok | instagram | youtube | mock
    source_id: str
    source_url: str
    author: str = ""
    title: str = ""
    description: str = ""
    thumbnail_url: Optional[str] = None
    local_path: Optional[str] = None
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    duration_sec: int = 0
    meta: dict = field(default_factory=dict)


class Scraper:
    """Base scraper interface."""

    def scrape_handles(self, handles: list[str], max_per_run: int,
                       download: bool = True) -> list[ScrapedItem]:
        raise NotImplementedError

    def scrape_hashtags(self, hashtags: list[str], max_per_run: int,
                        download: bool = True) -> list[ScrapedItem]:
        raise NotImplementedError

    @staticmethod
    def now_iso() -> datetime:
        return datetime.now(timezone.utc)

    def _dedup(self, items: Iterable[ScrapedItem]) -> list[ScrapedItem]:
        seen = set()
        out: list[ScrapedItem] = []
        for it in items:
            key = (it.source, it.source_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out
