"""Daily quota tracking for YouTube uploads."""
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


def _read_today(db: Session, today: str) -> Optional[QuotaUsage]:
    # A database from before ids were fixed can hold duplicates. The fullest never understates the count.
    return db.execute(
        select(QuotaUsage).where(QuotaUsage.day == today)
        .order_by(QuotaUsage.uploads.desc(), QuotaUsage.id).limit(1)
    ).scalars().first()


def get_or_create_today(db: Session) -> QuotaUsage:
    today = _today()
    q = _read_today(db, today)
    if q is None:
        # The day is the id, so of two first calls racing only one can insert.
        q = QuotaUsage(id=today, day=today, uploads=0, cap=settings.max_uploads_per_day)
        try:
            with db.begin_nested():
                db.add(q)
        except IntegrityError:
            return _read_today(db, today)
    return q


def remaining(db: Session) -> int:
    q = get_or_create_today(db)
    return max(0, q.cap - q.uploads)


def consume_one(db: Session, note: Optional[str] = None) -> None:
    q = get_or_create_today(db)
    q.uploads += 1
    if note:
        q.notes = (q.notes or "") + "\n" + note


def snapshot(db: Session) -> dict:
    q = get_or_create_today(db)
    return {
        "day": q.day,
        "uploads": q.uploads,
        "cap": q.cap,
        "remaining": max(0, q.cap - q.uploads),
    }
