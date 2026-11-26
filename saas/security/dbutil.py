"""Portable atomic upserts (SQLite >= 3.35 and Postgres both speak ON CONFLICT ... RETURNING)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ..models.security import RateCounter, SeenSignal


def _insert_for(dialect_name: str):
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        return insert
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        return insert
    return None


def add_to_counter(conn: Connection, bucket: str, key: str, window_start: int, delta: int,
                   expires_at: int) -> int:
    """Atomically add `delta` to one window and return the new total."""
    insert = _insert_for(conn.dialect.name)
    if insert is not None:
        stmt = insert(RateCounter).values(bucket=bucket, key=key, window_start=window_start, count=delta,
                                          expires_at=expires_at)
        stmt = stmt.on_conflict_do_update(
            index_elements=["bucket", "key", "window_start"],
            set_={"count": RateCounter.count + delta},
        ).returning(RateCounter.count)
        return int(conn.execute(stmt).scalar_one())
    # Other backends: update-then-insert. Not atomic under a race on the very first hit, which only
    # under-counts by one; good enough for a fallback nobody runs today.
    res = conn.execute(
        update(RateCounter)
        .where(RateCounter.bucket == bucket, RateCounter.key == key, RateCounter.window_start == window_start)
        .values(count=RateCounter.count + delta)
    )
    if not res.rowcount:
        conn.execute(RateCounter.__table__.insert().values(bucket=bucket, key=key, window_start=window_start,
                                                           count=delta, expires_at=expires_at))
    return int(conn.execute(
        select(RateCounter.count).where(RateCounter.bucket == bucket, RateCounter.key == key,
                                        RateCounter.window_start == window_start)
    ).scalar_one())


def read_counter(conn: Connection, bucket: str, key: str, window_start: int) -> int:
    val = conn.execute(
        select(RateCounter.count).where(RateCounter.bucket == bucket, RateCounter.key == key,
                                        RateCounter.window_start == window_start)
    ).scalar_one_or_none()
    return int(val or 0)


def touch_signal(db: Session, kind: str, value: str | None, subject_kind: str, subject_id: str | None,
                 now: datetime) -> None:
    """Record that `subject` showed up with `value` (an IP, fingerprint or canonical email)."""
    if not value or not subject_id:
        return
    value = value[:160]
    insert = _insert_for(db.get_bind().dialect.name)
    if insert is not None:
        stmt = insert(SeenSignal).values(kind=kind, value=value, subject_kind=subject_kind,
                                         subject_id=subject_id, first_seen=now, last_seen=now, hits=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["kind", "value", "subject_kind", "subject_id"],
            set_={"last_seen": now, "hits": SeenSignal.hits + 1},
        )
        db.execute(stmt)
        return
    row = db.execute(select(SeenSignal).where(
        SeenSignal.kind == kind, SeenSignal.value == value, SeenSignal.subject_kind == subject_kind,
        SeenSignal.subject_id == subject_id)).scalar_one_or_none()
    if row is None:
        db.add(SeenSignal(kind=kind, value=value, subject_kind=subject_kind, subject_id=subject_id,
                          first_seen=now, last_seen=now, hits=1))
    else:
        row.last_seen = now
        row.hits = (row.hits or 0) + 1
