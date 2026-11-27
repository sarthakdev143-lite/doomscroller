"""Shared fixtures/helpers for the core agent's tests (auth, billing, jobs, api v1, mcp).

Test modules do `from core_fixtures import *` so pytest sees the fixtures. Nothing here touches the
network: HIBP is off, Turnstile has no secret, mail is captured in-process, yt-dlp/DNS are stubbed by
the tests that need them.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

CSRF = "testcsrftoken0123456789abcdef"
PASSWORD = "Correct-Horse-Battery-42"


@pytest.fixture()
def core(monkeypatch, tmp_path, saas_db):
    """Deterministic settings for core tests; returns the settings object."""
    from saas.auth import compat, mailer
    from saas.config import settings

    monkeypatch.setattr(settings, "hibp_enabled", False)
    monkeypatch.setattr(settings, "launch_mode", "open")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "turnstile_secret_key", "")
    monkeypatch.setattr(settings, "engine", "mock")
    monkeypatch.setattr(mailer, "_SYNC_FOR_TESTS", True)
    mailer.outbox.clear()
    compat._local.reset()
    try:
        from saas.security import limits

        limits.reset_state()
    except ImportError:
        pass
    # Deterministic, generous risk verdict unless a test overrides it.
    monkeypatch.setattr(compat, "assess_signup", lambda *a, **k: {"score": 0, "reasons": [], "block": False,
                                                                   "grant_free_credits": True})
    from saas.billing import catalog

    # Tests use a frozen, fully published catalog, not the provisional product price list.
    catalog.seed_catalog(saas_db, Path(__file__).with_name("core_catalog.json"))
    saas_db.commit()
    yield settings
    mailer.outbox.clear()


def form_fields() -> dict:
    return {"fp": uuid.uuid4().hex, "ts": int(time.time() * 1000) - 5000, "website": ""}


def csrf(client) -> dict:
    client.cookies.set("csrf", CSRF)
    return {"X-CSRF-Token": CSRF}


def make_user(db, email: str = None, *, password: str = PASSWORD, verified: bool = True, plan: str = "free",
              credits: int = 0, role: str = "user", paid: bool = False):
    from saas.auth import passwords, service
    from saas.billing import ledger
    from saas.common import utcnow
    from saas.models.core import User

    email = email or f"u{uuid.uuid4().hex[:10]}@example.com"
    user = User(email=email, email_canonical=service.canonical_email(email), password_hash=passwords.hash_password(password),
                role=role, status="active", plan_slug=plan, referral_code=uuid.uuid4().hex[:8],
                email_verified_at=utcnow() if verified else None, signup_risk={"grant_free_credits": True},
                first_paid_at=utcnow() if paid else None, flags={})
    db.add(user)
    db.flush()
    ledger.ensure_account(db, user.id)
    if credits:
        ledger.grant(db, user.id, credits, kind="admin_adjust", idempotency_key=f"test:{user.id}:{uuid.uuid4().hex}")
    db.commit()
    return user


def login(client, email: str, password: str = PASSWORD) -> dict:
    client.cookies.clear()
    r = client.post("/auth/login", json={"email": email, "password": password, **form_fields()})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    return csrf(client)


def make_key(db, user, scopes=None) -> str:
    from saas.auth import keys

    _key, raw = keys.create(db, user, "test", scopes)
    db.commit()
    return raw


def outbox_link(path_prefix: str) -> str:
    """The token from the most recent dev email whose link contains path_prefix."""
    from saas.auth import mailer

    for mail in reversed(mailer.outbox):
        for word in mail.text.split():
            if path_prefix in word:
                return word.rsplit("/", 1)[-1]
    raise AssertionError(f"no mail with {path_prefix}")


def write_video_stub(path: Path) -> None:
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 4096)
