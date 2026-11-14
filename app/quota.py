"""Daily quota tracking for YouTube uploads.

Each managed channel uploads through its own Google Cloud project, so each has its own daily count.
With no channel the count is the legacy shared one, as before.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import QuotaUsage

_MAX_NOTES_CHARS = 8_000


def _clean_channel_id(channel_id: Optional[str]) -> Optional[str]:
    if not isinstance(channel_id, str):
        return None
    cleaned = channel_id.strip()
    return cleaned or None


def _today(now: Optional[datetime] = None) -> str:
    if now is None:
        dt = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        dt = now.replace(tzinfo=timezone.utc)
    else:
        dt = now.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _row_id(today: str, channel_id: Optional[str]) -> str:
    # QuotaUsage.id is String(32): fit short channel IDs verbatim and hash longer ones
    # so channels sharing a 20-char prefix can never collide on primary key.
    cid = _clean_channel_id(channel_id)
    if not cid:
        return today
    if len(cid) <= 21:
        return f"{today}:{cid}"
    digest = hashlib.sha1(cid.encode("utf-8")).hexdigest()[:8]
    return f"{today}:{cid[:12]}_{digest}"


def _read_today(db: Session, today: str, channel_id: Optional[str] = None) -> Optional[QuotaUsage]:
    # A database from before ids were fixed can hold duplicates. The fullest never understates the count.
    cid = _clean_channel_id(channel_id)
    mine = QuotaUsage.channel_id.is_(None) if cid is None else QuotaUsage.channel_id == cid
    return db.execute(
        select(QuotaUsage).where(QuotaUsage.day == today, mine)
        .order_by(QuotaUsage.uploads.desc(), QuotaUsage.id).limit(1)
    ).scalars().first()


def get_or_create_today(
    db: Session, channel_id: Optional[str] = None, *, now: Optional[datetime] = None
) -> QuotaUsage:
    today = _today(now)
    cid = _clean_channel_id(channel_id)
    q = _read_today(db, today, cid)
    if q is None:
        q = QuotaUsage(
            id=_row_id(today, cid),
            day=today,
            uploads=0,
            cap=max(0, int(settings.max_uploads_per_day)),
            channel_id=cid,
        )
        try:
            with db.begin_nested():
                db.add(q)
        except IntegrityError:
            found = _read_today(db, today, cid)
            if found is not None:
                return found
            raise
    return q


def remaining(
    db: Session, channel_id: Optional[str] = None, *, now: Optional[datetime] = None
) -> int:
    q = get_or_create_today(db, channel_id, now=now)
    return max(0, (q.cap or 0) - (q.uploads or 0))


def consume_one(
    db: Session,
    note: Optional[str] = None,
    channel_id: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
) -> None:
    q = get_or_create_today(db, channel_id, now=now)
    q.uploads = max(0, (q.uploads or 0)) + 1
    if note:
        clean_note = str(note).strip()
        if clean_note:
            joined = f"{q.notes}\n{clean_note}" if q.notes else clean_note
            q.notes = joined[-_MAX_NOTES_CHARS:]


def snapshot(
    db: Session, channel_id: Optional[str] = None, *, now: Optional[datetime] = None
) -> dict:
    q = get_or_create_today(db, channel_id, now=now)
    uploads = max(0, q.uploads or 0)
    cap = max(0, q.cap or 0)
    return {
        "day": q.day,
        "uploads": uploads,
        "cap": cap,
        "remaining": max(0, cap - uploads),
    }
