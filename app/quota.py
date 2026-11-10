"""Daily quota tracking for YouTube uploads.

Each managed channel uploads through its own Google Cloud project, so each has its own daily count.
With no channel the count is the legacy shared one, as before.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import QuotaUsage


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _row_id(today: str, channel_id: Optional[str]) -> str:
    # The day (and channel) is the id, so of two first calls racing only one can insert.
    return today if not channel_id else f"{today}:{channel_id[:20]}"


def _read_today(db: Session, today: str, channel_id: Optional[str] = None) -> Optional[QuotaUsage]:
    # A database from before ids were fixed can hold duplicates. The fullest never understates the count.
    mine = QuotaUsage.channel_id.is_(None) if not channel_id else QuotaUsage.channel_id == channel_id
    return db.execute(
        select(QuotaUsage).where(QuotaUsage.day == today, mine)
        .order_by(QuotaUsage.uploads.desc(), QuotaUsage.id).limit(1)
    ).scalars().first()


def get_or_create_today(db: Session, channel_id: Optional[str] = None) -> QuotaUsage:
    today = _today()
    q = _read_today(db, today, channel_id)
    if q is None:
        q = QuotaUsage(id=_row_id(today, channel_id), day=today, uploads=0, cap=settings.max_uploads_per_day,
                       channel_id=channel_id or None)
        try:
            with db.begin_nested():
                db.add(q)
        except IntegrityError:
            return _read_today(db, today, channel_id)
    return q


def remaining(db: Session, channel_id: Optional[str] = None) -> int:
    q = get_or_create_today(db, channel_id)
    return max(0, q.cap - q.uploads)


def consume_one(db: Session, note: Optional[str] = None, channel_id: Optional[str] = None) -> None:
    q = get_or_create_today(db, channel_id)
    q.uploads += 1
    if note:
        q.notes = (q.notes or "") + "\n" + note


def snapshot(db: Session, channel_id: Optional[str] = None) -> dict:
    q = get_or_create_today(db, channel_id)
    return {
        "day": q.day,
        "uploads": q.uploads,
        "cap": q.cap,
        "remaining": max(0, q.cap - q.uploads),
    }
