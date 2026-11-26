"""The SaaS database. Its own engine and its own file: never the growth loop's doomscroller.db.

SQLite (WAL) in dev and on a single box; Postgres in prod via SAAS_DATABASE_URL. Models must stay
portable: no SQLite-only types, timezone-aware UTC datetimes, JSON columns via sqlalchemy.JSON.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        memory = ":memory:" in url or url in ("sqlite://", "sqlite:///")
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if memory:
            kwargs["poolclass"] = StaticPool
        eng = create_engine(url, **kwargs)

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            if not memory:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        return eng
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For workers and scripts: commit on success, roll back on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (registers every table on Base.metadata)

    Base.metadata.create_all(bind=engine)
