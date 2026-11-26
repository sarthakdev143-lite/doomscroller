"""Sliding-window limiter: DB-backed (survives restarts / shared by workers), fast path, contract shape."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture()
def limiter(saas_db):
    from saas.security import limits

    limits.reset_state()
    yield limits.limiter
    limits.reset_state()


def test_allows_up_to_limit_then_refuses(limiter):
    t = 1_000_000.0
    results = [limiter.hit("b", "k", 3, 60, now=t + i)[0] for i in range(5)]
    assert results == [True, True, True, False, False]


def test_retry_after_is_positive_and_bounded(limiter):
    t = 2_000_000.0
    for i in range(3):
        limiter.hit("b", "k", 3, 60, now=t)
    ok, retry = limiter.hit("b", "k", 3, 60, now=t + 1)
    assert not ok and 1 <= retry <= 120


def test_counts_survive_a_restart(limiter):
    """The DB is the source of truth: a new process (fresh in-memory state) still sees the hits."""
    t = 3_000_000.0
    for _ in range(3):
        limiter.hit("signup", "1.2.3.4", 3, 600, now=t)
    limiter.reset()  # simulate a restart / another worker
    ok, _ = limiter.hit("signup", "1.2.3.4", 3, 600, now=t + 1)
    assert ok is False


def test_two_workers_share_one_budget(saas_db):
    from saas.security.limits import Limiter

    a, b = Limiter(), Limiter()
    t = 3_500_000.0
    assert a.hit("x", "k", 4, 60, now=t)[0]
    assert b.hit("x", "k", 4, 60, now=t)[0]
    assert a.hit("x", "k", 4, 60, now=t)[0]
    assert b.hit("x", "k", 4, 60, now=t)[0]
    assert not a.hit("x", "k", 4, 60, now=t)[0]
    assert not b.hit("x", "k", 4, 60, now=t)[0]


def test_window_slides(limiter):
    t = 4_000_000.0 - (4_000_000.0 % 60)  # start of a window
    for _ in range(10):
        limiter.hit("s", "k", 10, 60, now=t + 1)
    # Half-way into the next window the previous one still weighs 50%: 5 + new hits.
    assert limiter.hit("s", "k", 10, 60, now=t + 90)[0]
    # Two windows later it is forgotten entirely.
    assert limiter.hit("s", "k", 10, 60, now=t + 200)[0]


def test_keys_and_buckets_are_independent(limiter):
    t = 5_000_000.0
    for _ in range(2):
        limiter.hit("a", "k1", 2, 60, now=t)
    assert not limiter.hit("a", "k1", 2, 60, now=t)[0]
    assert limiter.hit("a", "k2", 2, 60, now=t)[0]
    assert limiter.hit("b", "k1", 2, 60, now=t)[0]


def test_write_behind_mode_limits_without_syncing_every_hit(limiter, monkeypatch):
    calls = {"n": 0}
    orig = limiter._sync

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(limiter, "_sync", counting)
    t = 6_000_000.0
    oks = [limiter.hit("g", "ip", 50, 60, sync_s=5.0, now=t)[0] for _ in range(40)]
    assert all(oks)
    assert calls["n"] <= 2


def test_fails_open_on_db_but_still_limits_locally(limiter, monkeypatch):
    from saas.security import dbutil

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(dbutil, "add_to_counter", boom)
    monkeypatch.setattr(dbutil, "read_counter", boom)
    t = 7_000_000.0
    oks = [limiter.hit("d", "k", 3, 60, now=t)[0] for _ in range(5)]
    assert oks == [True, True, True, False, False]


def test_cleanup_deletes_expired_windows(limiter, saas_db):
    from sqlalchemy import func, select

    from saas.models.security import RateCounter

    limiter.hit("c", "k", 5, 60, now=1000.0)  # long expired
    assert saas_db.execute(select(func.count()).select_from(RateCounter)).scalar_one() == 1
    assert limiter.cleanup() == 1


def test_enforce_raises_contract_shape(saas_db):
    from saas.security import limits

    limits.reset_state()
    app = FastAPI()
    from saas.security import middleware

    middleware.install(app)

    @app.get("/x")
    def x(request: Request):
        limits.enforce(request, "unit", 2, 60)
        return {"ok": True}

    with TestClient(app) as c:
        assert c.get("/x").status_code == 200
        assert c.get("/x").status_code == 200
        r = c.get("/x")
        assert r.status_code == 429
        assert r.json()["error"] == "rate_limited" and "message" in r.json()
        assert int(r.headers["retry-after"]) >= 1


def test_hit_is_non_raising(saas_db):
    from saas.security import limits

    limits.reset_state()
    assert limits.hit(None, "nr", 1, 60, key="k") is True
    assert limits.hit(None, "nr", 1, 60, key="k") is False
