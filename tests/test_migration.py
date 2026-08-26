"""Tests for the additive SQLite auto-migration.

``create_all`` ignores tables that already exist, so adding a field to a model
would otherwise break against an existing database file. These tests pin the
behaviour that matters: the column appears, existing rows survive, and the new
column is backfilled rather than left NULL (comparing NULL to a float raises).
"""
import sqlite3

import pytest
from sqlalchemy import Column, Float, Integer, String, Table, create_engine, text

from app import db as db_module


@pytest.fixture()
def sqlite_engine(tmp_path, monkeypatch):
    """A real on-disk SQLite engine, swapped in for the module-level one."""
    path = tmp_path / "migrate.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    monkeypatch.setattr(db_module, "engine", engine)
    return engine


def test_adds_missing_column_and_keeps_rows(sqlite_engine, monkeypatch):
    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):
        pass

    widgets = Table(
        "widgets", Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
    )
    Base.metadata.create_all(sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'first')"))

    # The model grows a scored column with a default.
    widgets.append_column(Column("score", Float, default=0.0))
    monkeypatch.setattr(db_module, "_sqlite_add_missing_columns.__globals__", {}, raising=False)

    import app.models as models

    monkeypatch.setattr(models, "Base", Base)
    applied = db_module._sqlite_add_missing_columns()

    assert "widgets.score" in applied
    with sqlite_engine.connect() as conn:
        rows = conn.execute(text("SELECT id, name, score FROM widgets")).all()
    assert rows == [(1, "first", 0.0)], "existing row kept and backfilled"


def test_second_run_is_a_no_op(sqlite_engine, monkeypatch):
    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):
        pass

    Table(
        "things", Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("count", Integer, default=0),
    )
    Base.metadata.create_all(sqlite_engine)

    import app.models as models

    monkeypatch.setattr(models, "Base", Base)
    assert db_module._sqlite_add_missing_columns() == []
    assert db_module._sqlite_add_missing_columns() == []


def test_real_schema_migrates_from_scratch_and_is_idempotent(tmp_path, monkeypatch):
    """The actual Video schema applies cleanly to an empty file, twice."""
    path = tmp_path / "real.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    monkeypatch.setattr(db_module, "engine", engine)

    db_module.init_db()
    db_module.init_db()  # idempotent

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    for expected in (
        "viral_score", "reach_score", "width", "height",
        "language", "keywords", "author_name", "media_url",
        "source_published_at",
    ):
        assert expected in columns


def test_backfill_only_covers_static_defaults():
    """Row-specific callables (uuid, utcnow) must not be backfilled."""
    from app.models import Video

    columns = {c.name: c for c in Video.__table__.columns}
    # A plain numeric default is backfillable.
    assert db_module._scalar_default(columns["viral_score"]) == 0.0
    assert db_module._scalar_default(columns["views"]) == 0
    # A JSON column defaulting to list becomes an empty JSON array.
    assert db_module._scalar_default(columns["keywords"]) == "[]"
    # uuid4 / utcnow are per-row, so they are left alone.
    assert db_module._scalar_default(columns["id"]) is None
    assert db_module._scalar_default(columns["scraped_at"]) is None
