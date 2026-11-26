"""SaaS test fixtures. Every SaaS test runs against an in-memory DB and a temp data dir.

The root tests/conftest.py has already neutralised the owner's .env and content folders.
SAAS_* values are pinned here before anything imports saas.config.
"""
from __future__ import annotations

import os
import tempfile

os.environ["SAAS_ENV_FILE"] = ""
os.environ["SAAS_ENV"] = "dev"
os.environ["SAAS_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SAAS_SECRET_KEY"] = "test-secret-key-not-for-production-0123456789"
os.environ["SAAS_DATA_DIR"] = os.path.join(tempfile.gettempdir(), "doomscroller-saas-test-data")
os.environ["SAAS_TURNSTILE_SECRET_KEY"] = ""
os.environ["SAAS_SMTP_HOST"] = ""
os.environ["SAAS_STRIPE_SECRET_KEY"] = ""
os.environ["SAAS_RAZORPAY_KEY_ID"] = ""
os.environ["SAAS_GOOGLE_CLIENT_ID"] = ""

import pytest  # noqa: E402


@pytest.fixture()
def saas_db():
    """Fresh schema per test: drop and recreate every table on the shared in-memory engine."""
    from saas import models  # noqa: F401
    from saas.db import Base, SessionLocal, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(saas_db):
    from fastapi.testclient import TestClient

    from saas.main import create_app

    with TestClient(create_app()) as c:
        yield c
