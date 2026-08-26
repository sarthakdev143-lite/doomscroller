"""Tests for the scheduler job registry.

The registry exists because the job map behind ``POST /scheduler/run/{job_id}``
used to be maintained by hand and drifted -- ``transform`` was schedulable but
not runnable on demand, and ``chop_long`` pointed at ``cleanup_job``. These
tests pin the two halves together.
"""
from fastapi.testclient import TestClient

from app import scheduler
from app.main import app


def test_every_scheduled_job_is_runnable_on_demand():
    sched = scheduler._make_scheduler()
    try:
        scheduled = {job.id for job in sched.get_jobs()}
    finally:
        # _make_scheduler does not start it, but be explicit.
        if sched.running:  # pragma: no cover
            sched.shutdown(wait=False)
    assert scheduled, "scheduler registered no jobs at all"
    assert scheduled <= set(scheduler.JOB_FUNCTIONS), (
        f"scheduled but not in JOB_FUNCTIONS: {scheduled - set(scheduler.JOB_FUNCTIONS)}"
    )


def test_registry_entries_are_all_callable():
    assert scheduler.JOB_FUNCTIONS
    for job_id, fn in scheduler.JOB_FUNCTIONS.items():
        assert callable(fn), f"{job_id} is not callable"


def test_run_job_rejects_unknown_id():
    import pytest

    with pytest.raises(KeyError):
        scheduler.run_job("no_such_job")


def test_available_endpoint_matches_the_registry():
    with TestClient(app) as client:
        body = client.get("/scheduler/available").json()
    assert body["jobs"] == sorted(scheduler.JOB_FUNCTIONS)


def test_unknown_job_returns_404_listing_valid_ids():
    with TestClient(app) as client:
        response = client.post("/scheduler/run/definitely_not_a_job")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "unknown job" in detail
    # The error tells the caller what it could have asked for.
    assert "scrape" in detail


def test_disabled_jobs_are_still_runnable_on_demand(monkeypatch):
    """A job the scheduler skipped (feature flag off) can still be triggered.

    ``discovery`` is only registered when AUTO_DISCOVERY is true, but the
    endpoint must accept it either way so an operator can run it manually.
    """
    monkeypatch.setattr(scheduler.settings, "auto_discovery", False)
    sched = scheduler._make_scheduler()
    assert "discovery" not in {j.id for j in sched.get_jobs()}
    assert "discovery" in scheduler.JOB_FUNCTIONS
