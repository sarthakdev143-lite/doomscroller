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
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from .models import Creator, Video

log = logging.getLogger(__name__)


def _normalize_handle_pair(raw: Any) -> tuple[str, str]:
    """Return ``(trimmed_display_handle, bare_lowercase_key)`` or ``('', '')``."""
    if not isinstance(raw, str):
        return "", ""
    trimmed = raw.strip()
    bare = trimmed.lstrip("@").strip().lower()
    if not bare:
        return "", ""
    return trimmed, bare


def _clean_platform(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _clean_score(raw: Any) -> float:
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val if math.isfinite(val) and val >= 0.0 else 0.0


def _find_existing_creator(db: Session, platform: str, bare_lower: str) -> Creator | None:
    """Lookup creator case-insensitively across bare and '@'-prefixed forms without MultipleResultsFound."""
    return db.execute(
        select(Creator)
        .where(
            func.lower(Creator.platform) == platform.lower(),
            func.lower(Creator.handle).in_([bare_lower, f"@{bare_lower}"]),
        )
        .order_by(desc(Creator.score), desc(Creator.is_seed), Creator.id)
        .limit(1)
    ).scalars().first()


def ensure_seed_creators(db: Session, handles: list[str], platform: str = "instagram") -> int:
    """Insert any seed handles that aren't yet in the Creator table."""
    from .config import settings

    plat = _clean_platform(platform) or "instagram"
    raw_handles = handles if handles else settings.handles_list
    added = 0
    seen_keys: set[str] = set()
    for raw in raw_handles or []:
        display, bare = _normalize_handle_pair(raw)
        if not bare or bare in seen_keys:
            continue
        seen_keys.add(bare)
        existing = _find_existing_creator(db, plat, bare)
        if existing is not None:
            continue
        db.add(Creator(
            handle=display,
            platform=plat,
            is_seed=True,
            score=1.0,
            meta={"seed": True},
        ))
        added += 1
    db.flush()
    return added


def discover_creators(db: Session, *, days: int = 7, top_n: int = 5) -> list[Creator]:
    """Find new creators from the authors of the top-N recent viral videos."""
    if days <= 0 or top_n <= 0:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(Video)
        .where(Video.scraped_at >= since)
        .order_by(desc(Video.engagement_score))
        .limit(max(4, top_n * 4))
    ).scalars().all()

    # Group engagement by normalized (platform, bare_handle)
    author_scores: dict[tuple[str, str], float] = {}
    author_counts: dict[tuple[str, str], int] = {}
    author_display: dict[tuple[str, str], str] = {}
    for v in rows:
        plat = _clean_platform(getattr(v, "source", None))
        display, bare = _normalize_handle_pair(getattr(v, "author", None))
        if not plat or not bare:
            continue
        key = (plat, bare)
        score_add = _clean_score(getattr(v, "engagement_score", 0.0))
        author_scores[key] = author_scores.get(key, 0.0) + score_add
        author_counts[key] = author_counts.get(key, 0) + 1
        # Prefer bare handle (without '@') when available
        if key not in author_display or author_display[key].startswith("@"):
            author_display[key] = display.lstrip("@") or display

    ranked = sorted(author_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    new_creators: list[Creator] = []
    for (plat, bare), score in ranked:
        count = author_counts.get((plat, bare), 1)
        existing = _find_existing_creator(db, plat, bare)
        if existing is not None:
            existing.score = max(_clean_score(existing.score), float(score))
            existing.videos_scraped = max(0, existing.videos_scraped or 0) + count
            continue
        c = Creator(
            handle=author_display[(plat, bare)],
            platform=plat,
            is_seed=False,
            discovered_from="auto",
            score=float(score),
            videos_scraped=count,
        )
        db.add(c)
        new_creators.append(c)
    db.flush()
    return new_creators


def list_active_creators(db: Session, limit: int = 50) -> list[Creator]:
    """Return creators sorted by score — these are the ones the scraper should hit next."""
    if limit <= 0:
        return []
    rows = db.execute(
        select(Creator)
        .order_by(desc(Creator.score), desc(Creator.is_seed), Creator.id)
        .limit(max(limit, limit * 2))
    ).scalars().all()
    out: list[Creator] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        plat = _clean_platform(row.platform)
        _, bare = _normalize_handle_pair(row.handle)
        key = (plat, bare or str(row.id))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def boost_creator(db: Session, platform: str, handle: str, amount: float) -> None:
    """Increase a creator's score when one of their videos gets approved/published."""
    plat = _clean_platform(platform)
    _, bare = _normalize_handle_pair(handle)
    if not plat or not bare:
        return
    if isinstance(amount, bool):
        return
    try:
        delta = float(amount)
    except (TypeError, ValueError):
        return
    if not math.isfinite(delta):
        return
    c = _find_existing_creator(db, plat, bare)
    if c is not None:
        c.score = _clean_score(c.score) + delta
        db.flush()
