"""Mock scraper that returns realistic fake viral videos with real dummy mp4 files.

Used when USE_MOCK=true so the system is fully runnable without network
access, proxies, or platform credentials. Each scraped item gets a tiny
real mp4 in the content directory so the transform + publish pipeline has
something to work with.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
from datetime import datetime, timedelta, timezone
import shutil
import subprocess
from pathlib import Path
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
    "The controversial opinion nobody asked for",
    "Me explaining my code to my rubber duck",
    "POV: you finally fixed the bug",
    "This is your sign to start today",
    "Rating my week based on this one screenshot",
    "The algorithm really said today is your day",
    "I asked AI to write this script",
    "Nobody: ... Me at 2am:",
    "This transition ate and left no crumbs",
    "Full tutorial: zero to hero in 60 seconds",
]

_AUTHORS = [
    # TikTok top creators
    "@khaby.lame", "@charlidamelio", "@mrbeast", "@bellapoarch",
    "@addisonre", "@zachking", "@kimberly.loaiza", "@bts_official_bighit",
    "@therock", "@willsmith", "@domelipa", "@billieeilish",
    "@cznburak", "@lamine.yamal", "@jasonderulo", "@kyliejenner",
    "@selenagomez", "@ishowspeed", "@dixiedamelio", "@lorengray",
    "@bp_tiktok", "@brentrivera", "@kallmekris", "@justmaiko",
    "@brookemonk_", "@neymarjr", "@carlosferiag", "@nianaguerrero",
    "@itsjojosiwa",
    # Instagram top creators / brands
    "@cristiano", "@leomessi", "@arianagrande", "@kimkardashian",
    "@beyonce", "@khloekardashian", "@kendalljenner", "@kourtneykardash",
    # Snapchat spotlight / discover handles
    "@kylizzlemynizzl", "@djkhaled305", "@moonlightbae", "@lilswag79",
    "@loren", "@rickthesizzler", "@snoopdogg", "@itsgigihadid",
    "@babybels777", "@theddlovato", "@bellathornedab", "@zendaya_96",
    "@shawnmendes1", "@neymarjr", "@loganpaul", "@amandacerny",
    "@nickiminaj", "@ladygaga", "@ellen", "@calvinharris",
    "@caradevilqueen", "@mileycyrus", "@jlobts", "@mtrainor22",
    "@jaredleto", "@kingbach", "@notcharlieputh", "@gypsybanessa",
    "@mynamechance", "@katyperry", "@iamcardib", "@rebelwilsonsnap",
    "@realmadrid", "@fcbarcelona",
]


def _seed_from(s: str) -> int:
    return int(hashlib.md5(s.encode("utf-8")).hexdigest(), 16)


def _ensure_master_dummy(content_dir: str, master_path: str) -> str:
    """Create one shared dummy mp4 that we copy per scraped item."""
    if content_dir:
        os.makedirs(content_dir, exist_ok=True)
    Path(master_path).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(master_path) and os.path.getsize(master_path) > 0:
        return master_path
    from app.winproc import background_subprocess_kwargs

    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i",
                "color=c=black:s=1080x1920:d=3",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", master_path,
            ],
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
            timeout=30,
            **background_subprocess_kwargs(low_priority=True),
        )
        return master_path
    except Exception:
        # Fallback: empty file with .mp4 extension
        Path(master_path).write_bytes(b"")
        return master_path


class MockScraper(Scraper):
    def __init__(self) -> None:
        from ..config import settings
        self.content_dir = settings.content_path
        os.makedirs(self.content_dir, exist_ok=True)
        self._master_path = os.path.join(self.content_dir, "_mock_master.mp4")
        _ensure_master_dummy(str(self.content_dir), self._master_path)

    def scrape_handles(self, handles: list[str], max_per_run: int,
                       download: bool = True) -> list[ScrapedItem]:
        if max_per_run <= 0:
            return []
        clean_handles = [
            h.strip() for h in (handles or []) if isinstance(h, str) and h.strip()
        ] or list(_AUTHORS)
        from ..config import settings as _s
        snap_only = [p.lower() for p in _s.enabled_platforms_list] == ["snapchat"]
        source = "snapchat" if snap_only else "tiktok"
        out: List[ScrapedItem] = []
        n = max(1, (max_per_run + len(clean_handles) - 1) // len(clean_handles))
        for h in clean_handles:
            if len(out) >= max_per_run:
                break
            for i in range(n):
                if len(out) >= max_per_run:
                    break
                out.append(self._fake_item(source=source, handle=h, idx=i, download=download))
        return self._dedup(out)[:max_per_run]

    def scrape_hashtags(self, hashtags: list[str], max_per_run: int,
                        download: bool = True) -> list[ScrapedItem]:
        if max_per_run <= 0:
            return []
        clean_tags = [
            t.strip().lstrip("#").strip()
            for t in (hashtags or [])
            if isinstance(t, str) and t.strip().lstrip("#").strip()
        ] or ["fyp", "viral", "shorts", "reels", "snapchat"]
        from ..config import settings as _s2
        snap_only = [p.lower() for p in _s2.enabled_platforms_list] == ["snapchat"]
        source = "snapchat" if snap_only else "instagram"
        out: List[ScrapedItem] = []
        n = max(1, (max_per_run + len(clean_tags) - 1) // len(clean_tags))
        for tag in clean_tags:
            if len(out) >= max_per_run:
                break
            for i in range(n):
                if len(out) >= max_per_run:
                    break
                out.append(self._fake_item(source=source, handle=tag, idx=i, download=download))
        return self._dedup(out)[:max_per_run]

    def _fake_item(
        self, *, source: str, handle: str, idx: int, download: bool = True
    ) -> ScrapedItem:
        rnd = random.Random(_seed_from(f"{handle}-{idx}"))
        title = rnd.choice(_TITLES)
        author = rnd.choice(_AUTHORS) if not handle.startswith("@") else handle
        safe_slug = re.sub(r"[^A-Za-z0-9_.-]", "_", handle.strip()).strip("_.")[:40] or "mock"
        sid = f"{safe_slug}_{idx}_{rnd.randint(1000, 9999)}"
        views = rnd.randint(50_000, 8_000_000)
        likes = int(views * rnd.uniform(0.03, 0.18))
        comments = int(likes * rnd.uniform(0.01, 0.08))
        shares = int(likes * rnd.uniform(0.05, 0.3))
        duration_sec = rnd.randint(8, 58)

        local_path: str | None = None
        if download:
            os.makedirs(self.content_dir, exist_ok=True)
            local_path = os.path.join(str(self.content_dir), f"{source}_{sid}.mp4")
            if not os.path.exists(local_path):
                if not os.path.exists(self._master_path):
                    _ensure_master_dummy(str(self.content_dir), self._master_path)
                if os.path.exists(self._master_path):
                    shutil.copy2(self._master_path, local_path)
                else:
                    Path(local_path).write_bytes(b"")

        published_at = datetime.now(timezone.utc) - timedelta(
            hours=rnd.uniform(1, 96)
        )
        return ScrapedItem(
            source=source,
            source_id=sid,
            source_url=f"https://example.com/{source}/{sid}",
            author=author.lstrip("@"),
            author_name=author.lstrip("@").replace(".", " ").title(),
            title=title,
            description=f"Mock scraped from {handle} on {source}.",
            thumbnail_url=None,
            media_url=f"https://example.com/{source}/{sid}.mp4",
            local_path=local_path,
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            duration_sec=duration_sec,
            width=1080,
            height=1920,
            language="en",
            keywords=rnd.sample(
                ["viral", "funny", "trending", "shorts", "dance", "pets", "food"], 3
            ),
            published_at=published_at,
            follower_count=rnd.randint(1_000, 5_000_000),
            meta={"mock": True, "seed_handle": handle},
        )
