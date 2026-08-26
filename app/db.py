"""Database session management."""
from __future__ import annotations

import logging
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
        db_path = url.replace("sqlite:///", "", 1)
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return create_engine(url, future=True, connect_args=connect_args, echo=False, pool_pre_ping=True)
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
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True, expire_on_commit=False)


# SQLAlchemy type -> SQLite column type, for the lightweight auto-migration below.
_SQLITE_TYPE_FALLBACK = "TEXT"


def _sqlite_add_missing_columns() -> list[str]:
    """Add columns present on the models but missing from the SQLite file.

    ``create_all`` only ever creates *new* tables -- it silently ignores tables
    that already exist, so adding a field to a model would otherwise raise
    ``OperationalError: no such column`` against an existing database. Rather
    than forcing everyone to delete ``doomscroller.db`` on every model change,
    reconcile the two with ``ALTER TABLE ... ADD COLUMN``.

    Only additive changes are handled. Dropping or retyping a column still needs
    a real migration, which this project deliberately does not carry.
    """
    from sqlalchemy import inspect, text

    from .models import Base

    if not engine.url.get_backend_name().startswith("sqlite"):
        return []

    applied: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all will build it from scratch
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                try:
                    ddl_type = column.type.compile(dialect=engine.dialect)
                except Exception:
                    ddl_type = _SQLITE_TYPE_FALLBACK
                # A NOT NULL column needs a default to be addable to a populated
                # table; fall back to nullable, which is harmless for our use.
                statement = (
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'
                )
                try:
                    conn.execute(text(statement))
                except Exception as exc:  # pragma: no cover - defensive
                    logging.getLogger(__name__).warning(
                        "auto-migration failed for %s.%s: %s",
                        table.name, column.name, exc,
                    )
                    continue
                applied.append(f"{table.name}.{column.name}")
                # ALTER TABLE leaves existing rows NULL, but the model declares
                # a default that application code relies on (comparing NULL to a
                # float raises). Backfill so old rows match new ones.
                backfill = _scalar_default(column)
                if backfill is not None:
                    conn.execute(
                        text(
                            f'UPDATE "{table.name}" SET "{column.name}" = :value '
                            f'WHERE "{column.name}" IS NULL'
                        ),
                        {"value": backfill},
                    )
    return applied


def _scalar_default(column) -> object | None:
    """Return the literal a newly added column should be backfilled with.

    SQLAlchemy wraps callable defaults in a ``CallableColumnDefault`` whose
    ``arg`` is a context-taking shim; the function the model actually declared
    is on ``arg.__wrapped__``. Only container factories (``list``/``dict``) and
    plain scalars can be backfilled -- row-specific callables such as ``uuid4``
    and ``utcnow`` are deliberately left NULL.
    """
    default = getattr(column, "default", None)
    if default is None:
        return None
    arg = getattr(default, "arg", None)

    if getattr(default, "is_callable", False):
        original = getattr(arg, "__wrapped__", arg)
        if original is list:
            return "[]"
        if original is dict:
            return "{}"
        return None

    if isinstance(arg, bool):
        return int(arg)
    if isinstance(arg, (int, float, str)):
        return arg
    return None


def init_db() -> None:
    """Create all tables, then reconcile any newly added model columns."""
    from .models import Base  # noqa: WPS433 - import inside to avoid circular import
    Base.metadata.create_all(bind=engine)
    added = _sqlite_add_missing_columns()
    if added:
        logging.getLogger(__name__).info(
            "auto-migration added %d column(s): %s", len(added), ", ".join(added)
        )


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
