"""Database session management."""
from __future__ import annotations

import enum
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings

SQLITE_BUSY_TIMEOUT_MS = 30_000


def _apply_sqlite_pragmas(dbapi_conn, *, memory: bool = False) -> None:
    cursor = dbapi_conn.cursor()
    try:
        # Set busy_timeout FIRST so switching to WAL waits out any brief startup lock on Windows.
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        if not memory:
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA cache_size=-4000")
    except sqlite3.Error:
        pass
    finally:
        cursor.close()


def _ensure_sqlite_pragmas(eng: Engine) -> None:
    if not eng.url.get_backend_name().startswith("sqlite"):
        return
    if getattr(eng, "_doomscroller_pragmas", False):
        return
    is_memory = ":memory:" in str(eng.url).lower() or eng.url.database in (None, "", ":memory:")

    @event.listens_for(eng, "connect")
    def _configure_sqlite(dbapi_conn, _record) -> None:
        _apply_sqlite_pragmas(dbapi_conn, memory=is_memory)

    eng._doomscroller_pragmas = True  # type: ignore[attr-defined]


def create_engine_for_url(url: str) -> Engine:
    cleaned = (url or "").strip()
    lowered = cleaned.lower()
    if lowered.startswith("sqlite:///") and ":memory:" not in lowered:
        # The scheduler thread and API requests all write, so wait out a brief lock.
        connect_args = {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_MS // 1000}
        db_path = cleaned[len("sqlite:///"):].split("?", 1)[0]
        if db_path:
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        eng = create_engine(cleaned, future=True, connect_args=connect_args, echo=False, pool_pre_ping=True)
        _ensure_sqlite_pragmas(eng)
        return eng
    if lowered.startswith("sqlite") and ":memory:" in lowered:
        # In-memory SQLite — keep a single connection so the schema persists.
        eng = create_engine(
            cleaned,
            future=True,
            connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_MS // 1000},
            poolclass=StaticPool,
        )
        _ensure_sqlite_pragmas(eng)
        return eng
    return create_engine(cleaned, future=True, echo=False)


def _engine_factory() -> Engine:
    return create_engine_for_url(settings.db_url)


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

    _ensure_sqlite_pragmas(engine)
    applied: list[str] = []

    with engine.begin() as conn:
        conn.execute(text(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"))
        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())

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
                    if "duplicate column" in str(exc).lower():
                        continue
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


def _sqlite_add_missing_indexes() -> list[str]:
    """Ensure model-declared indexes exist on upgraded SQLite tables."""
    from sqlalchemy import inspect

    from .models import Base

    if not engine.url.get_backend_name().startswith("sqlite"):
        return []

    created: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present_indexes = {idx["name"] for idx in inspector.get_indexes(table.name) if idx.get("name")}
        for index in table.indexes:
            if not index.name or index.name in present_indexes:
                continue
            try:
                index.create(bind=engine, checkfirst=True)
                created.append(index.name)
            except Exception as exc:  # pragma: no cover - defensive
                if "already exists" not in str(exc).lower():
                    logging.getLogger(__name__).warning(
                        "auto-index failed for %s.%s: %s", table.name, index.name, exc
                    )
    return created


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
    if isinstance(arg, enum.Enum):
        return arg.name
    if isinstance(arg, (int, float, str)):
        return arg
    return None


def init_db() -> None:
    """Upgrade existing tables, create any missing tables, and ensure indexes."""
    from .models import Base  # noqa: WPS433 - import inside to avoid circular import

    _ensure_sqlite_pragmas(engine)
    added = _sqlite_add_missing_columns()
    Base.metadata.create_all(bind=engine)
    _sqlite_add_missing_indexes()
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
