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


def test_scheduler_errors_scrub_secrets_in_returns_history_and_logs(tmp_path, monkeypatch, caplog):
    from sqlalchemy import delete
    from app import logging_config
    from app.config import settings
    from app.db import init_db, session_scope
    from app.models import ClipJob, LongJob, Video, VideoStatus

    init_db()
    with session_scope() as db:
        for tbl in (LongJob, ClipJob, Video):
            db.execute(delete(tbl))

    rev = "rv_sched_canary_99"
    proxy_pw = "PROXY_CANARY_88"
    url_key = "URL_CANARY_77"
    proxy_url = f"http://u:{proxy_pw}@proxy:8080"
    secret_msg = f"boom token={rev} {proxy_url} https://api.example/x?key={url_key}"

    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(settings, "review_token", rev)
    monkeypatch.setattr(settings, "proxy_url", proxy_url)

    def assert_clean(text: str) -> None:
        assert rev not in text
        assert proxy_pw not in text
        assert url_key not in text

    with caplog.at_level("INFO"):
        # 1. instrumented() on crash & on returned error dict
        def crash_fn():
            raise RuntimeError(secret_msg)

        res_crash = scheduler.instrumented("scrape", crash_fn)()
        assert "[REDACTED]" in res_crash["error"]
        assert_clean(res_crash["error"])

        res_ret = scheduler.instrumented("discovery", lambda: {"error": secret_msg})()
        assert "[REDACTED]" in res_ret["error"]
        assert_clean(res_ret["error"])

        history_text = logging_config.get_job_history_path().read_text(encoding="utf-8")
        assert_clean(history_text)

        # 2. scrape_job
        monkeypatch.setattr(scheduler, "_scraper_pending", lambda db: 0)
        import tasks.pipeline as pipeline_mod
        monkeypatch.setattr(pipeline_mod, "scrape_task", lambda **kw: (_ for _ in ()).throw(RuntimeError(secret_msg)))
        res = scheduler.scrape_job()
        assert "[REDACTED]" in res["error"]
        assert_clean(res["error"])

        # 3. discovery_job
        monkeypatch.setattr(scheduler.discovery, "discover_creators", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError(secret_msg)))
        res = scheduler.discovery_job()
        assert "[REDACTED]" in res["error"]
        assert_clean(res["error"])

        # 4. transform_job (inner)
        with session_scope() as db:
            db.add(Video(
                source="youtube",
                source_id="tr_err_1",
                source_url="https://youtu.be/tr_err_1",
                title="Tr Err",
                local_path=str(tmp_path / "x.mp4"),
                status=VideoStatus.APPROVED,
            ))
        monkeypatch.setattr(pipeline_mod, "transform_task", lambda vid: (_ for _ in ()).throw(RuntimeError(secret_msg)))
        res = scheduler.transform_job()
        assert len(res["errors"]) == 1
        assert "[REDACTED]" in res["errors"][0]["error"]
        assert_clean(res["errors"][0]["error"])

        # 5. analytics_pull_job (inner)
        with session_scope() as db:
            db.add(Video(
                source="youtube",
                source_id="an_err_1",
                source_url="https://youtu.be/an_err_1",
                title="An Err",
                status=VideoStatus.PUBLISHED,
                youtube_video_id="yt_an_err_1",
            ))
        import app.publishers.youtube_publisher as yt_pub_mod
        monkeypatch.setattr(
            yt_pub_mod.YouTubePublisher,
            "fetch_stats",
            lambda self, ids: (_ for _ in ()).throw(RuntimeError(secret_msg)),
        )
        scheduler.analytics_pull_job()

        # 6. publish_job (metadata warning) + _recover_stuck_publishes
        ready_file = tmp_path / "ready.mp4"
        ready_file.write_bytes(b"mp4")
        with session_scope() as db:
            db.add(Video(
                source="youtube",
                source_id="pub_err_1",
                source_url="https://youtu.be/pub_err_1",
                title="Pub Err",
                transformed_path=str(ready_file),
                status=VideoStatus.TRANSFORMED,
            ))
        monkeypatch.setattr(settings, "enable_publish", True)
        monkeypatch.setattr(settings, "growth_enabled", False)
        monkeypatch.setattr(settings, "auto_publish", True)
        monkeypatch.setattr(settings, "upload_jitter_minutes", 0)
        monkeypatch.setattr(pipeline_mod, "recover_stuck_publishes", lambda: (_ for _ in ()).throw(RuntimeError(secret_msg)))
        monkeypatch.setattr(pipeline_mod, "generate_metadata_for_video", lambda vid: (_ for _ in ()).throw(RuntimeError(secret_msg)))
        monkeypatch.setattr(pipeline_mod, "publish_task", lambda vid, meta=None: {"ok": True})
        scheduler.publish_job()

        # 7. recompute_scores_job
        monkeypatch.setattr(scheduler, "recompute_scores", lambda rows: (_ for _ in ()).throw(RuntimeError(secret_msg)))
        res = scheduler.recompute_scores_job()
        assert "[REDACTED]" in res["error"]
        assert_clean(res["error"])

        # 8. reschedule_publish
        class BadSched:
            def get_job(self, job_id):
                raise RuntimeError(secret_msg)

        monkeypatch.setattr(scheduler, "_scheduler", None)
        res = scheduler.reschedule_publish(BadSched())
        assert "[REDACTED]" in res["error"]
        assert_clean(res["error"])

        # 9. Outer failures for transform_job, auto_approve_job, analytics_pull_job, publish_job, cleanup_job
        monkeypatch.setattr(settings, "auto_approve", True)
        def boom_scope():
            raise RuntimeError(secret_msg)

        monkeypatch.setattr(scheduler, "session_scope", boom_scope)
        for fn in (scheduler.transform_job, scheduler.auto_approve_job, scheduler.publish_job, scheduler.cleanup_job):
            r = fn()
            assert "[REDACTED]" in r["error"]
            assert_clean(r["error"])

        monkeypatch.setattr(
            yt_pub_mod,
            "YouTubePublisher",
            lambda: (_ for _ in ()).throw(RuntimeError(secret_msg)),
        )
        r_an = scheduler.analytics_pull_job()
        assert "[REDACTED]" in r_an["error"]
        assert_clean(r_an["error"])

    assert_clean(caplog.text)


def test_overdue_jobs_respects_feature_gates_and_clamps_intervals(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import logging_config
    from app.config import settings

    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(settings, "growth_enabled", False)
    monkeypatch.setattr(settings, "auto_discovery", False)
    monkeypatch.setattr(settings, "enable_publish", False)
    monkeypatch.setattr(settings, "scrape_interval_hours", 0)
    monkeypatch.setattr(settings, "discovery_interval_hours", -2)
    monkeypatch.setattr(settings, "growth_watch_minutes", 0)

    for job_id in ("scrape", "discovery", "publish", "growth_watch", "growth_autopilot"):
        logging_config.record_job_run(job_id, {"ok": True}, ok=True)

    now_late = datetime.now(timezone.utc) + timedelta(hours=30)
    stale_classic = scheduler.overdue_jobs(now=now_late)
    assert "scrape" in stale_classic
    assert "discovery" not in stale_classic
    assert "publish" not in stale_classic

    monkeypatch.setattr(settings, "growth_enabled", True)
    stale_growth = scheduler.overdue_jobs(now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert "growth_autopilot" in stale_growth
    assert "growth_watch" not in stale_growth

    # Clamped triggers & intervals when <= 0
    monkeypatch.setattr(settings, "growth_enabled", False)
    monkeypatch.setattr(settings, "auto_discovery", True)
    sched = scheduler._make_scheduler()
    try:
        assert sched.get_job("scrape").trigger.interval.total_seconds() >= 3600
        assert sched.get_job("discovery").trigger.interval.total_seconds() >= 3600
    finally:
        if sched.running:
            sched.shutdown(wait=False)

    stale_30m = scheduler.overdue_jobs(now=datetime.now(timezone.utc) + timedelta(minutes=30))
    assert "scrape" not in stale_30m
    assert "discovery" not in stale_30m


def test_analytics_pull_job_orders_newest_first_and_only_boosts_on_non_empty_stats(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete, select
    from app.db import init_db, session_scope
    from app.models import ClipJob, Creator, LongJob, Video, VideoStatus
    import app.publishers.youtube_publisher as yt_pub_mod

    init_db()
    with session_scope() as db:
        for tbl in (LongJob, ClipJob, Creator, Video):
            db.execute(delete(tbl))

        base = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        for i in range(3):
            db.add(Video(
                source="youtube",
                source_id=f"null_src_{i}",
                source_url=f"https://youtu.be/null_{i}",
                title=f"Null {i}",
                author=f"@null_{i}",
                status=VideoStatus.PUBLISHED,
                youtube_video_id=f"yt_null_{i}",
                published_at=None,
                scraped_at=base - timedelta(days=10, hours=i),
            ))
        for i in range(2):
            db.add(Video(
                source="youtube",
                source_id=f"old_src_{i}",
                source_url=f"https://youtu.be/old_{i}",
                title=f"Old {i}",
                author=f"@old_{i}",
                status=VideoStatus.PUBLISHED,
                youtube_video_id=f"yt_old_{i}",
                published_at=base - timedelta(days=5, hours=i),
                scraped_at=base - timedelta(days=5),
            ))
        for i in range(50):
            db.add(Video(
                source="youtube",
                source_id=f"new_src_{i:02d}",
                source_url=f"https://youtu.be/new_{i:02d}",
                title=f"New {i:02d}",
                author=f"@new_{i:02d}",
                status=VideoStatus.PUBLISHED,
                youtube_video_id=f"yt_new_{i:02d}",
                published_at=base + timedelta(hours=i),
                scraped_at=base,
            ))

        for handle in ("null_0", "old_0", "new_00", "new_49"):
            db.add(Creator(platform="youtube", handle=handle, score=10.0))

    # 1. Empty stats -> 0 updates, 0 creator boosts
    monkeypatch.setattr(yt_pub_mod.YouTubePublisher, "fetch_stats", lambda self, ids: {})
    assert scheduler.analytics_pull_job() == {"updated": 0}
    with session_scope() as db:
        scores = {c.handle: c.score for c in db.execute(select(Creator)).scalars().all()}
        assert all(v == 10.0 for v in scores.values())

    # 2. Fetch raises -> 0 updates, 0 creator boosts
    monkeypatch.setattr(
        yt_pub_mod.YouTubePublisher,
        "fetch_stats",
        lambda self, ids: (_ for _ in ()).throw(RuntimeError("api down")),
    )
    assert scheduler.analytics_pull_job() == {"updated": 0}
    with session_scope() as db:
        scores = {c.handle: c.score for c in db.execute(select(Creator)).scalars().all()}
        assert all(v == 10.0 for v in scores.values())

    # 3. Non-empty stats for 1 video -> newest 50 queried, only that video's creator boosted
    seen_ids: list[str] = []

    def fake_fetch(self, ids):
        seen_ids.extend(ids)
        return {"yt_new_49": {"viewCount": 500, "likeCount": 25, "commentCount": 5}}

    monkeypatch.setattr(yt_pub_mod.YouTubePublisher, "fetch_stats", fake_fetch)
    assert scheduler.analytics_pull_job() == {"updated": 1}
    assert len(seen_ids) == 50
    assert all(f"yt_null_{i}" not in seen_ids for i in range(3))
    assert all(f"yt_old_{i}" not in seen_ids for i in range(2))
    assert "yt_new_49" in seen_ids and "yt_new_00" in seen_ids

    with session_scope() as db:
        scores = {c.handle: c.score for c in db.execute(select(Creator)).scalars().all()}
        assert scores["new_49"] == 11.0
        assert scores["new_00"] == 10.0
        assert scores["old_0"] == 10.0
        assert scores["null_0"] == 10.0


def test_instrumented_scrubs_nested_dicts_lists_and_secret_keys(tmp_path, monkeypatch):
    from app import logging_config
    from app.config import settings

    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path / "logs")
    bearer = "CANARY_BEARER_NESTED_42"
    bot_tok = "123456:CANARY_BOT_NESTED_42"
    rev_tok = "CANARY_REV_NESTED_42"
    monkeypatch.setattr(settings, "review_token", rev_tok)

    nested_payload = {
        "ch_one": {"error": f"Authorization: Bearer {bearer} rev={rev_tok}"},
        "details": [f"https://example.com/hook?key={rev_tok}", f"bot{bot_tok}"],
        "access_token": "CANARY_ACCESS_TOK_42",
        "ok": False,
    }
    res = scheduler.instrumented("growth_autopilot", lambda: nested_payload)()
    dumped = repr(res)
    for secret in (bearer, bot_tok, "CANARY_BOT_NESTED_42", rev_tok, "CANARY_ACCESS_TOK_42"):
        assert secret not in dumped
    assert "[REDACTED]" in res["ch_one"]["error"]
    assert res["access_token"] == "[REDACTED]"


def test_analytics_pull_job_skips_non_dict_and_none_counters_without_crashing(monkeypatch):
    from datetime import datetime, timezone
    from sqlalchemy import delete, select
    from app.db import init_db, session_scope
    from app.models import ClipJob, Creator, LongJob, Video, VideoStatus
    import app.publishers.youtube_publisher as yt_pub_mod

    init_db()
    with session_scope() as db:
        for tbl in (LongJob, ClipJob, Creator, Video):
            db.execute(delete(tbl))

        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        db.add_all([
            Video(
                source="youtube", source_id="bad_1", source_url="https://youtu.be/bad_1",
                title="Bad", author="@bad", status=VideoStatus.PUBLISHED,
                youtube_video_id="yt_1", published_at=now, views=5, likes=1, comments=0,
            ),
            Video(
                source="youtube", source_id="none_2", source_url="https://youtu.be/none_2",
                title="None", author="@none", status=VideoStatus.PUBLISHED,
                youtube_video_id="yt_2", published_at=now, views=42, likes=7, comments=2,
            ),
            Video(
                source="youtube", source_id="good_3", source_url="https://youtu.be/good_3",
                title="Good", author="@good", status=VideoStatus.PUBLISHED,
                youtube_video_id="yt_3", published_at=now, views=1, likes=3, comments=4,
            ),
        ])
        db.add_all([
            Creator(platform="youtube", handle="bad", score=10.0),
            Creator(platform="youtube", handle="none", score=10.0),
            Creator(platform="youtube", handle="good", score=10.0),
        ])

    monkeypatch.setattr(
        yt_pub_mod.YouTubePublisher,
        "fetch_stats",
        lambda self, ids: {
            "yt_1": "corrupt_str",
            "yt_2": {"viewCount": None, "likeCount": None, "commentCount": None},
            "yt_3": {"viewCount": 10, "likeCount": None, "commentCount": True},
        },
    )
    assert scheduler.analytics_pull_job() == {"updated": 1}

    with session_scope() as db:
        by_yt = {v.youtube_video_id: v for v in db.execute(select(Video)).scalars().all()}
        assert (by_yt["yt_1"].views, by_yt["yt_1"].likes, by_yt["yt_1"].comments) == (5, 1, 0)
        assert (by_yt["yt_2"].views, by_yt["yt_2"].likes, by_yt["yt_2"].comments) == (42, 7, 2)
        assert (by_yt["yt_3"].views, by_yt["yt_3"].likes, by_yt["yt_3"].comments) == (10, 3, 4)
        scores = {c.handle: c.score for c in db.execute(select(Creator)).scalars().all()}
        assert scores["bad"] == 10.0
        assert scores["none"] == 10.0
        assert scores["good"] == 11.0


def test_analytics_pull_job_clamps_negative_counts_to_zero(monkeypatch):
    from datetime import datetime, timezone
    from sqlalchemy import delete, select
    from app.db import init_db, session_scope
    from app.models import ClipJob, Creator, LongJob, Video, VideoStatus
    import app.publishers.youtube_publisher as yt_pub_mod

    init_db()
    with session_scope() as db:
        for tbl in (LongJob, ClipJob, Creator, Video):
            db.execute(delete(tbl))
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        db.add(
            Video(
                source="youtube", source_id="neg_1", source_url="https://youtu.be/neg_1",
                title="Neg", author="@neg", status=VideoStatus.PUBLISHED,
                youtube_video_id="yt_neg", published_at=now, views=5, likes=1, comments=0,
            )
        )

    monkeypatch.setattr(
        yt_pub_mod.YouTubePublisher,
        "fetch_stats",
        lambda self, ids: {"yt_neg": {"viewCount": -10, "likeCount": -2, "commentCount": -1}},
    )
    assert scheduler.analytics_pull_job() == {"updated": 1}

    with session_scope() as db:
        v = db.execute(select(Video).where(Video.youtube_video_id == "yt_neg")).scalar_one()
        assert (v.views, v.likes, v.comments) == (0, 0, 0)

