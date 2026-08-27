"""Tests for job instrumentation, run history, catch-up and disk GC.

These pin down four defects found after a day of unattended operation:
nothing called ``configure_logging`` so app INFO was discarded; the job wrapper
that wrote history was dead code; a sleeping machine silently dropped jobs past
the misfire grace; and cleanup could only ever see files a database row pointed
at, so orphans accumulated forever.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import logging_config, scheduler


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Point the history file at a temp dir so tests never touch logs/."""
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)
    yield tmp_path


# ------------------------------------------------------------- instrumentation
def test_successful_job_is_recorded():
    wrapped = scheduler.instrumented("demo", lambda: {"created": 3})
    assert wrapped() == {"created": 3}
    history = logging_config.read_job_history()
    assert len(history) == 1
    entry = history[0]
    assert entry["job_id"] == "demo"
    assert entry["ok"] is True
    assert entry["result"] == {"created": 3}
    assert "duration_sec" in entry


def test_raising_job_is_recorded_and_does_not_propagate():
    """A crashing job must never take the scheduler down with it."""
    def boom():
        raise RuntimeError("kaboom")

    result = scheduler.instrumented("boomer", boom)()
    assert result["error"] == "kaboom"
    entry = logging_config.read_job_history()[-1]
    assert entry["ok"] is False
    assert "kaboom" in entry["error"]


def test_soft_failure_is_marked_not_ok():
    """Jobs signal failure by returning {"error": ...} rather than raising."""
    scheduler.instrumented("soft", lambda: {"error": "quota exhausted"})()
    assert logging_config.read_job_history()[-1]["ok"] is False


def test_instrumented_preserves_arguments_and_name():
    wrapped = scheduler.instrumented("args", lambda a, b=2: {"sum": a + b})
    assert wrapped(1, b=5) == {"sum": 6}


def test_every_registered_job_is_wrapped_when_scheduled():
    """The original bug: _make_scheduler registered raw functions."""
    sched = scheduler._make_scheduler()
    for job in sched.get_jobs():
        assert getattr(job.func, "__wrapped__", None) is not None, (
            f"{job.id} was scheduled unwrapped, so its runs would go unrecorded"
        )


def test_manual_run_job_is_also_recorded(monkeypatch):
    monkeypatch.setitem(scheduler.JOB_FUNCTIONS, "demo", lambda: {"ok": 1})
    scheduler.run_job("demo")
    assert logging_config.read_job_history()[-1]["job_id"] == "demo"


# ------------------------------------------------------------------- history
def test_history_survives_a_corrupt_line(isolated_history):
    path = logging_config.get_job_history_path()
    path.write_text(
        json.dumps({"job_id": "a", "timestamp": "2026-01-01T00:00:00+00:00", "ok": True})
        + "\nnot json at all\n"
        + json.dumps({"job_id": "b", "timestamp": "2026-01-02T00:00:00+00:00", "ok": True})
        + "\n",
        encoding="utf-8",
    )
    entries = logging_config.read_job_history()
    assert [e["job_id"] for e in entries] == ["a", "b"]


def test_history_filters_by_job():
    scheduler.instrumented("alpha", lambda: {})()
    scheduler.instrumented("beta", lambda: {})()
    assert {e["job_id"] for e in logging_config.read_job_history(job_id="alpha")} == {"alpha"}


def test_missing_history_file_is_not_an_error():
    assert logging_config.read_job_history() == []
    assert logging_config.last_run_times() == {}
    assert logging_config.job_health() == {}


def test_job_health_counts_runs_and_failures():
    scheduler.instrumented("mixed", lambda: {})()
    scheduler.instrumented("mixed", lambda: {"error": "no"})()
    health = logging_config.job_health()["mixed"]
    assert health["runs"] == 2
    assert health["failures"] == 1
    assert health["ok"] is False  # latest run


def test_last_run_times_ignores_failures(isolated_history):
    path = logging_config.get_job_history_path()
    path.write_text(
        json.dumps({"job_id": "j", "timestamp": "2026-01-01T00:00:00+00:00", "ok": True})
        + "\n"
        + json.dumps({"job_id": "j", "timestamp": "2026-06-01T00:00:00+00:00", "ok": False})
        + "\n",
        encoding="utf-8",
    )
    # The failed later run must not count as "we did this recently".
    assert logging_config.last_run_times()["j"].month == 1


# ------------------------------------------------------------------ catch-up
def _write_history(path, job_id: str, when: datetime) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "job_id": job_id, "timestamp": when.isoformat(), "ok": True,
        }) + "\n")


def test_overdue_detects_a_job_missed_during_sleep(isolated_history):
    path = logging_config.get_job_history_path()
    now = datetime.now(timezone.utc)
    # scrape runs every few hours; ten hours asleep means it was skipped.
    _write_history(path, "scrape", now - timedelta(hours=10))
    assert "scrape" in scheduler.overdue_jobs(now=now)


def test_recent_job_is_not_overdue(isolated_history):
    path = logging_config.get_job_history_path()
    now = datetime.now(timezone.utc)
    _write_history(path, "scrape", now - timedelta(minutes=2))
    assert "scrape" not in scheduler.overdue_jobs(now=now)


def test_never_run_job_is_not_overdue(isolated_history):
    # No history at all: the normal first-run schedule already covers it.
    assert scheduler.overdue_jobs() == []


def test_very_stale_job_is_not_resurrected(isolated_history):
    path = logging_config.get_job_history_path()
    now = datetime.now(timezone.utc)
    _write_history(path, "scrape", now - timedelta(days=20))
    # Beyond the catch-up window the work is stale enough to be pointless.
    assert "scrape" not in scheduler.overdue_jobs(now=now)


def test_misfire_grace_tolerates_a_short_suspend():
    sched = scheduler._make_scheduler()
    grace = sched._job_defaults["misfire_grace_time"]
    assert grace >= 3600 * 3, "a short laptop sleep should not drop jobs"


# ---------------------------------------------------------------- disk GC
@pytest.fixture()
def content_dir(tmp_path, monkeypatch):
    """A throwaway content directory with the schema in place.

    cleanup/storage both read the videos table to learn what is referenced.
    """
    from app.config import settings
    from app.db import init_db

    init_db()
    directory = tmp_path / "content"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(type(settings), "content_path",
                        property(lambda self: directory))
    return directory


def _make_file(path, size=1200, age_hours=48):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (stamp, stamp))
    return str(path)


def test_orphans_are_reclaimed(content_dir):
    orphan = _make_file(content_dir / "snap_orphan.mp4")
    result = scheduler.cleanup_job()
    assert result["orphans_deleted"] >= 1
    assert not os.path.exists(orphan)


def test_dry_run_reports_without_deleting(content_dir):
    orphan = _make_file(content_dir / "snap_orphan.mp4")
    result = scheduler.cleanup_job(dry_run=True)
    assert result["dry_run"] is True
    assert result["orphans_deleted"] >= 1
    assert os.path.exists(orphan), "dry run must not delete anything"


def test_recent_files_are_never_treated_as_orphans(content_dir):
    """A download in flight has no row yet and must survive."""
    fresh = _make_file(content_dir / "snap_downloading.mp4", age_hours=0)
    scheduler.cleanup_job()
    assert os.path.exists(fresh)


def test_internal_scaffolding_is_protected(content_dir):
    master = _make_file(content_dir / "_mock_master.mp4")
    scheduler.cleanup_job()
    assert os.path.exists(master), "underscore-prefixed files are internal"


def test_storage_report_counts_orphans(content_dir):
    _make_file(content_dir / "snap_a.mp4", size=2000)
    report = scheduler.storage_report()
    assert report["files"] == 1
    assert report["orphan_files"] == 1
    assert report["total_mb"] >= 0


# ------------------------------------------------------------------ logging
def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    logging_config.configure_logging(force=True)
    first = len([h for h in root.handlers if getattr(h, "_doomscroller", False)])
    logging_config.configure_logging()
    second = len([h for h in root.handlers if getattr(h, "_doomscroller", False)])
    assert first == second, "repeat calls must not duplicate handlers"


def test_configure_logging_lets_info_through():
    logging_config.configure_logging(force=True)
    assert logging.getLogger("app.scheduler").isEnabledFor(logging.INFO)
