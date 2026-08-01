"""Creator auto-discovery.

Maintains a Creator table. Seed creators come from TARGET_HANDLES in .env.
The discoverer:
  1. Picks the top-N most-engaging recently-scraped videos
  2. Extracts their authors as candidate creators
  3. Adds them to the Creator table with `discovered_from=<seed>`
  4. Boosts a creator's score when their videos do well
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .db import session_scope
from .models import Creator, Video

log = logging.getLogger(__name__)


def ensure_seed_creators(db: Session, handles: list[str], platform: str = "tiktok") -> int:
    """Insert any seed handles that aren't yet in the Creator table."""
    from .config import settings
    handles = handles or settings.handles_list
    added = 0
    for h in handles:
        h = h.strip()
        if not h:
            continue
        existing = db.execute(
            select(Creator).where(Creator.handle == h, Creator.platform == platform)
        ).scalar_one_or_none()
        if existing:
            continue
        db.add(Creator(
            handle=h, platform=platform, is_seed=True,
            score=1.0,
            meta={"seed": True},
        ))
        added += 1
    db.flush()
    return added


def discover_creators(db: Session, *, days: int = 7, top_n: int = 5) -> list[Creator]:
    """Find new creators from the authors of the top-N recent viral videos."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(Video)
        .where(Video.scraped_at >= since)
        .order_by(desc(Video.engagement_score))
        .limit(top_n * 4)
    ).scalars().all()

    # Group engagement by author
    author_scores: dict[tuple[str, str], float] = {}
    for v in rows:
        if not v.author:
            continue
        key = (v.source, v.author)
        author_scores[key] = author_scores.get(key, 0.0) + v.engagement_score

    # Sort and take top N
    ranked = sorted(author_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    new_creators: list[Creator] = []
    for (platform, handle), score in ranked:
        existing = db.execute(
            select(Creator).where(Creator.handle == handle, Creator.platform == platform)
        ).scalar_one_or_none()
        if existing:
            existing.score = max(existing.score, float(score))
            existing.videos_scraped = (existing.videos_scraped or 0) + 1
            continue
        c = Creator(
            handle=handle, platform=platform, is_seed=False,
            discovered_from="auto",
            score=float(score),
            videos_scraped=1,
        )
        db.add(c)
        new_creators.append(c)
    db.flush()
    return new_creators


def list_active_creators(db: Session, limit: int = 50) -> list[Creator]:
    """Return creators sorted by score — these are the ones the scraper should hit next."""
    rows = db.execute(
        select(Creator).order_by(desc(Creator.score), desc(Creator.is_seed)).limit(limit)
    ).scalars().all()
    return list(rows)


def boost_creator(db: Session, platform: str, handle: str, amount: float) -> None:
    """Increase a creator's score when one of their videos gets approved/published."""
    c = db.execute(
        select(Creator).where(Creator.handle == handle, Creator.platform == platform)
    ).scalar_one_or_none()
    if c:
        c.score = (c.score or 0.0) + amount
        db.flush()
