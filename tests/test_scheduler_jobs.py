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


def test_cleanup_reclaims_expired_rejected_distinct_transform_protects_active_longjob_and_storage_counts_thumbs(
    tmp_path, monkeypatch
):
    import os
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete
    from app.config import settings
    from app.db import init_db, session_scope
    from app.models import ClipJob, LongJob, Video, VideoStatus

    init_db()
    with session_scope() as db:
        for tbl in (LongJob, ClipJob, Video):
            db.execute(delete(tbl))

    content_root = tmp_path / "content"
    (content_root / "transformed").mkdir(parents=True)
    (content_root / "long").mkdir(parents=True)
    live_thumbs = content_root / "longform" / "default" / "live_thumbs"
    live_thumbs.mkdir(parents=True)

    rej_distinct = content_root / "transformed" / "rej_distinct.mp4"
    rej_distinct.write_bytes(b"R" * 1024)
    live_mp4 = content_root / "longform" / "default" / "live.mp4"
    live_mp4.write_bytes(b"L" * 1024)
    live_thumb = live_thumbs / "thumb_v1.jpg"
    live_thumb.write_bytes(b"T" * 256)
    live_hero = live_thumbs / "hero_raw.jpg"
    live_hero.write_bytes(b"H" * 256)
    cached_src = content_root / "long" / "cached_src.mp4"
    cached_src.write_bytes(b"C" * 1024)

    old_ts = datetime.now(timezone.utc) - timedelta(days=14)
    old_epoch = old_ts.timestamp()
    os.utime(cached_src, (old_epoch, old_epoch))

    monkeypatch.setattr(settings, "content_dir", str(content_root))
    monkeypatch.setattr(settings, "source_video_max_age_days", 7)

    with session_scope() as db:
        v_rej = Video(
            source="youtube",
            source_id="rej1",
            source_url="https://youtu.be/rej1",
            title="Rejected Distinct",
            local_path=None,
            transformed_path=str(rej_distinct),
            status=VideoStatus.REJECTED,
            curated_at=old_ts,
            meta={"clip_rendered": True},
        )
        v_live = Video(
            source="longform",
            source_id="lf_live",
            source_url="https://youtu.be/lf_live",
            title="Live Long",
            transformed_path=str(live_mp4),
            status=VideoStatus.PENDING,
            meta={
                "long_form": True,
                "hero_frame": str(live_hero),
                "selected_thumbnail": str(live_thumb),
                "thumbnails": [{"id": "v1", "path": str(live_thumb)}],
            },
        )
        lj = LongJob(format="best_of", status="running")
        db.add_all([v_rej, v_live, lj])
        db.flush()
        v_rej_id, lj_id = v_rej.id, lj.id

    rep = scheduler.storage_report()
    assert rep["orphan_files"] == 0

    res1 = scheduler.cleanup_job()
    assert not rej_distinct.exists()
    assert res1["transforms_deleted"] >= 1
    assert cached_src.exists()
    assert res1["cache_deleted"] == 0
    with session_scope() as db:
        assert db.get(Video, v_rej_id).transformed_path is None
        db.get(LongJob, lj_id).status = "done"

    res2 = scheduler.cleanup_job()
    assert not cached_src.exists()
    assert res2["cache_deleted"] >= 1
