"""Daily quota tracking for YouTube uploads."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import QuotaUsage


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_or_create_today(db: Session) -> QuotaUsage:
    today = _today()
    q = db.execute(
        select(QuotaUsage).where(QuotaUsage.day == today)
    ).scalar_one_or_none()
    if q is None:
        q = QuotaUsage(day=today, uploads=0, cap=settings.max_uploads_per_day)
        db.add(q)
        db.flush()
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
