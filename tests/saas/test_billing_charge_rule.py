"""The charge rule: a job costs max(ceil(source minutes), 3 x ceil(finished minutes)).

Reserved up front with the longest finished output the options allow (clips x max_clip_seconds); settled on the
real finished length, never above the reservation. Shown in the estimate as `credits` + `basis`."""
from __future__ import annotations

import pytest

from core_fixtures import core, login, make_user  # noqa: F401
from test_jobs_lifecycle import URL, sources, worker  # noqa: F401


@pytest.fixture()
def rule(core, monkeypatch):
    monkeypatch.setattr(core, "finished_minute_multiplier", 3)
    return core


def test_charge_arithmetic(rule):
    from saas.jobs import service

    assert service.charge_for(23 * 60, 3 * 25) == (23, "source")  # the owner's 3-clip shape: source wins
    assert service.charge_for(30 * 60, 8 * 30) == (30, "source")  # 8 x 30 s from 30 min: max(30, 12)
    assert service.charge_for(5 * 60, 10 * 60) == (30, "finished")  # 10 minutes of clips from a 5-minute source
    assert service.charge_for(61, 61) == (6, "finished")  # ceil(61 s) = 2 source; 3 x ceil(61 s) = 6
    q = service.job_quote(300, clips=3, max_clip_seconds=60)
    assert q == {"credits": 9, "basis": "finished", "credits_source": 5, "credits_finished_max": 9}
    assert service.job_quote(1800, clips=3, max_clip_seconds=60)["basis"] == "source"


def test_rule_off_is_source_minutes(core):
    from saas.jobs import service

    assert service.charge_for(300, 600) == (5, "source")


def test_estimate_shows_credits_and_basis(client, rule, saas_db, sources):
    make_user(saas_db, "est-rule@example.com", plan="pro", credits=100)
    h = login(client, "est-rule@example.com")
    sources["meta"]["duration_s"] = 300.0
    r = client.post("/api/jobs/estimate", json={"url": URL, "clips": 10, "max_clip_seconds": 60}, headers=h).json()
    assert (r["credits"], r["basis"], r["credits_source"], r["credits_finished_max"]) == (30, "finished", 5, 30)
    r = client.post("/api/jobs/estimate", json={"url": URL, "clips": 2, "max_clip_seconds": 30}, headers=h).json()
    assert (r["credits"], r["basis"]) == (5, "source")
    sources["meta"]["duration_s"] = 1800.0
    r = client.post("/api/jobs/estimate", json={"url": URL}, headers=h).json()
    assert (r["credits"], r["basis"]) == (30, "source")


def test_reserve_worst_case_then_settle_on_real_output(client, rule, saas_db, sources):
    from saas.billing import ledger
    from saas.jobs.engine import MockEngine

    u = make_user(saas_db, "settle-rule@example.com", plan="pro", credits=100)
    h = login(client, "settle-rule@example.com")
    sources["meta"]["duration_s"] = 300.0  # 5 source minutes
    r = client.post("/api/jobs", json={"source_url": URL, "options": {"clips": 4, "max_clip_seconds": 60}}, headers=h)
    job = r.json()
    assert job["credits_reserved"] == 12 and job["charge_basis"] == "finished"  # 3 x ceil(4 x 60 s / 60)
    assert ledger.balance(saas_db, u.id) == 88
    # MockEngine renders clips of 30, 31, 32, 33 s = 126 s finished: 3 x ceil(2.1) = 9 > 5 source minutes.
    worker(MockEngine()).run_one()
    body = client.get(f"/api/jobs/{job['id']}").json()
    assert body["credits_charged"] == 9 and body["charge_basis"] == "finished"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 91


def test_long_source_settles_on_source_minutes(client, rule, saas_db, sources):
    from saas.billing import ledger
    from saas.jobs.engine import MockEngine

    u = make_user(saas_db, "long-rule@example.com", plan="pro", credits=100)
    h = login(client, "long-rule@example.com")
    sources["meta"]["duration_s"] = 23 * 60.0
    job = client.post("/api/jobs", json={"source_url": URL, "options": {"clips": 3}}, headers=h).json()
    assert job["credits_reserved"] == 23 and job["charge_basis"] == "source"
    worker(MockEngine()).run_one()
    body = client.get(f"/api/jobs/{job['id']}").json()
    assert body["credits_charged"] == 23 and body["charge_basis"] == "source"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 77


def test_free_tier_uses_the_same_rule(client, rule, saas_db, sources):
    from saas.billing import ledger
    from saas.jobs.engine import MockEngine

    u = make_user(saas_db, "free-rule@example.com")
    ledger.grant_free(saas_db, u.id, 10, 10, idempotency_key="free-rule")
    saas_db.commit()
    h = login(client, "free-rule@example.com")
    sources["meta"]["duration_s"] = 120.0
    r = client.post("/api/jobs", json={"source_url": URL, "options": {"clips": 5, "max_clip_seconds": 60}}, headers=h)
    assert r.status_code == 402  # worst case 3 x 5 = 15 minutes exceeds the 10 free minutes
    job = client.post("/api/jobs", json={"source_url": URL, "options": {"clips": 2, "max_clip_seconds": 60}},
                      headers=h).json()
    assert job["funding"] == "free" and job["credits_reserved"] == 6
    worker(MockEngine()).run_one()  # 30 + 31 s = 61 s finished: 3 x ceil(61/60) = 6
    saas_db.expire_all()
    assert ledger.free_balance(saas_db, u.id) == (4, 8)
