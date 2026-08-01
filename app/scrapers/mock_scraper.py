"""Mock scraper that returns realistic fake viral videos.

Used when USE_MOCK=true so the system is fully runnable without network
access, proxies, or platform credentials.
"""
from __future__ import annotations

import hashlib
import random
from typing import List

from .base import ScrapedItem, Scraper

_TITLES = [
    "POV: when the beat drops",
    "I tried this hack so you don't have to",
    "Wait for it...",
    "Tell me you're a dev without telling me",
    "3-second life hack you wish you knew",
    "This took 47 takes",
    "Why didn't anyone tell me this?",
    "The ending got me",
    "Watch till the end",
    "Day 47 of building in public",
    "When the code finally compiles",
    "The recipe that broke the internet",
    "I cannot believe this worked",
    "Cat vs cucumber: round 12",
    "How to actually be productive",
    "Never skip warm-up day",
    "They said it couldn't be done",
    "Reading my old code be like",
    "How it started vs how it's going",
    "Stop scrolling. Try this.",
]

_AUTHORS = [
    "@viralbyte", "@dailyfyp", "@creator_lab", "@themaker", "@codebro",
    "@snippetdaily", "@chefmind", "@lifehackhq", "@catdad", "@buildinpublik",
]


def _seed_from(s: str) -> int:
    return int(hashlib.md5(s.encode("utf-8")).hexdigest(), 16)


class MockScraper(Scraper):
    def scrape_handles(self, handles: list[str], max_per_run: int,
                       download: bool = True) -> list[ScrapedItem]:
        handles = handles or _AUTHORS
        out: List[ScrapedItem] = []
        for h in handles:
            n = max(1, max_per_run // max(1, len(handles)))
            for i in range(n):
                out.append(self._fake_item(source="tiktok", handle=h, idx=i))
        return self._dedup(out)[:max_per_run]

    def scrape_hashtags(self, hashtags: list[str], max_per_run: int,
                        download: bool = True) -> list[ScrapedItem]:
        hashtags = hashtags or ["fyp", "viral", "shorts"]
        out: List[ScrapedItem] = []
        for tag in hashtags:
            n = max(1, max_per_run // max(1, len(hashtags)))
            for i in range(n):
                out.append(self._fake_item(source="instagram", handle=tag, idx=i))
        return self._dedup(out)[:max_per_run]

    def _fake_item(self, *, source: str, handle: str, idx: int) -> ScrapedItem:
        rnd = random.Random(_seed_from(f"{handle}-{idx}"))
        title = rnd.choice(_TITLES)
        author = rnd.choice(_AUTHORS) if not handle.startswith("@") else handle
        sid = f"{handle}_{idx}_{rnd.randint(1000, 9999)}"
        views = rnd.randint(50_000, 8_000_000)
        likes = int(views * rnd.uniform(0.03, 0.18))
        comments = int(likes * rnd.uniform(0.01, 0.08))
        shares = int(likes * rnd.uniform(0.05, 0.3))
        return ScrapedItem(
            source=source,
            source_id=sid,
            source_url=f"https://example.com/{source}/{sid}",
            author=author,
            title=title,
            description=f"Mock scraped from {handle} on {source}.",
            thumbnail_url=None,
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            duration_sec=rnd.randint(8, 58),
            meta={"mock": True, "seed_handle": handle},
        )
