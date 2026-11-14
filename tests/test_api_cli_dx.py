"""TDD tests for API validation/bounds, multi-channel endpoints, and CLI DX (clip.py & seed.py)."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import scheduler
from app.clipper import job as clip_job_mod
from app.config import settings
from app.db import get_db, init_db, session_scope
from app.main import app
from app.models import Channel, ClipJob, MetadataDraft, Video, VideoStatus
from app.schemas import PublishBody, ScrapeBody, TransformBody, VideoOut
from scripts import clip as clip_cli
from scripts import seed as seed_cli


@pytest.fixture(autouse=True)
def _clean_db():
    """Ensure tables exist and isolate test rows created during each test."""
    init_db()
    with session_scope() as db:
        db.execute(delete(MetadataDraft))
        db.execute(delete(ClipJob))
        db.execute(delete(Video))
        db.execute(delete(Channel).where(Channel.slug == "tech"))
    yield
    app.dependency_overrides.clear()
    with session_scope() as db:
        db.execute(delete(MetadataDraft))
        db.execute(delete(ClipJob))
        db.execute(delete(Video))
        db.execute(delete(Channel).where(Channel.slug == "tech"))


def _ensure_tech_channel(require_approval: bool = True) -> str:
    from app.growth import channels

    with session_scope() as db:
        channels._ensure_seeded(db)
        ch = channels.channel_by_slug(db, "tech")
        if ch is None:
            ch = Channel(
                slug="tech",
                name="Tech Shorts",
                niche="tech",
                brand_name="@techshorts",
                uploads_per_day=3,
                timezone="UTC",
                require_approval=require_approval,
                enabled=True,
            )
            db.add(ch)
            db.flush()
        else:
            ch.require_approval = require_approval
            ch.enabled = True
            db.flush()
        return ch.id


def _make_video(
    *,
    source: str = "tiktok",
    source_id: str | None = None,
    status: VideoStatus = VideoStatus.PENDING,
    channel_id: str | None = None,
    local_path: str | None = None,
    transformed_path: str | None = None,
) -> str:
    with session_scope() as db:
        v = Video(
            source=source,
            source_id=source_id or f"vid_{uuid.uuid4().hex[:8]}",
            source_url="https://example.com/v",
            author="@creator",
            author_name="Creator",
            title="Sample Title",
            description="Sample desc",
            views=10_000,
            likes=500,
            comments=20,
            shares=30,
            duration_sec=25,
            status=status,
            channel_id=channel_id,
            local_path=local_path,
            transformed_path=transformed_path,
        )
        db.add(v)
        db.flush()
        return v.id


def test_list_endpoints_enforce_bounds_on_limit_and_offset():
    client = TestClient(app)

    for bad_limit in (0, -1, 500):
        assert client.get(f"/videos/pending?limit={bad_limit}").status_code == 422
        assert client.get(f"/videos?limit={bad_limit}").status_code == 422
        assert client.get(f"/creators?limit={bad_limit}").status_code == 422
        assert client.get(f"/clip/jobs?limit={bad_limit}").status_code == 422

    for bad_hist_limit in (0, -5, 1000):
        assert client.get(f"/scheduler/history?limit={bad_hist_limit}").status_code == 422

    assert client.get("/videos?offset=-1").status_code == 422
    assert client.get("/creators?offset=-1").status_code == 422


def test_videos_filtering_case_insensitive_status_channel_scope_and_nullable_orm():
    tech_id = _ensure_tech_channel()
    legacy_vid = _make_video(source_id="leg_1", status=VideoStatus.PENDING, channel_id=None)
    tech_vid = _make_video(source_id="tech_1", status=VideoStatus.PENDING, channel_id=tech_id)
    _make_video(source_id="appr_1", status=VideoStatus.APPROVED, channel_id=tech_id)

    client = TestClient(app)

    # Case-insensitive & whitespace-tolerant status query
    r_upper = client.get("/videos?status=APPROVED")
    assert r_upper.status_code == 200
    assert [row["source_id"] for row in r_upper.json()] == ["appr_1"]

    r_bad = client.get("/videos?status=nonexistent")
    assert r_bad.status_code == 400
    assert "pending" in r_bad.json()["detail"]

    # Channel filtering on /videos and /videos/pending
    r_tech_pending = client.get("/videos/pending?channel=tech")
    assert r_tech_pending.status_code == 200
    assert [row["id"] for row in r_tech_pending.json()] == [tech_vid]
    assert r_tech_pending.json()[0]["channel_id"] == tech_id

    r_def_pending = client.get("/videos/pending?channel=default")
    assert r_def_pending.status_code == 200
    assert [row["id"] for row in r_def_pending.json()] == [legacy_vid]

    r_tech_all = client.get("/videos?channel=tech")
    assert r_tech_all.status_code == 200
    assert {row["source_id"] for row in r_tech_all.json()} == {"tech_1", "appr_1"}

    assert client.get("/videos?channel=missing_slug").status_code == 404
    assert client.get("/videos/pending?channel=missing_slug").status_code == 404

    # VideoOut coerces nullable legacy attributes without 500 ValidationError
    from types import SimpleNamespace

    legacy_obj = SimpleNamespace(
        id="v1",
        source="tiktok",
        source_id="s1",
        source_url="https://example.com/1",
        author=None,
        author_name=None,
        title=None,
        description=None,
        thumbnail_url=None,
        media_url=None,
        local_path=None,
        transformed_path=None,
        youtube_video_id=None,
        language=None,
        keywords=None,
        views=None,
        likes=None,
        comments=None,
        shares=None,
        duration_sec=None,
        width=None,
        height=None,
        engagement_score=None,
        velocity_score=None,
        reach_score=None,
        viral_score=None,
        status="pending",
        channel_id=tech_id,
        meta=None,
        error=None,
        source_published_at=None,
        scraped_at=datetime.now(timezone.utc),
        curated_at=None,
        transformed_at=None,
        published_at=None,
    )
    validated = VideoOut.model_validate(legacy_obj)
    assert validated.author == ""
    assert validated.author_name == ""
    assert validated.language == ""
    assert validated.views == 0
    assert validated.viral_score == 0.0
    assert validated.channel_id == tech_id


def test_schema_bounds_on_transform_publish_and_scrape():
    with pytest.raises(Exception):
        TransformBody(speed_factor=0.0)
    with pytest.raises(Exception):
        TransformBody(speed_factor=-1.0)
    with pytest.raises(Exception):
        TransformBody(speed_factor=3.0)
    assert TransformBody(speed_factor=1.25).speed_factor == 1.25

    with pytest.raises(Exception):
        PublishBody(privacy="secret")
    assert PublishBody(privacy=" PUBLIC ").privacy == "public"
    assert PublishBody(privacy=None).privacy is None

    with pytest.raises(Exception):
        ScrapeBody(max_per_run=0)
    with pytest.raises(Exception):
        ScrapeBody(max_per_run=-5)
    with pytest.raises(Exception):
        ScrapeBody(max_per_run=500)
    assert ScrapeBody(max_per_run=25).max_per_run == 25

    vid = _make_video(status=VideoStatus.TRANSFORMED)
    client = TestClient(app)
    assert client.post(f"/videos/{vid}/publish", json={"privacy": "friends_only"}).status_code == 422
    assert client.post("/scrape", json={"max_per_run": 0}).status_code == 422


def test_metadata_endpoints_handle_null_timestamps_and_partial_updates():
    import app.main as main_mod

    assert not hasattr(main_mod, "MetadataBody"), "dead MetadataBody class stub should be removed"

    vid = _make_video()
    with session_scope() as db:
        md = MetadataDraft(
            video_id=vid,
            title="Original Title",
            description="Original Desc",
            tags=["one"],
        )
        db.add(md)

    # Simulate a migrated DB row whose generated_at column is NULL in memory
    from types import SimpleNamespace

    class _FakeScalarResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                title="Migrated Title",
                description="Migrated Desc",
                tags=["legacy"],
                detected_language="en",
                edited=False,
                generated_at=None,
            )

    class _FakeNullTimestampSession:
        def get(self, model, pk):
            return SimpleNamespace(id=pk)

        def execute(self, stmt):
            return _FakeScalarResult()

    assert main_mod.get_metadata(vid, db=_FakeNullTimestampSession())["generated_at"] is None

    client = TestClient(app)
    got = client.get(f"/metadata/{vid}")
    assert got.status_code == 200
    assert got.json()["exists"] is True
    assert isinstance(got.json()["generated_at"], str)

    # Passing title=None must NOT overwrite title with literal "None"
    updated = client.put(
        f"/metadata/{vid}",
        json={"title": None, "description": "Updated Desc", "tags": [" shorts ", "", "  ", "tech"]},
    )
    assert updated.status_code == 200
    after = client.get(f"/metadata/{vid}").json()
    assert after["title"] == "Original Title"
    assert after["description"] == "Updated Desc"
    assert after["tags"] == ["shorts", "tech"]

    # Non-list tags rejected with 422 instead of silently ignored
    assert client.put(f"/metadata/{vid}", json={"tags": "not-a-list"}).status_code == 422


def test_download_and_chop_reject_directory_paths_cleanly(tmp_path):
    vid = _make_video(local_path=str(tmp_path), transformed_path=str(tmp_path))
    client = TestClient(app)

    dl = client.get(f"/videos/{vid}/download")
    assert dl.status_code == 404

    chop = client.post(f"/videos/{vid}/chop")
    assert chop.status_code == 400


def test_publish_config_validates_inputs_parses_string_bools_and_isolates_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "uploads_per_day", 8)
    monkeypatch.setattr(settings, "upload_interval_hours", 3)
    monkeypatch.setattr(settings, "upload_jitter_minutes", 45)
    monkeypatch.setattr(settings, "auto_approve", True)
    monkeypatch.setattr(scheduler, "reschedule_publish", lambda: {"rescheduled": True})

    client = TestClient(app)

    for bad_payload in (
        {"uploads_per_day": "abc"},
        {"uploads_per_day": True},
        {"uploads_per_day": 0},
        {"uploads_per_day": 25},
        {"upload_jitter_minutes": "bad"},
        {"upload_jitter_minutes": False},
        {"upload_jitter_minutes": -1},
        {"upload_jitter_minutes": 121},
        {"auto_approve": "maybe"},
        {"auto_approve": [1, 2]},
    ):
        resp = client.post("/config/publish", json=bad_payload)
        assert resp.status_code == 400, f"expected 400 for {bad_payload}, got {resp.status_code}"

    # String "false" must set auto_approve=False (never bool("false") == True!)
    monkeypatch.setenv("DOOMSCROLLER_ENV_FILE", "")
    r_false = client.post("/config/publish", json={"auto_approve": "false"})
    assert r_false.status_code == 200
    assert r_false.json()["auto_approve"] is False
    assert settings.auto_approve is False

    # When DOOMSCROLLER_ENV_FILE points to a custom .env, persist cleanly
    env_file = tmp_path / "custom.env"
    env_file.write_text("UPLOADS_PER_DAY=8\nUPLOAD_INTERVAL_HOURS=3\n", encoding="utf-8")
    monkeypatch.setenv("DOOMSCROLLER_ENV_FILE", str(env_file))

    r_ok = client.post(
        "/config/publish",
        json={"uploads_per_day": 4, "upload_jitter_minutes": 15, "auto_approve": "true"},
    )
    assert r_ok.status_code == 200
    body = r_ok.json()
    assert body["uploads_per_day"] == 4
    assert body["upload_interval_hours"] == 6
    assert body["upload_jitter_minutes"] == 15
    assert body["auto_approve"] is True

    written = env_file.read_text(encoding="utf-8")
    assert "UPLOADS_PER_DAY=4" in written
    assert "UPLOAD_INTERVAL_HOURS=6" in written
    assert "UPLOAD_JITTER_MINUTES=15" in written
    assert "AUTO_APPROVE=true" in written


def test_storage_cleanup_supports_dry_run_and_queue_focus_uses_di(monkeypatch):
    monkeypatch.setattr(scheduler, "cleanup_job", lambda dry_run=False: {"dry_run": dry_run, "orphans_deleted": 2})
    client = TestClient(app)

    res = client.post("/storage/cleanup?dry_run=true")
    assert res.status_code == 200
    assert res.json()["dry_run"] is True
    assert res.json()["result"] == {"dry_run": True, "orphans_deleted": 2}

    used_override = []

    def _override_db():
        used_override.append(True)
        with session_scope() as s:
            yield s

    app.dependency_overrides[get_db] = _override_db
    focus = client.get("/queue/focus")
    assert focus.status_code == 200
    assert used_override == [True]


def test_clip_api_supports_target_channel_filters_retry_and_dismiss(monkeypatch):
    tech_id = _ensure_tech_channel(require_approval=False)
    monkeypatch.setattr(
        clip_job_mod,
        "clip_source",
        lambda source, **kw: {"ok": True, "clips": [], "source": source, "channel_id": kw.get("channel_id")},
    )

    client = TestClient(app)
    assert client.post("/clip", json={"source": "https://youtu.be/abc", "channel": "unknown_chan"}).status_code == 404

    created = client.post("/clip", json={"source": "https://youtu.be/abc", "count": 2, "channel": "tech"})
    assert created.status_code == 200
    assert created.json()["channel_slug"] == "tech"
    job_id = created.json()["job_id"]

    # GET /clip/jobs/{job_id} must include channel_slug just like GET /clip/jobs
    single = client.get(f"/clip/jobs/{job_id}")
    assert single.status_code == 200
    assert single.json()["channel_slug"] == "tech"
    assert single.json()["auto_queue"] is True  # derived from tech channel's require_approval=False

    # Add a failed job to test status/channel filters, retry, and dismiss
    with session_scope() as db:
        failed_job = ClipJob(source="https://youtu.be/fail1", status="failed", error="network", channel_id=tech_id)
        running_job = ClipJob(source="https://youtu.be/run1", status="running", stage="downloading")
        db.add_all([failed_job, running_job])
        db.flush()
        failed_id = failed_job.id
        running_id = running_job.id

    assert client.get("/clip/jobs?status=bogus").status_code == 400
    filtered = client.get("/clip/jobs?status=failed&channel=tech")
    assert filtered.status_code == 200
    assert [j["id"] for j in filtered.json()] == [failed_id]

    # Dismiss & Retry guards while running_job is still running (before retry drains queue)
    assert client.delete("/clip/jobs/missing").status_code == 404
    assert client.delete(f"/clip/jobs/{running_id}").status_code == 400
    assert client.post("/clip/jobs/missing/retry").status_code == 404
    assert client.post(f"/clip/jobs/{running_id}/retry").status_code == 400

    retried = client.post(f"/clip/jobs/{failed_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["id"] == failed_id

    dismissed = client.delete(f"/clip/jobs/{job_id}")
    assert dismissed.status_code == 200
    assert dismissed.json()["dismissed"] is True


def test_filtered_clip_jobs_hide_soft_dismissed_rows_and_fill_limit_until_retried(monkeypatch):
    tech_id = _ensure_tech_channel(require_approval=False)
    monkeypatch.setattr(clip_job_mod, "clip_queue_job", lambda: {"processed": 0, "failed": 0})
    now = datetime.now(timezone.utc)

    with session_scope() as db:
        older_visible_done = ClipJob(
            source="https://youtu.be/keep_done",
            status="done",
            channel_id=tech_id,
            result={"ok": True, "clips": []},
            created_at=now - timedelta(minutes=10),
        )
        newer_dismissed_done = ClipJob(
            source="https://youtu.be/hide_done",
            status="done",
            channel_id=tech_id,
            result={"ok": True, "clips": []},
            created_at=now - timedelta(minutes=2),
        )
        dismissed_failed = ClipJob(
            source="https://youtu.be/hide_failed",
            status="failed",
            error="transient",
            channel_id=tech_id,
            result={"ok": False, "error": "transient"},
            created_at=now - timedelta(minutes=1),
        )
        default_done = ClipJob(
            source="https://youtu.be/default_done",
            status="done",
            channel_id=None,
            result={"ok": True, "clips": []},
            created_at=now,
        )
        db.add_all([older_visible_done, newer_dismissed_done, dismissed_failed, default_done])
        db.flush()
        keep_done_id = older_visible_done.id
        hide_done_id = newer_dismissed_done.id
        hide_failed_id = dismissed_failed.id
        default_done_id = default_done.id

    client = TestClient(app)

    # Dismiss individual terminal jobs via DELETE /clip/jobs/{id} (soft-dismiss preserves DB rows)
    assert client.delete(f"/clip/jobs/{hide_done_id}").status_code == 200
    assert client.delete(f"/clip/jobs/{hide_failed_id}").status_code == 200
    assert client.delete(f"/clip/jobs/{default_done_id}").status_code == 200

    with session_scope() as db:
        assert db.get(ClipJob, hide_done_id) is not None
        assert db.get(ClipJob, hide_done_id).result.get("dismissed") is True

    # Filtered GET /clip/jobs?status=..., ?channel=..., and ?status=...&channel=...&limit=1
    # must hide soft-dismissed rows and paginate past newer dismissed rows to fill limit=1.
    r_status_done = client.get("/clip/jobs?status=done")
    assert r_status_done.status_code == 200
    assert [j["id"] for j in r_status_done.json()] == [keep_done_id]

    r_status_failed = client.get("/clip/jobs?status=failed")
    assert r_status_failed.status_code == 200
    assert r_status_failed.json() == []

    r_chan_tech = client.get("/clip/jobs?channel=tech")
    assert r_chan_tech.status_code == 200
    assert [j["id"] for j in r_chan_tech.json()] == [keep_done_id]

    r_chan_default = client.get("/clip/jobs?channel=default")
    assert r_chan_default.status_code == 200
    assert r_chan_default.json() == []

    r_both_limit1 = client.get("/clip/jobs?status=done&channel=tech&limit=1")
    assert r_both_limit1.status_code == 200
    assert [j["id"] for j in r_both_limit1.json()] == [keep_done_id]

    # Retrying a soft-dismissed failed job clears dismissed and restores visibility on filtered list
    retried = client.post(f"/clip/jobs/{hide_failed_id}/retry")
    assert retried.status_code == 200
    r_queued_tech = client.get("/clip/jobs?status=queued&channel=tech")
    assert r_queued_tech.status_code == 200
    assert [j["id"] for j in r_queued_tech.json()] == [hide_failed_id]


def test_clip_cli_json_purity_channel_routing_validation_and_jobs_inspection(monkeypatch, capsys):
    tech_id = _ensure_tech_channel(require_approval=True)
    calls: list[dict] = []

    def _fake_clip_source(source, **kwargs):
        if kwargs.get("progress"):
            kwargs["progress"]("downloading")
            kwargs["progress"]("rendering clip 1 of 1")
        calls.append({"source": source, **kwargs})
        return {
            "ok": True,
            "source": source,
            "kind": "speech",
            "queued": bool(kwargs.get("auto_queue")),
            "clips": [
                {
                    "id": "c1",
                    "path": "content/clips/c1.mp4",
                    "title": "Sharp Hook",
                    "hook": "Wait for this",
                    "start": 10.0,
                    "end": 35.0,
                    "duration": 25.0,
                    "score": 0.88,
                    "kind": "speech",
                    "reason": "strong payoff",
                }
            ],
            "warnings": ["minor audio dip"],
        }

    monkeypatch.setattr(clip_cli, "clip_source", _fake_clip_source)

    # 1. --json must output PURE parseable JSON on stdout (progress sent to stderr, no trailing prose)
    rc = clip_cli.main(["https://youtu.be/demo", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert parsed["ok"] is True
    assert len(parsed["clips"]) == 1
    assert "downloading" in captured.err

    # 2. --channel <slug> & --mine route properly and human output has no stale Streamlit reference
    calls.clear()
    rc2 = clip_cli.main(["https://youtu.be/demo2", "--channel", "tech", "--count", "2", "--mine"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert calls[-1]["channel_id"] == tech_id
    assert calls[-1]["count"] == 2
    assert calls[-1]["mine"] is True
    assert calls[-1]["auto_queue"] is False  # tech channel has require_approval=True
    assert "dashboard's Pending tab" not in out2
    assert "tech" in out2

    # 3. Validation errors: bad --count, missing source, unknown --channel
    assert clip_cli.main(["https://youtu.be/demo", "--count", "0"]) == 1
    assert clip_cli.main(["https://youtu.be/demo", "--count", "-2"]) == 1
    assert clip_cli.main(["https://youtu.be/demo", "--channel", "missing_chan"]) == 1
    assert clip_cli.main([]) == 1
    capsys.readouterr()

    # 4. Queue inspection via --jobs / --status (no positional source needed)
    with session_scope() as db:
        db.add(ClipJob(source="https://youtu.be/q1", status="queued", channel_id=tech_id))
        db.add(ClipJob(source="https://youtu.be/d1", status="done", channel_id=None))

    rc_jobs = clip_cli.main(["--jobs"])
    jobs_out = capsys.readouterr().out
    assert rc_jobs == 0
    assert "https://youtu.be/q1" in jobs_out
    assert "https://youtu.be/d1" in jobs_out

    rc_status_json = clip_cli.main(["--status", "--channel", "tech", "--json"])
    status_json = json.loads(capsys.readouterr().out)
    assert rc_status_json == 0
    assert status_json["counts"]["queued"] == 1
    assert len(status_json["jobs"]) == 1
    assert status_json["jobs"][0]["channel_slug"] == "tech"


def test_seed_cli_populates_all_fields_respects_exact_count_dedups_and_supports_channel(tmp_path, monkeypatch, capsys):
    tech_id = _ensure_tech_channel()
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))

    # 1. Exact count, rich Video fields, real source_published_at, channel assignment, and --json output
    rc = seed_cli.main(["5", "--channel", "tech", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["created"] == 5
    assert out["channel"] == "tech"

    with session_scope() as db:
        rows = db.query(Video).all()
        assert len(rows) == 5
        sample = rows[0]
        assert sample.channel_id == tech_id
        assert sample.author_name != ""
        assert sample.language == "en"
        assert sample.width == 1080 and sample.height == 1920
        assert len(sample.keywords) > 0
        assert sample.media_url is not None
        assert sample.source_published_at is not None
        # Age decay means velocity_score is strictly below engagement_score for aged mock items
        assert sample.velocity_score < sample.engagement_score

    # 2. Duplicate (source, source_id) across handles & hashtags must never raise IntegrityError
    from app.scrapers.base import ScrapedItem

    dup_item = ScrapedItem(
        source="snapchat",
        source_id="dup_123",
        source_url="https://example.com/dup",
        author="dup_user",
        author_name="Dup User",
        title="Dup",
        description="desc",
        thumbnail_url=None,
        media_url="https://example.com/dup.mp4",
        local_path=None,
        views=100_000,
        likes=5_000,
        comments=100,
        shares=200,
        duration_sec=15,
        published_at=datetime.now(timezone.utc) - timedelta(hours=12),
    )
    monkeypatch.setattr(seed_cli.MockScraper, "scrape_handles", lambda self, handles, max_per_run, download=True: [dup_item])
    monkeypatch.setattr(seed_cli.MockScraper, "scrape_hashtags", lambda self, hashtags, max_per_run, download=True: [dup_item])

    assert seed_cli.main(2) == 0
    capsys.readouterr()

    # 3. Invalid CLI inputs return exit code 1 cleanly
    assert seed_cli.main(["0"]) == 1
    assert seed_cli.main(["-3"]) == 1
    assert seed_cli.main(["--channel", "unknown_slug"]) == 1
    capsys.readouterr()


def test_idle_import_of_app_main_never_loads_numpy():
    code = "import sys, app.main; assert 'numpy' not in sys.modules, 'numpy imported at idle'"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
