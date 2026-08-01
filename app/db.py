"""Database session management."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings


def _engine_factory() -> Engine:
    url = settings.db_url
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        connect_args = {"check_same_thread": False}
        # Ensure the parent directory exists for file-based SQLite databases.
        db_path = url.replace("sqlite:///", "", 1)
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return create_engine(url, future=True, connect_args=connect_args, echo=False)
    if url.startswith("sqlite") and ":memory:" in url:
        # In-memory SQLite — keep a single connection so the schema persists.
        return create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(url, future=True, echo=False)


engine: Engine = _engine_factory()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """Create all tables."""
    from .models import Base  # noqa: WPS433 - import inside to avoid circular import
    Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session. Auto-commits on success, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
