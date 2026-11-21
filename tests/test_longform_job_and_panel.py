"""Tests for LongJob queue, RENDER_LOCK, strict Shorts/Long-Form isolation, autopilot, panel routes, cleanup & CLI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import scheduler
from app.clipper import job as clip_job
from app.config import settings
from app.db import init_db, session_scope
from app.growth import approval, autopilot, channels, learn, safety
from app.longform import job as long_job, planner, render as lf_render
from app.models import Channel, GrowthEvent, GrowthState, LongJob, MetadataDraft, QuotaUsage, Video, VideoStatus
from app.review import review_app


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    init_db()
    with session_scope() as db:
        for tbl in (LongJob, GrowthEvent, GrowthState, QuotaUsage, MetadataDraft, Video, Channel):
            db.execute(delete(tbl))
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    (tmp_path / "channels.json").write_text(json.dumps({
        "channels": [
            {"slug": "default", "niche": "comedy", "long_uploads_per_day": 1, "long_auto_publish": False},
            {"slug": "indianvlogs", "name": "Mr Indian Vlogs", "niche": "hindi", "uploads_per_day": 3,
             "long_uploads_per_day": 1, "long_auto_publish": False, "timezone": "Asia/Kolkata",
             "feeds": ["https://www.youtube.com/@souravjvlogs", "https://www.youtube.com/@FukraInsaan"]},
        ]
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "review_token", "test-secret")
    monkeypatch.setattr(settings, "use_mock", True)


def _signed_client() -> TestClient:
    client = TestClient(review_app)
    assert client.get("/?key=test-secret", follow_redirects=False).status_code == 303
    return client


def test_real_channels_json_is_valid_and_sets_both_channels_to_1_long_per_day(monkeypatch):
    real_cfg = Path(__file__).resolve().parent.parent / "channels.json"
    monkeypatch.setattr(settings, "channels_file", str(real_cfg))
    entries = channels._load_config()
    assert entries is not None
    by_slug = {e["slug"]: e for e in entries}
    assert by_slug["default"]["long_uploads_per_day"] == 1
    assert by_slug["default"]["long_auto_publish"] is False
    assert by_slug["indianvlogs"]["long_uploads_per_day"] == 1
    assert by_slug["indianvlogs"]["long_auto_publish"] is False


def test_enqueue_run_worker_isolation_and_render_lock(tmp_path, monkeypatch):
    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "indianvlogs")

    # 1. Verify RENDER_LOCK contention skips without blocking.
    clip_job.RENDER_LOCK.acquire()
    try:
        assert long_job.run_next_long_job() == {"busy": True}
    finally:
        clip_job.RENDER_LOCK.release()

    # 2. Enqueue + run mocked build.
    job_id = long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs", target_minutes=8)

    from app.longform import thumbnails as lf_thumbs

    seg = planner.SegmentPlan("s.mp4", "https://youtu.be/abc", "abc", "@souravjvlogs", "Sourav", "Vlog 1", 5.0, 45.0, 90.0, "Funny Moment", "hi")
    fake_plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",), theme="",
        title="Best of @souravjvlogs", description="Hilarious highlights", tags=("hindi vlog",),
        language="hi", target_seconds=480.0, cold_open=seg, segments=(seg,),
    )
    monkeypatch.setattr(planner, "plan_compilation", lambda **k: fake_plan)

    def fake_render(plan, out_path, **k):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"MP4" * 500)
        return lf_render.RenderResult(
            output_path=out_path, duration=390.0, chapters=((0.0, "Intro / Preview"), (5.0, "#1 Funny Moment")),
            chapter_text="0:00 Intro / Preview\n0:05 #1 Funny Moment", timestamps_for_thumbs=(15.0,),
        )

    def fake_thumbs(vp, *, out_dir, **k):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        res = []
        for i in (1, 2, 3):
            tp = Path(out_dir) / f"thumb_v{i}.jpg"
            tp.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
            res.append({"id": f"v{i}", "label": f"Variant {i}", "path": str(tp)})
        return res, "Test AI prompt"

    monkeypatch.setattr(lf_render, "render_compilation", fake_render)
    monkeypatch.setattr(lf_thumbs, "generate_thumbnails_with_prompt", fake_thumbs)

    out = long_job.run_next_long_job()
    assert out["ok"] is True
    video_id = out["video_id"]

    with session_scope() as db:
        v = db.get(Video, video_id)
        assert v is not None and v.status == VideoStatus.PENDING
        assert v.width == 1920 and v.height == 1080
        assert v.meta["long_form"] is True and v.meta["clip_rendered"] is False
        assert "#Shorts" not in v.title

        # Strict isolation: Shorts approval/autopilot/learn ignore long_form rows!
        assert [x.id for x in approval.awaiting_review(db)] == []
        assert [x.id for x in approval.awaiting_scraped(db)] == []
        assert [x.id for x in autopilot.ready_clips(db)] == []

        # Supply respects stock cap & skips while 1 video is already waiting.
    # Add second waiting long video -> supply stops at cap=2.
    with session_scope() as db:
        p2 = Path(settings.content_dir) / "longform" / "indianvlogs" / "v2.mp4"
        p2.parent.mkdir(parents=True, exist_ok=True)
        p2.write_bytes(b"x" * 100)
        db.add(Video(
            source="longform", source_id="v2", source_url="https://youtu.be/v2", title="V2", transformed_path=str(p2),
            status=VideoStatus.TRANSFORMED, channel_id=ch.id, meta={"long_form": True},
        ))
    res_sup = long_job.long_supply_job()
    assert res_sup["indianvlogs"]["queued"] is False and res_sup["indianvlogs"]["stock"] == 2


def test_long_autopilot_respects_daily_cap_and_opt_in_auto_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_publish", True)
    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch.client_secrets = ""  # MockPublisher mode
        mp4 = tmp_path / "long1.mp4"
        mp4.write_bytes(b"x" * 2048)
        thumb = tmp_path / "t.jpg"
        thumb.write_bytes(b"jpg")
        v1 = Video(
            source="longform", source_id="la1", source_url="https://youtu.be/la1", title="Best of @souravjvlogs",
            description="Chapters:\n0:00 Intro", transformed_path=str(mp4),
            status=VideoStatus.PENDING, channel_id=ch.id,
            meta={"long_form": True, "selected_thumbnail": str(thumb)},
        )
        db.add(v1)
        db.flush()
        v1_id = v1.id

    # long_auto_publish=False: PENDING stays waiting.
    r1 = long_job.long_autopilot_job()
    assert r1["indianvlogs"]["skipped"] == "none_ready"

    # Flip long_auto_publish=True -> promotes PENDING and uploads once, second run hits daily_cap!
    channels.update_channel("indianvlogs", long_auto_publish=True)
    r2 = long_job.long_autopilot_job()
    assert r2["indianvlogs"]["ok"] is True
    with session_scope() as db:
        assert db.get(Video, v1_id).status == VideoStatus.PUBLISHED
        evs = db.execute(select(GrowthEvent).where(GrowthEvent.kind == "long_posted")).scalars().all()
        assert len(evs) == 1
        # Confirm matured_results still excludes long_form videos from Shorts A/B modeling.
        assert learn.matured_results(db) == []

    r3 = long_job.long_autopilot_job()
    assert r3["indianvlogs"]["skipped"] == "daily_cap"


def test_long_panel_endpoints_guarded_and_full_lifecycle(tmp_path, monkeypatch):
    # Unauthenticated requests rejected with 401.
    anon = TestClient(review_app)
    for method, path in (
        ("GET", "/api/long/videos"),
        ("POST", "/api/long/build"),
        ("GET", "/api/long/jobs"),
        ("POST", "/api/long/x/approve"),
        ("POST", "/api/long/x/reject"),
        ("POST", "/api/long/x/metadata"),
        ("POST", "/api/long/x/thumbnail"),
    ):
        resp = anon.request(method, path, json={})
        assert resp.status_code == 401

    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    client = _signed_client()

    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "indianvlogs")
        mp4 = tmp_path / "comp.mp4"
        mp4.write_bytes(b"MP4DATA")
        thumbs_dir = tmp_path / "thumbs"
        thumbs_dir.mkdir()
        tpaths = {}
        for vid_key in ("v1", "v2"):
            tp = thumbs_dir / f"{vid_key}.jpg"
            from PIL import Image
            Image.new("RGB", (1280, 720), (100, 100, 200)).save(tp, format="JPEG")
            tpaths[vid_key] = str(tp)
        v = Video(
            source="longform", source_id="panel1", source_url="https://youtu.be/panel1", title="Old Title", description="0:00 Intro",
            transformed_path=str(mp4), status=VideoStatus.PENDING, channel_id=ch.id, width=1920, height=1080,
            meta={
                "long_form": True,
                "thumbnails": [
                    {"id": "v1", "label": "Hero", "path": tpaths["v1"]},
                    {"id": "v2", "label": "Split", "path": tpaths["v2"]},
                ],
                "selected_thumbnail": tpaths["v1"],
                "selected_thumbnail_id": "v1",
            },
        )
        db.add(v)
        db.flush()
        vid = v.id

    assert client.post("/api/long/build", json={"channel_slug": "indianvlogs", "format": "vs", "target": "@A vs @B"}).status_code == 200
    data = client.get("/api/long/videos").json()
    assert len(data["pending"]) == 1 and len(data["jobs"]) == 1
    assert client.get(f"/api/long/{vid}/thumb/v2").status_code == 200
    assert client.post(f"/api/long/{vid}/thumbnail", json={"variant_id": "v2"}).json()["selected_thumbnail_id"] == "v2"

    # Upload custom thumbnail
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (240, 80, 20)).save(buf, format="PNG")
    up = client.post(f"/api/long/{vid}/thumbnail/upload", content=buf.getvalue())
    assert up.status_code == 200 and up.json()["selected_thumbnail_id"] == "custom"

    # Update metadata & approve (no #Shorts!)
    app_res = client.post(f"/api/long/{vid}/approve", json={"title": "Clean New Long Title", "description": "0:00 Intro\n0:10 Best Part", "tags": ["vlog", "funny"]})
    assert app_res.status_code == 200 and app_res.json()["status"] == "transformed"
    with session_scope() as db:
        row = db.get(Video, vid)
        assert row.title == "Clean New Long Title"
        assert "#Shorts" not in row.title
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == vid)).scalar_one()
        assert draft.title == "Clean New Long Title"


def test_cleanup_protects_live_longform_and_reclaims_expired(tmp_path):
    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.default_channel(db)
        root = Path(settings.content_dir) / "longform" / ch.slug
        root.mkdir(parents=True, exist_ok=True)
        live_mp4 = root / "live.mp4"
        live_mp4.write_bytes(b"A" * 5000)
        live_thumb = root / "live_thumbs" / "thumb_v1.jpg"
        live_thumb.parent.mkdir(parents=True, exist_ok=True)
        live_thumb.write_bytes(b"B" * 2000)

        dead_mp4 = root / "old.mp4"
        dead_mp4.write_bytes(b"C" * 5000)
        dead_thumb = root / "old_thumbs" / "thumb_v1.jpg"
        dead_thumb.parent.mkdir(parents=True, exist_ok=True)
        dead_thumb.write_bytes(b"D" * 2000)

        old_ts = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(Video(
            source="longform", source_id="live1", source_url="https://youtu.be/live1", title="Live",
            local_path=str(live_mp4), transformed_path=str(live_mp4),
            status=VideoStatus.PENDING, channel_id=ch.id,
            meta={"long_form": True, "selected_thumbnail": str(live_thumb), "thumbnails": [{"id": "v1", "path": str(live_thumb)}]},
        ))
        db.add(Video(
            source="longform", source_id="dead1", source_url="https://youtu.be/dead1", title="Dead",
            local_path=str(dead_mp4), transformed_path=str(dead_mp4),
            status=VideoStatus.PUBLISHED, published_at=old_ts, channel_id=ch.id,
            meta={"long_form": True, "selected_thumbnail": str(dead_thumb), "thumbnails": [{"id": "v1", "path": str(dead_thumb)}]},
        ))

    res = scheduler.cleanup_job()
    assert res["transforms_deleted"] >= 1
    assert live_mp4.exists() and live_thumb.exists()
    assert not dead_mp4.exists() and not dead_thumb.exists()


def test_compile_cli_enqueue_and_jobs_json(capsys):
    from scripts import compile as compile_cli

    with session_scope() as db:
        channels.sync_channels(db)
    rc = compile_cli.main(["--channel", "indianvlogs", "--format", "best_of", "--creator", "@souravjvlogs", "--minutes", "7", "--enqueue", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["job_id"]
    rc2 = compile_cli.main(["--jobs", "--json"])
    assert rc2 == 0
    jobs_out = json.loads(capsys.readouterr().out)
    assert len(jobs_out["jobs"]) == 1 and jobs_out["jobs"][0]["id"] == out["job_id"]
