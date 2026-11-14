"""The phone control panel's API: channels, status, report and one-off controls, all behind the key."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels, controls
from app.models import Channel, ClipJob, Feed, GrowthEvent, GrowthState, QuotaUsage, Video


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (Feed, Channel, ClipJob, GrowthEvent, GrowthState, QuotaUsage, Video):
            db.execute(delete(table))
    monkeypatch.setattr(settings, "channels_file", "")
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    monkeypatch.setattr(settings, "youtube_client_secrets", "")
    monkeypatch.setattr(settings, "brand_name", "@seduxing")
    monkeypatch.setattr(settings, "review_token", "panel-test-key")
    controls._status.clear()


def _write(tmp_path, monkeypatch, data):
    path = tmp_path / "channels.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(path))


def _client():
    from app.review import review_app

    c = TestClient(review_app)
    assert c.get("/?key=panel-test-key", follow_redirects=False).status_code == 303
    return c


def test_every_api_route_needs_the_key():
    import inspect
    import re
    import subprocess
    import sys
    from fastapi.routing import APIRoute
    from app.review import review_app

    c = TestClient(review_app)  # no key
    checked = 0
    for route in review_app.routes:
        if not isinstance(route, APIRoute) or route.path == "/":
            continue
        assert "_guard(request)" in inspect.getsource(route.endpoint), f"{route.path} missing _guard(request)"
        concrete = re.sub(r"\{[^}]+\}", "x", route.path)
        for method in route.methods or ():
            if method == "GET":
                resp = c.get(concrete)
            elif method == "POST":
                resp = c.post(concrete, json={"slug": "x", "name": "x", "source": "x"})
            else:
                continue
            assert resp.status_code == 401, f"{method} {route.path} returned {resp.status_code}"
            checked += 1
    assert checked >= 20

    # Importing app.review in a fresh interpreter must never pull in numpy
    proc = subprocess.run(
        [sys.executable, "-c", "import sys, app.review as r; assert not hasattr(r, 'ClipJob'); assert 'numpy' not in sys.modules"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_channels_overview_lists_config_counts_and_next_step(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "name": "Tech", "feeds": ["https://www.youtube.com/@mkbhd"]}]})
    with session_scope() as db:
        channels.sync_channels(db)
    rows = _client().get("/api/channels").json()["channels"]
    tech = next(c for c in rows if c["slug"] == "tech")
    assert tech["feeds"] == ["https://www.youtube.com/@mkbhd"]
    assert tech["connected"] is False
    assert "Google OAuth client file" in tech["next_step"]  # the honest first step


def test_add_channel_through_the_panel_then_feed_and_settings(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": []})
    c = _client()
    assert c.post("/api/channels", json={"slug": "tech", "name": "Tech", "uploads": 4}).status_code == 200
    saved = json.loads((tmp_path / "channels.json").read_text())["channels"][0]
    assert saved["slug"] == "tech" and saved["enabled"] is False and saved["uploads_per_day"] == 4

    assert c.post("/api/channels/tech/feeds", json={"add": ["https://www.youtube.com/@mkbhd"]}).status_code == 200
    assert c.post("/api/channels/tech/settings", json={"brand_name": "@techclips"}).status_code == 200
    rows = c.get("/api/channels").json()["channels"]
    tech = next(x for x in rows if x["slug"] == "tech")
    assert tech["brand_name"] == "@techclips" and tech["feeds"] == ["https://www.youtube.com/@mkbhd"]


def test_the_panel_refuses_a_feed_already_on_another_channel(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "feeds": ["https://www.youtube.com/@mkbhd"]}, {"slug": "comedy"}]})
    with session_scope() as db:
        channels.sync_channels(db)
    r = _client().post("/api/channels/comedy/feeds", json={"add": ["https://youtube.com/@MKBHD"]})
    assert r.status_code == 400 and "already listed under 'tech'" in r.json()["detail"]


def test_a_bad_add_is_a_clean_400_not_a_500(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": []})
    r = _client().post("/api/channels", json={"slug": "Bad Slug!"})
    assert r.status_code == 400 and "slug" in r.json()["detail"].lower()


def test_status_endpoint_returns_doctor(tmp_path, monkeypatch):
    out = _client().get("/api/doctor").json()
    assert "ready" in out and isinstance(out["checks"], list)


def test_report_endpoint_returns_a_report():
    out = _client().get("/api/report").json()
    assert "blocks" in out and "events" in out


def test_controls_run_reload_inline_and_report_status(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech"}]})
    c = _client()
    listing = c.get("/api/controls").json()
    assert {a["action"] for a in listing["actions"]} >= {"supply", "watch", "collect", "learn", "reload", "cleanup"}
    assert "storage" in listing and "total_mb" in listing["storage"] and "files" in listing["storage"]
    done = c.post("/api/controls/reload").json()
    assert done["state"] == "done"
    assert c.get("/api/controls").json()["status"]["reload"]["state"] == "done"
    assert c.post("/api/controls/nope").status_code == 404


def test_a_slow_control_runs_in_the_background(monkeypatch):
    calls = []
    monkeypatch.setattr(controls, "_call", lambda action: calls.append(action) or {"snapshots": 3})
    c = _client()
    started = c.post("/api/controls/supply").json()
    assert started["state"] in ("running", "done")
    for _ in range(50):
        if controls.status().get("supply", {}).get("state") == "done":
            break
        time.sleep(0.02)
    assert controls.status()["supply"]["state"] == "done"
    assert controls.status()["supply"]["result"] == "3 stats updated"
    assert calls == ["supply"]


def test_the_page_is_never_cached_so_an_update_shows_at_once():
    from app.review import review_app

    c = TestClient(review_app)
    r = c.get("/?key=panel-test-key", follow_redirects=True)
    assert "no-store" in r.headers.get("cache-control", "")  # a stale copy must not hide a redeploy


def test_resume_all_and_unblock_endpoints():
    c = _client()
    assert c.post("/api/resume-all").json() == {"ok": False}  # nothing paused
    assert c.post("/api/unblock", json={"name": "nobody"}).json() == {"name": "nobody", "lifted": 0}


def test_locked_page_has_a_key_input_for_fresh_tunnel_domains():
    from app.review import review_app

    c = TestClient(review_app)
    r = c.get("/")
    assert r.status_code == 401
    assert "<form" in r.text and 'name="key"' in r.text


def test_clip_and_storage_endpoints_replace_streamlit_with_auth_guard(monkeypatch):
    from app import review
    from app.clipper import job as clip_job
    from app.review import review_app

    assert not hasattr(review, "ClipJob"), "review.py should stay a thin HTTP shell without direct ORM queries"

    unauth = TestClient(review_app)
    assert unauth.get("/api/clip/jobs").status_code == 401
    assert unauth.post("/api/clip", json={"source": "https://youtu.be/abc"}).status_code == 401
    assert unauth.post("/api/controls/cleanup").status_code == 401

    ran_queue = []
    monkeypatch.setattr(clip_job, "start_queue_worker", lambda: ran_queue.append(True))

    c = _client()
    page_html = c.get("/").text
    assert 'id="tab-clip"' in page_html
    assert "@media(min-width:900px)" in page_html

    # Redundant storage endpoints were folded into /api/controls + controls.trigger('cleanup')
    assert c.get("/api/storage").status_code == 404
    assert c.post("/api/storage/cleanup").status_code == 404

    assert c.post("/api/clip", json={"source": "   "}).status_code == 400
    queued = c.post("/api/clip", json={"source": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "count": 2}).json()
    assert queued["status"] == "queued" and queued["job_id"]
    assert ran_queue == [True]

    jobs = c.get("/api/clip/jobs").json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == queued["job_id"]
    assert jobs[0]["source"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert jobs == clip_job.list_jobs(limit=15)

    cleaned = c.post("/api/controls/cleanup").json()
    assert cleaned["action"] == "cleanup" and cleaned["state"] == "done"


def test_unconfigured_panel_shows_setup_guidance_and_authenticated_panel_supports_logout_and_tunnel_info(
    tmp_path, monkeypatch
):
    from app.growth import notify
    from app.review import review_app

    # 1. Unconfigured REVIEW_TOKEN -> 503 with clear setup instructions, no misleading login form
    monkeypatch.setattr(settings, "review_token", "")
    unconf = TestClient(review_app).get("/")
    assert unconf.status_code == 503
    assert "REVIEW_TOKEN" in unconf.text
    assert 'name="key"' not in unconf.text

    # 2. Configured REVIEW_TOKEN -> tunnel_url exposed on /api/controls (without leaking key), logout clears cookie
    monkeypatch.setattr(settings, "review_token", "panel-test-key")
    tunnel_file = tmp_path / "tunnel_url.txt"
    tunnel_file.write_text("https://calm-breeze-1234.trycloudflare.com\n", encoding="utf-8")
    monkeypatch.setattr(notify, "TUNNEL_URL_FILE", tunnel_file)

    c = _client()
    ctrl = c.get("/api/controls").json()
    assert ctrl["tunnel_url"] == "https://calm-breeze-1234.trycloudflare.com"
    assert "panel-test-key" not in json.dumps(ctrl)

    out = c.post("/api/logout")
    assert out.status_code == 200 and out.json() == {"ok": True}
    assert c.get("/api/controls").status_code == 401


def test_clip_panel_respects_approval_by_default_supports_channel_slug_and_retry_dismiss(
    tmp_path, monkeypatch
):
    from app.clipper import job as clip_job
    from app.review import review_app

    _write(
        tmp_path,
        monkeypatch,
        {
            "channels": [
                {"slug": "tech", "name": "Tech", "require_approval": True},
                {"slug": "autochan", "name": "Auto", "require_approval": False},
            ]
        },
    )
    with session_scope() as db:
        channels.sync_channels(db)
        tech_id = channels.channel_by_slug(db, "tech").id
        auto_id = channels.channel_by_slug(db, "autochan").id

    workers = []
    monkeypatch.setattr(clip_job, "start_queue_worker", lambda: workers.append(True))
    monkeypatch.setattr(settings, "clip_auto_queue", False)

    unauth = TestClient(review_app)
    assert unauth.post("/api/clip/jobs/abc/retry").status_code == 401
    assert unauth.post("/api/clip/jobs/abc/dismiss").status_code == 401

    c = _client()
    page_html = c.get("/").text
    assert "auto_queue:true" not in page_html, "Clip UI must not hardcode auto_queue:true and bypass review"

    # 1. Default manual clip request -> auto_queue resolves to False (goes to Review, never straight to live upload)
    r1 = c.post("/api/clip", json={"source": "https://youtu.be/vid1", "count": 3}).json()
    with session_scope() as db:
        j1 = db.get(ClipJob, r1["job_id"])
        assert j1.auto_queue is False

    # 2. Named channel with require_approval=True -> sets channel_id and auto_queue=False
    r2 = c.post("/api/clip", json={"source": "https://youtu.be/vid2", "channel_slug": "tech"}).json()
    with session_scope() as db:
        j2 = db.get(ClipJob, r2["job_id"])
        assert j2.channel_id == tech_id and j2.auto_queue is False

    # 3. Named channel with require_approval=False -> auto_queue=True when omitted, or False when explicitly overridden
    r3 = c.post("/api/clip", json={"source": "https://youtu.be/vid3", "channel_slug": "autochan"}).json()
    with session_scope() as db:
        j3 = db.get(ClipJob, r3["job_id"])
        assert j3.channel_id == auto_id and j3.auto_queue is True

    # Unknown channel slug -> clean 400
    bad_ch = c.post("/api/clip", json={"source": "https://youtu.be/vid4", "channel_slug": "missing"})
    assert bad_ch.status_code == 400 and "missing" in bad_ch.json()["detail"]

    # 4. Retry & Dismiss lifecycle
    assert c.post("/api/clip/jobs/does-not-exist/retry").status_code == 404
    assert c.post("/api/clip/jobs/does-not-exist/dismiss").status_code == 404
    # Cannot retry a job that is still queued
    assert c.post(f"/api/clip/jobs/{r1['job_id']}/retry").status_code == 409

    # Mark j1 as failed with 3 attempts + permanent error, then retry via API
    with session_scope() as db:
        j1 = db.get(ClipJob, r1["job_id"])
        j1.status = "failed"
        j1.attempts = 3
        j1.error = "network timeout"
        j1.result = {"feed": "https://youtube.com/@mkbhd", "ok": False, "error": "network timeout", "permanent": True}

    workers.clear()
    retried = c.post(f"/api/clip/jobs/{r1['job_id']}/retry")
    assert retried.status_code == 200
    body = retried.json()
    assert body["status"] == "queued" and body["error"] is None
    assert workers == [True]
    with session_scope() as db:
        j1 = db.get(ClipJob, r1["job_id"])
        assert j1.status == "queued" and j1.attempts == 0 and j1.error is None
        assert j1.result == {"feed": "https://youtube.com/@mkbhd"}

    # Running job cannot be dismissed (409), queued/failed job can be dismissed (200)
    with session_scope() as db:
        db.get(ClipJob, r2["job_id"]).status = "running"
    assert c.post(f"/api/clip/jobs/{r2['job_id']}/dismiss").status_code == 409

    dismissed = c.post(f"/api/clip/jobs/{r3['job_id']}/dismiss")
    assert dismissed.status_code == 200 and dismissed.json() == {"id": r3["job_id"], "dismissed": True}
    with session_scope() as db:
        assert db.get(ClipJob, r3["job_id"]) is None


def test_review_queue_exposes_up_next_shelf_title_editing_and_shelf_controls(tmp_path):
    from app.models import MetadataDraft, VideoStatus
    from app.review import review_app

    clip_pending = tmp_path / "pending.mp4"
    clip_queued = tmp_path / "queued.mp4"
    clip_pending.write_bytes(b"mp4-pending")
    clip_queued.write_bytes(b"mp4-queued")

    with session_scope() as db:
        v_p = Video(
            source="youtube",
            source_id="src1_10_38",
            source_url="https://www.youtube.com/watch?v=abc123",
            title="Pending raw title",
            author_name="Creator One",
            duration_sec=28,
            transformed_path=str(clip_pending),
            status=VideoStatus.PENDING,
            meta={
                "clip_rendered": True,
                "clip_kind": "speech",
                "clip_score": 0.876,
                "clip_start": 10.4,
                "clip_end": 38.2,
                "clip_hook": "Wait for the twist",
                "clip_reason": "High retention payoff",
            },
        )
        v_q = Video(
            source="youtube",
            source_id="src2_45_70",
            source_url="https://www.youtube.com/watch?v=xyz789",
            title="Auto queued clip",
            author_name="Creator Two",
            duration_sec=25,
            transformed_path=str(clip_queued),
            status=VideoStatus.TRANSFORMED,
            meta={
                "clip_rendered": True,
                "clip_kind": "speech",
                "clip_score": 0.91,
                "clip_start": 45.0,
                "clip_end": 70.0,
            },
        )
        db.add_all([v_p, v_q])
        db.flush()
        pid, qid = v_p.id, v_q.id

    unauth = TestClient(review_app)
    assert unauth.post(f"/api/clips/{pid}/title", json={"title": "x"}).status_code == 401
    assert unauth.post(f"/api/clips/{qid}/shelf-reject").status_code == 401

    c = _client()
    q = c.get("/api/queue").json()
    assert [x["id"] for x in q["clips"]] == [pid]
    assert [x["id"] for x in q["queued"]] == [qid]
    card = q["clips"][0]
    assert card["score"] == 0.88 and card["start"] == 10.4 and card["end"] == 38.2
    assert card["why"] == "High retention payoff"

    # Inline title edit works on both PENDING and TRANSFORMED clips without changing status
    assert c.post(f"/api/clips/{qid}/title", json={"title": "   "}).status_code == 400
    saved = c.post(f"/api/clips/{qid}/title", json={"title": "  Sharper   YouTube Hook  "})
    assert saved.status_code == 200
    assert saved.json()["edited"] is True
    assert "Sharper YouTube Hook" in saved.json()["title"]
    with session_scope() as db:
        assert db.get(Video, qid).status == VideoStatus.TRANSFORMED
        draft = db.query(MetadataDraft).filter_by(video_id=qid).one()
        assert draft.edited is True and draft.title.startswith("Sharper YouTube Hook")

    # Auto-queued TRANSFORMED clip (even without owner_approved) can be pulled back to Review via /undo
    pulled = c.post(f"/api/clips/{qid}/undo")
    assert pulled.status_code == 200 and pulled.json()["status"] == "pending"
    assert len(c.get("/api/queue").json()["clips"]) == 2
    assert c.get("/api/queue").json()["queued"] == []

    # Re-approve qid onto shelf, then reject directly from shelf, then undo that rejection
    assert c.post(f"/api/clips/{qid}/approve", json={"title": ""}).status_code == 200
    rejected = c.post(f"/api/clips/{qid}/shelf-reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert c.get("/api/queue").json()["queued"] == []
    undone = c.post(f"/api/clips/{qid}/undo")
    assert undone.status_code == 200 and undone.json()["status"] == "pending"


def test_review_ui_inline_javascript_has_valid_syntax(tmp_path):
    import re
    import shutil
    import subprocess
    from app.review_ui import PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    match = re.search(r"<script>([\s\S]*?)</script>", PAGE)
    assert match is not None
    js_file = tmp_path / "panel.js"
    js_file.write_text(match.group(1), encoding="utf-8")
    proc = subprocess.run([node, "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_channel_settings_edit_all_fields_and_default_channel_shows_env_feeds(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_feeds", '["https://www.youtube.com/@veritasium"]')
    c = _client()
    default_rows = c.get("/api/channels").json()["channels"]
    assert default_rows[0]["default"] is True
    assert default_rows[0]["feeds"] == ["https://www.youtube.com/@veritasium"]

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "name": "Tech"}]})
    with session_scope() as db:
        channels.sync_channels(db)

    bad = c.post("/api/channels/tech/settings", json={"auto_approve_score": 1.5})
    assert bad.status_code == 400 and "auto_approve_score" in bad.json()["detail"]

    ok = c.post(
        "/api/channels/tech/settings",
        json={
            "niche": "tech",
            "require_approval": False,
            "auto_approve_score": 0.85,
            "cadence_backstop": True,
        },
    )
    assert ok.status_code == 200
    rows = c.get("/api/channels").json()["channels"]
    tech = next(x for x in rows if x["slug"] == "tech")
    assert tech["niche"] == "tech"
    assert tech["require_approval"] is False
    assert tech["auto_approve_score"] == 0.85
    assert tech["cadence_backstop"] is True


def test_controls_expose_scheduler_health_history_and_scraper_actions(tmp_path, monkeypatch):
    from app import logging_config

    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)
    logging_config.record_job_run("cleanup", {"mb_reclaimed": 4.2}, duration_sec=0.3, ok=True)

    c = _client()
    ctrl = c.get("/api/controls").json()
    action_ids = {a["action"] for a in ctrl["actions"]}
    assert {"scrape", "transform"} <= action_ids
    sched = ctrl["scheduler"]
    assert isinstance(sched["running"], bool)
    assert isinstance(sched["overdue"], list)
    assert sched["health"]["cleanup"]["ok"] is True
    assert sched["recent"][0]["job_id"] == "cleanup"


def test_classic_scraped_pending_videos_can_be_curated_without_affecting_growth_stock(tmp_path):
    from app.growth import approval
    from app.models import MetadataDraft, VideoStatus
    from app.review import review_app

    raw1 = tmp_path / "scraped1.mp4"
    raw2 = tmp_path / "scraped2.mp4"
    raw1.write_bytes(b"raw-video-bytes-1")
    raw2.write_bytes(b"raw-video-bytes-2")

    with session_scope() as db:
        v1 = Video(
            source="snapchat",
            source_id="snap_1",
            source_url="https://story.snapchat.com/s/1",
            title="Scraped candidate one",
            author="snapcreator",
            duration_sec=19,
            viral_score=0.77,
            local_path=str(raw1),
            status=VideoStatus.PENDING,
            meta={"platform": "snapchat"},
        )
        v2 = Video(
            source="snapchat",
            source_id="snap_2",
            source_url="https://story.snapchat.com/s/2",
            title="Scraped candidate two",
            duration_sec=22,
            viral_score=0.61,
            local_path=str(raw2),
            status=VideoStatus.PENDING,
            meta={"platform": "snapchat"},
        )
        db.add_all([v1, v2])
        db.flush()
        id1, id2 = v1.id, v2.id
        # Growth loop stock check MUST ignore non-clip scraped rows
        assert approval.awaiting_review(db) == []

    unauth = TestClient(review_app)
    assert unauth.post(f"/api/scraped/{id1}/approve", json={"title": "x"}).status_code == 401
    assert unauth.post(f"/api/scraped/{id2}/reject").status_code == 401

    c = _client()
    q = c.get("/api/queue").json()
    assert q["clips"] == []
    assert [x["id"] for x in q["scraped"]] == [id1, id2]

    media_resp = c.get(f"/media/{id1}")
    assert media_resp.status_code == 200 and media_resp.content == b"raw-video-bytes-1"

    ap = c.post(f"/api/scraped/{id1}/approve", json={"title": "  Clean Spotlight Short  "})
    assert ap.status_code == 200 and ap.json()["status"] == "approved"

    rj = c.post(f"/api/scraped/{id2}/reject")
    assert rj.status_code == 200 and rj.json()["status"] == "rejected"

    with session_scope() as db:
        assert db.get(Video, id1).status == VideoStatus.APPROVED
        assert db.get(Video, id2).status == VideoStatus.REJECTED
        draft = db.query(MetadataDraft).filter_by(video_id=id1).one()
        assert draft.edited is True and draft.title.startswith("Clean Spotlight Short")


def test_pwa_manifest_dry_run_preview_and_queue_filter_control_action(monkeypatch):
    from app.models import VideoStatus
    from app.review import review_app

    unauth = TestClient(review_app)
    assert unauth.get("/manifest.webmanifest").status_code == 401
    assert unauth.get("/api/controls/preview").status_code == 401

    with session_scope() as db:
        bad = Video(
            source="snapchat",
            source_id="unsafe_q_1",
            source_url="https://story.snapchat.com/s/u1",
            title="Free casino bonus spins",
            duration_sec=15,
            width=540,
            height=960,
            language="en",
            status=VideoStatus.PENDING,
        )
        good = Video(
            source="snapchat",
            source_id="good_q_1",
            source_url="https://story.snapchat.com/s/g1",
            title="Skateboard trick",
            duration_sec=15,
            width=540,
            height=960,
            language="en",
            status=VideoStatus.PENDING,
        )
        db.add_all([bad, good])
        db.flush()
        bad_id, good_id = bad.id, good.id

    c = _client()
    html = c.get("/").text
    assert 'rel="manifest"' in html and 'id="rv-search"' in html

    manifest = c.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    mdata = manifest.json()
    assert mdata["display"] == "standalone" and mdata["name"] == "Doomscroller"

    preview = c.get("/api/controls/preview").json()
    assert preview["filters"]["dry_run"] is True
    assert preview["filters"]["rejected"] == 1
    assert preview["cleanup"]["dry_run"] is True
    with session_scope() as db:
        assert db.get(Video, bad_id).status == VideoStatus.PENDING

    ran = c.post("/api/controls/filter_queue").json()
    assert ran["state"] == "done"
    with session_scope() as db:
        assert db.get(Video, bad_id).status == VideoStatus.REJECTED
        assert db.get(Video, good_id).status == VideoStatus.PENDING


def test_channels_cli_set_supports_approval_score_and_backstop_flags(tmp_path, monkeypatch):
    import sys
    from scripts import channels as channels_cli

    _write(tmp_path, monkeypatch, {"channels": [{"slug": "tech", "name": "Tech"}]})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "channels.py",
            "set",
            "tech",
            "--no-require-approval",
            "--auto-approve-score",
            "0.9",
            "--cadence-backstop",
        ],
    )
    rc = channels_cli.main()
    assert rc == 0
    saved = json.loads((tmp_path / "channels.json").read_text(encoding="utf-8"))["channels"][0]
    assert saved["require_approval"] is False
    assert saved["auto_approve_score"] == 0.9
    assert saved["cadence_backstop"] is True


def test_clip_file_upload_streams_into_inbox_enforces_auth_extension_and_size_cap(tmp_path, monkeypatch):
    from pathlib import Path
    from app.clipper import job as clip_job
    from app.review import review_app

    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    ran_worker = []
    monkeypatch.setattr(clip_job, "start_queue_worker", lambda: ran_worker.append(True))

    # 1. Unauthenticated upload is rejected with 401
    unauth = TestClient(review_app)
    assert unauth.post("/api/clip/upload?filename=clip.mp4", content=b"1234").status_code == 401

    c = _client()
    # 2. Disallowed extension / path traversal attempt -> 400
    assert c.post("/api/clip/upload?filename=../../evil.exe", content=b"1234").status_code == 400
    # 3. Empty payload -> 400
    assert c.post("/api/clip/upload?filename=empty.mp4", content=b"").status_code == 400

    # 4. Size cap enforced and partial file cleaned up
    monkeypatch.setattr(clip_job, "MAX_UPLOAD_BYTES", 32)
    too_big = c.post("/api/clip/upload?filename=huge.mp4", content=b"x" * 64)
    assert too_big.status_code == 413
    inbox = tmp_path / "content" / "inbox"
    assert list(inbox.glob("*")) == []

    # 5. Valid upload lands inside content/inbox/, queues ClipJob for review by default, and starts worker
    monkeypatch.setattr(clip_job, "MAX_UPLOAD_BYTES", 1024 * 1024)
    ok = c.post(
        "/api/clip/upload?filename=My%20Phone%20Video!.mp4&count=2",
        content=b"fake-mp4-binary-payload",
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "queued" and body["bytes"] == len(b"fake-mp4-binary-payload")
    assert ran_worker == [True]

    saved_files = list(inbox.glob("*.mp4"))
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == b"fake-mp4-binary-payload"

    jobs = c.get("/api/clip/jobs").json()["jobs"]
    assert jobs[0]["id"] == body["job_id"]
    assert jobs[0]["auto_queue"] is False
    assert Path(jobs[0]["source"]).resolve() == saved_files[0].resolve()


def test_metadata_preview_and_editor_preserve_creator_credit_on_review_and_shelf_cards(tmp_path):
    from app.models import MetadataDraft, VideoStatus
    from app.review import review_app
    from tasks.pipeline import generate_metadata_for_video

    clip_pending = tmp_path / "meta_pending.mp4"
    clip_queued = tmp_path / "meta_queued.mp4"
    clip_approve = tmp_path / "meta_approve.mp4"
    for p in (clip_pending, clip_queued, clip_approve):
        p.write_bytes(b"mp4-bytes")

    with session_scope() as db:
        v_p = Video(
            source="youtube",
            source_id="meta_src_1",
            source_url="https://www.youtube.com/watch?v=credit111",
            title="Raw clip about GPUs",
            description="Deep dive into tensor cores.",
            author="mkbhd",
            author_name="Marques Brownlee",
            keywords=["gpu", "nvidia"],
            duration_sec=26,
            transformed_path=str(clip_pending),
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_score": 0.84},
        )
        v_q = Video(
            source="youtube",
            source_id="meta_src_2",
            source_url="https://www.youtube.com/watch?v=credit222",
            title="Queued shelf clip",
            author_name="Creator Name Only",
            duration_sec=21,
            transformed_path=str(clip_queued),
            status=VideoStatus.TRANSFORMED,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_score": 0.9},
        )
        v_a = Video(
            source="youtube",
            source_id="meta_src_3",
            source_url="https://www.youtube.com/watch?v=credit333",
            title="Approve with inline metadata",
            author="veritasium",
            author_name="Veritasium",
            duration_sec=30,
            transformed_path=str(clip_approve),
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_score": 0.88},
        )
        db.add_all([v_p, v_q, v_a])
        db.flush()
        pid, qid, aid = v_p.id, v_q.id, v_a.id

    # Auth guard on /metadata
    unauth = TestClient(review_app)
    assert unauth.post(f"/api/clips/{pid}/metadata", json={"description": "x"}).status_code == 401

    c = _client()
    html = c.get("/").text
    assert "Preview description & credit" in html
    assert "/metadata" in html

    # 1. Queue cards expose composed description (with #Shorts + creator credit + source link) and tags
    q = c.get("/api/queue").json()
    p_card = next(x for x in q["clips"] if x["id"] == pid)
    q_card = next(x for x in q["queued"] if x["id"] == qid)
    assert "#Shorts" in p_card["description"]
    assert "Credit: Marques Brownlee (@mkbhd)" in p_card["description"]
    assert "https://www.youtube.com/watch?v=credit111" in p_card["description"]
    assert "gpu" in p_card["tags"] and "mkbhd" in p_card["tags"]

    # Author-name-only clip still gets an explicit Credit line in its composed preview
    assert "Credit: Creator Name Only" in q_card["description"]
    assert "https://www.youtube.com/watch?v=credit222" in q_card["description"]

    # 2. Regression fix: editing ONLY the title must NOT blank out description/credit/tags on MetadataDraft
    t_res = c.post(f"/api/clips/{pid}/title", json={"title": "Why GPUs Changed Everything"})
    assert t_res.status_code == 200
    generate_metadata_for_video(pid)  # autopilot calls this before publish; must keep credit & tags
    with session_scope() as db:
        draft_p = db.query(MetadataDraft).filter_by(video_id=pid).one()
        assert draft_p.edited is True
        assert "Credit: Marques Brownlee (@mkbhd)" in draft_p.description
        assert "gpu" in draft_p.tags

    # 3. Full metadata edit on Up Next shelf clip (title + description + tags, angle brackets stripped)
    assert c.post(f"/api/clips/{qid}/metadata", json={}).status_code == 400
    assert c.post(f"/api/clips/{qid}/metadata", json={"title": "   "}).status_code == 400
    assert c.post(f"/api/clips/{qid}/metadata", json={"description": "   "}).status_code == 400

    edited = c.post(
        f"/api/clips/{qid}/metadata",
        json={
            "title": "Shelf Hook <script>",
            "description": "Custom intro <tag>\n\n#Shorts #ai\n\nCredit: Creator Name Only\nhttps://www.youtube.com/watch?v=credit222",
            "tags": ["AI Tools", "Tech!", "a", "ai tools"],
        },
    )
    assert edited.status_code == 200
    ebody = edited.json()
    assert ebody["edited"] is True
    assert "<" not in ebody["title"] and ">" not in ebody["title"]
    assert "<" not in ebody["description"] and "Credit: Creator Name Only" in ebody["description"]
    assert ebody["tags"] == ["ai tools", "tech"]

    # Queue immediately reflects saved description & tags without changing TRANSFORMED status
    q_after = c.get("/api/queue").json()
    q_card_after = next(x for x in q_after["queued"] if x["id"] == qid)
    assert q_card_after["edited"] is True
    assert q_card_after["description"].startswith("Custom intro tag")
    assert q_card_after["tags"] == ["ai tools", "tech"]

    # 4. Approving with inline edited description & tags persists all fields onto MetadataDraft
    ap = c.post(
        f"/api/clips/{aid}/approve",
        json={
            "title": "Physics Mystery",
            "description": "Watch closely.\n\n#Shorts\n\nCredit: Veritasium (@veritasium)",
            "tags": ["physics", "science"],
        },
    )
    assert ap.status_code == 200 and ap.json()["status"] == "transformed"
    with session_scope() as db:
        draft_a = db.query(MetadataDraft).filter_by(video_id=aid).one()
        assert draft_a.edited is True
        assert "Credit: Veritasium (@veritasium)" in draft_a.description
        assert draft_a.tags == ["physics", "science"]

        # Published clip refuses further metadata edits with 409
        v_q_row = db.get(Video, qid)
        v_q_row.status = VideoStatus.PUBLISHED
        v_q_row.youtube_video_id = "yt_live_999"
    assert c.post(f"/api/clips/{qid}/metadata", json={"description": "too late"}).status_code == 409


def test_batch_reject_flagged_cleans_up_flagged_pending_clips_with_channel_scoping(tmp_path, monkeypatch):
    from app.models import VideoStatus
    from app.review import review_app
    from app.review_ui import PAGE

    # UI regression check: default-channel clips have channel_slug == "" and must match the 'default' filter chip
    assert "(c.channel_slug||'default')===filter" in PAGE

    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"mp4-data")

    # 0. Before channels table is seeded, scoping to 'default' seeds the default channel and rejects channel_id=None flagged clips
    with session_scope() as db:
        legacy_flagged = Video(
            source="youtube",
            source_id="lf0",
            source_url="https://youtu.be/lf0",
            title="Legacy default flagged",
            transformed_path=str(mp4),
            channel_id=None,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "growth_flagged": "outro screen"},
        )
        db.add(legacy_flagged)
        db.flush()
        lf0_id = legacy_flagged.id

    c = _client()
    r_default = c.post("/api/clips/reject-flagged", json={"channel_slug": "default"})
    assert r_default.status_code == 200
    assert r_default.json() == {"rejected": 1, "ids": [lf0_id]}

    _write(
        tmp_path,
        monkeypatch,
        {"channels": [{"slug": "tech", "name": "Tech"}, {"slug": "comedy", "name": "Comedy"}]},
    )
    with session_scope() as db:
        channels.sync_channels(db)
        tech_id = channels.channel_by_slug(db, "tech").id
        comedy_id = channels.channel_by_slug(db, "comedy").id

    with session_scope() as db:
        f_tech_1 = Video(
            source="youtube",
            source_id="ft1",
            source_url="https://youtu.be/ft1",
            title="Flagged tech 1",
            transformed_path=str(mp4),
            channel_id=tech_id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "growth_flagged": "end credits"},
        )
        f_tech_2 = Video(
            source="youtube",
            source_id="ft2",
            source_url="https://youtu.be/ft2",
            title="Flagged tech 2",
            transformed_path=str(mp4),
            channel_id=tech_id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "growth_flagged": "sponsor read"},
        )
        clean_tech = Video(
            source="youtube",
            source_id="ct1",
            source_url="https://youtu.be/ct1",
            title="Clean tech clip",
            transformed_path=str(mp4),
            channel_id=tech_id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_score": 0.9},
        )
        f_comedy = Video(
            source="youtube",
            source_id="fc1",
            source_url="https://youtu.be/fc1",
            title="Flagged comedy clip",
            transformed_path=str(mp4),
            channel_id=comedy_id,
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "growth_flagged": "low audio"},
        )
        queued_flagged = Video(
            source="youtube",
            source_id="qf1",
            source_url="https://youtu.be/qf1",
            title="Already approved clip",
            transformed_path=str(mp4),
            channel_id=tech_id,
            status=VideoStatus.TRANSFORMED,
            meta={"clip_rendered": True, "growth_flagged": "old flag", "owner_approved": "now"},
        )
        scraped_pending = Video(
            source="snapchat",
            source_id="sp1",
            source_url="https://story.snapchat.com/s/sp1",
            title="Scraped pending",
            local_path=str(mp4),
            status=VideoStatus.PENDING,
            meta={"platform": "snapchat", "growth_flagged": "not a clip"},
        )
        db.add_all([f_tech_1, f_tech_2, clean_tech, f_comedy, queued_flagged, scraped_pending])
        db.flush()
        ft1_id, ft2_id, ct_id, fc_id, qf_id, sp_id = (
            f_tech_1.id,
            f_tech_2.id,
            clean_tech.id,
            f_comedy.id,
            queued_flagged.id,
            scraped_pending.id,
        )

    unauth = TestClient(review_app)
    assert unauth.post("/api/clips/reject-flagged").status_code == 401

    c = _client()
    assert "/api/clips/reject-flagged" in c.get("/").text
    assert c.post("/api/clips/reject-flagged", json={"channel_slug": "missing"}).status_code == 400

    # 1. Scoped to 'tech' -> rejects only tech's 2 flagged pending clips
    r_tech = c.post("/api/clips/reject-flagged", json={"channel_slug": "tech"})
    assert r_tech.status_code == 200
    body_tech = r_tech.json()
    assert body_tech["rejected"] == 2
    assert set(body_tech["ids"]) == {ft1_id, ft2_id}

    with session_scope() as db:
        assert db.get(Video, ft1_id).status == VideoStatus.REJECTED
        assert db.get(Video, ft2_id).status == VideoStatus.REJECTED
        assert db.get(Video, ct_id).status == VideoStatus.PENDING
        assert db.get(Video, fc_id).status == VideoStatus.PENDING
        assert db.get(Video, qf_id).status == VideoStatus.TRANSFORMED
        assert db.get(Video, sp_id).status == VideoStatus.PENDING

    # 2. Unscoped (no body) -> rejects remaining flagged pending clip (comedy), leaving clean/queued/scraped intact
    r_all = c.post("/api/clips/reject-flagged")
    assert r_all.status_code == 200
    assert r_all.json() == {"rejected": 1, "ids": [fc_id]}

    # 3. Idempotent when no flagged clips remain, and undo still works on batch-rejected clips
    assert c.post("/api/clips/reject-flagged").json() == {"rejected": 0, "ids": []}
    assert c.post(f"/api/clips/{fc_id}/undo").status_code == 200
    with session_scope() as db:
        assert db.get(Video, fc_id).status == VideoStatus.PENDING


def test_report_tab_exposes_copy_summary_button_and_formats_channel_and_fleet_summaries(tmp_path):
    import re
    import shutil
    import subprocess
    from app.review_ui import PAGE

    assert "Copy summary" in PAGE
    assert "formatReportSummary" in PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    match = re.search(r"<script>([\s\S]*?)</script>", PAGE)
    assert match is not None

    fn_match = re.search(r"(function formatReportSummary\([\s\S]*?\n\})", match.group(1))
    assert fn_match is not None, "formatReportSummary helper should be defined in inline script"

    harness = (
        fn_match.group(1)
        + """
const fleet = {
  day: 14, phase: 'per channel', published: 18, matured: 12, stock: 5,
  warnings: ['Tech: Low stock'],
  problems: [{channel: 'Tech', summary: 'Missed window 08-12', at: '2026-11-13T10:00:00Z'}],
  channels: {
    tech: {
      name: 'Tech', day: 14, phase: 'testing', published: 12, matured: 8, stock: 3,
      plan: {posts: [{slot: '08-12', state: 'posted'}, {slot: '16-20', state: 'waiting'}]},
      factors: {pace: [{arm: 'tight', multiplier: 1.5, p_best: 0.82, n: 6}]},
      best: [{title: 'GPU Secret #Shorts', views: 24500, diagnosis: 'Well above normal.'}]
    },
    comedy: {
      name: 'Comedy', day: 5, phase: 'testing', published: 6, matured: 4, stock: 2,
      best: [{title: 'Mic Drop #Shorts', views: 19000, diagnosis: 'Strong hold.'}]
    }
  }
};
console.log(JSON.stringify({
  all: formatReportSummary(fleet, ''),
  tech: formatReportSummary(fleet, 'tech')
}));
"""
    )
    js_file = tmp_path / "report_summary_test.js"
    js_file.write_text(harness, encoding="utf-8")
    proc = subprocess.run([node, str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    assert "Published: 18" in out["all"] and "In stock: 5" in out["all"]
    assert "GPU Secret #Shorts" in out["all"] and "Mic Drop #Shorts" in out["all"]
    assert "Tech: Low stock" in out["all"]

    assert "Tech" in out["tech"] and "Published: 12" in out["tech"]
    assert "08-12 posted" in out["tech"]
    assert "GPU Secret #Shorts" in out["tech"]
    assert "tight" in out["tech"]


def test_playback_speed_toggle_resolution_cards_and_backup_export(tmp_path, monkeypatch):
    from app.models import VideoStatus
    from app.review import review_app
    from app.review_ui import PAGE

    # 1. Speed toggle UI & keyboard shortcut S present in PAGE
    assert 'id="rv-speed"' in PAGE
    assert "reviewSpeed" in PAGE
    assert "1.5" in PAGE and "playbackRate" in PAGE

    # 2. Resolution exposed on /api/queue cards
    mp4 = tmp_path / "res.mp4"
    mp4.write_bytes(b"mp4-bytes")
    with session_scope() as db:
        v = Video(
            source="youtube",
            source_id="res_1",
            source_url="https://youtu.be/res1",
            title="Crisp vertical clip",
            width=1080,
            height=1920,
            duration_sec=24,
            transformed_path=str(mp4),
            status=VideoStatus.PENDING,
            meta={"clip_rendered": True, "clip_score": 0.92},
        )
        db.add(v)
        db.flush()
        vid = v.id

    unauth = TestClient(review_app)
    assert unauth.get("/api/backup").status_code == 401

    c = _client()
    q = c.get("/api/queue").json()
    card = next(x for x in q["clips"] if x["id"] == vid)
    assert card["width"] == 1080 and card["height"] == 1920
    assert card["resolution"] == "1080x1920"

    # 3. Backup export endpoint returns channels + queue attachment without leaking secrets
    _write(
        tmp_path,
        monkeypatch,
        {
            "channels": [
                {
                    "slug": "tech",
                    "name": "Tech",
                    "client_secrets": "C:/secret/client_secret_xyz.json",
                    "token_path": "C:/secret/token_xyz.json",
                }
            ]
        },
    )
    with session_scope() as db:
        channels.sync_channels(db)

    backup = c.get("/api/backup")
    assert backup.status_code == 200
    assert "attachment" in backup.headers.get("content-disposition", "")
    assert "no-store" in backup.headers.get("cache-control", "")
    raw_backup = backup.text
    assert "panel-test-key" not in raw_backup
    assert "client_secret_xyz" not in raw_backup
    assert "token_xyz" not in raw_backup
    payload = backup.json()
    assert "exported_at" in payload
    assert any(ch["slug"] == "tech" for ch in payload["channels"])
    assert any(cl["id"] == vid for cl in payload["queue"]["clips"])


def test_notify_test_endpoint_and_send_test_alert_control_ui(monkeypatch):
    from app.growth import notify
    from app.review import review_app
    from app.review_ui import PAGE

    assert "Send test alert" in PAGE
    assert "/api/notify/test" in PAGE
    assert 'aria-live="polite"' in PAGE
    assert "<kbd>S</kbd>" in PAGE and "<kbd>/</kbd>" in PAGE and "<kbd>Esc</kbd>" in PAGE

    unauth = TestClient(review_app)
    assert unauth.post("/api/notify/test").status_code == 401

    c = _client()
    monkeypatch.setattr(settings, "ntfy_topic", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    unconf = c.post("/api/notify/test")
    assert unconf.status_code == 200
    assert unconf.json()["configured"] is False
    assert unconf.json()["ok"] is False

    monkeypatch.setattr(settings, "ntfy_topic", "my-topic")
    monkeypatch.setattr(settings, "telegram_bot_token", "999999:SUPER_SECRET_TG_TOKEN")
    monkeypatch.setattr(settings, "telegram_chat_id", "12345")
    monkeypatch.setattr(notify, "_deliver_ntfy", lambda text, *, title, link: (True, ""))
    monkeypatch.setattr(notify, "_deliver_telegram", lambda text, *, link: (False, "ConnectError"))

    sent = c.post("/api/notify/test")
    assert sent.status_code == 200
    assert "SUPER_SECRET_TG_TOKEN" not in sent.text
    assert "panel-test-key" not in sent.text
    data = sent.json()
    assert data["configured"] is True and data["ok"] is True
    assert data["channels"]["ntfy"] == {"ok": True}
    assert data["channels"]["telegram"]["ok"] is False


def test_clear_finished_clip_jobs_keeps_queued_and_running_and_ui_drag_drop():
    from app.review import review_app
    from app.review_ui import PAGE

    assert "Clear finished" in PAGE
    assert "/api/clip/clear-finished" in PAGE
    assert "dragover" in PAGE and "drop" in PAGE

    unauth = TestClient(review_app)
    assert unauth.post("/api/clip/clear-finished").status_code == 401

    with session_scope() as db:
        j_queued = ClipJob(source="https://youtu.be/q1", status="queued")
        j_running = ClipJob(source="https://youtu.be/r1", status="running")
        j_done = ClipJob(source="https://youtu.be/d1", status="done")
        j_failed = ClipJob(source="https://youtu.be/f1", status="failed", error="boom")
        db.add_all([j_queued, j_running, j_done, j_failed])
        db.flush()
        keep_ids = {j_queued.id, j_running.id}
        clear_ids = {j_done.id, j_failed.id}

    c = _client()
    res = c.post("/api/clip/clear-finished")
    assert res.status_code == 200
    body = res.json()
    assert body["cleared"] == 2
    assert set(body["ids"]) == clear_ids

    remaining = {j["id"] for j in c.get("/api/clip/jobs").json()["jobs"]}
    assert remaining == keep_ids


def test_tunnel_ansi_url_extraction_atomic_write_and_run_recycled_pid_guard(tmp_path, monkeypatch):
    import os
    from app.growth import notify
    from scripts import run, tunnel

    # 1. ANSI escape codes & trailing punctuation stripped cleanly from tunnel URLs
    ansi_cf = "\x1b[36mINF\x1b[0m |  \x1b[1;32mhttps://vivid-panda-42.trycloudflare.com\x1b[0m.  |"
    assert tunnel.extract_tunnel_url(ansi_cf) == "https://vivid-panda-42.trycloudflare.com"
    assert tunnel.extract_tunnel_url("Tunnel ready at (https://abc123def.lhr.life), enjoy!") == "https://abc123def.lhr.life"
    assert tunnel.extract_tunnel_url("https://api.trycloudflare.com/ tunnel") is None

    # 2. Atomic tunnel_url.txt write leaves no .tmp file behind
    url_file = tmp_path / "logs" / "tunnel_url.txt"
    monkeypatch.setattr(notify, "TUNNEL_URL_FILE", url_file)
    monkeypatch.setattr(notify, "configured", lambda: False)
    sup = tunnel.TunnelSupervisor(binary=None, ssh_bin=None)
    sup._announce("https://vivid-panda-42.trycloudflare.com")
    assert url_file.read_text(encoding="utf-8").strip() == "https://vivid-panda-42.trycloudflare.com"
    assert list(url_file.parent.glob("*.tmp")) == []

    # 3. Recycled Windows PID guard in scripts/run.py
    assert run._pid_alive(os.getpid()) is False

    class _FakeCompleted:
        def __init__(self, stdout: str, returncode: int = 0):
            self.stdout = stdout
            self.returncode = returncode

    monkeypatch.setattr(run, "IS_WINDOWS", True)

    # Unrelated python process reusing old PID must NOT be considered alive or killed
    def fake_run_unrelated(cmd, **kwargs):
        joined = " ".join(str(x) for x in cmd)
        if "tasklist" in joined.lower():
            return _FakeCompleted('"python.exe","54321","Console","1","25,000 K"\n')
        return _FakeCompleted("C:\\Python312\\python.exe C:\\other_app\\worker.py\n")

    monkeypatch.setattr(run.subprocess, "run", fake_run_unrelated)
    assert run._pid_alive(54321) is False

    # Genuine serve_slim daemon PID IS recognized as alive
    def fake_run_doomscroller(cmd, **kwargs):
        joined = " ".join(str(x) for x in cmd)
        if "tasklist" in joined.lower():
            return _FakeCompleted('"python.exe","54322","Console","1","52,000 K"\n')
        return _FakeCompleted("D:\\doomscroller\\venv\\Scripts\\python.exe scripts/serve_slim.py\n")

    monkeypatch.setattr(run.subprocess, "run", fake_run_doomscroller)
    assert run._pid_alive(54322) is True


def test_daemon_logs_endpoint_auth_scrubbing_level_filter_and_ui(tmp_path, monkeypatch):
    from app import logging_config
    from app.review import review_app
    from app.review_ui import PAGE

    assert "Daemon logs" in PAGE
    assert "/api/logs" in PAGE
    assert "Warnings & Errors" in PAGE

    unauth = TestClient(review_app)
    assert unauth.get("/api/logs").status_code == 401

    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)
    log_file = tmp_path / logging_config.LOG_FILE
    log_file.write_text(
        "\n".join([
            "2026-11-13 18:00:01 [INFO] app.main: cycle finished with 0 errors (?key=panel-test-key)",
            "2026-11-13 18:00:02 [WARNING] app.growth: feed slow for bot999999:SECRET_TG_BOT_TOKEN",
            "2026-11-13 18:00:03 [ERROR] app.clipper: ffmpeg exited 1 REVIEW_TOKEN=panel-test-key",
            "2026-11-13 18:00:04 [CRITICAL] app.db: disk almost full",
        ]) + "\n",
        encoding="utf-8",
    )

    c = _client()
    all_resp = c.get("/api/logs?limit=50&level=all")
    assert all_resp.status_code == 200
    assert "no-store" in (all_resp.headers.get("cache-control") or "").lower()
    assert "panel-test-key" not in all_resp.text
    assert "SECRET_TG_BOT_TOKEN" not in all_resp.text
    all_data = all_resp.json()
    assert all_data["level"] == "all"
    assert all_data["count"] == 4
    assert len(all_data["lines"]) == 4

    warn_resp = c.get("/api/logs?limit=50&level=warn")
    assert warn_resp.status_code == 200
    warn_data = warn_resp.json()
    assert warn_data["level"] == "warn"
    assert warn_data["count"] == 3
    assert all("[INFO]" not in ln for ln in warn_data["lines"])
    assert any("[WARNING]" in ln for ln in warn_data["lines"])
    assert any("[ERROR]" in ln for ln in warn_data["lines"])
    assert any("[CRITICAL]" in ln for ln in warn_data["lines"])

    tail2 = c.get("/api/logs?limit=2&level=all").json()
    assert len(tail2["lines"]) == 2
    assert "[CRITICAL]" in tail2["lines"][-1]


def test_media_save_mp4_download_attachment_vs_inline_and_posted_cards(tmp_path):
    from datetime import datetime, timezone
    from app.models import VideoStatus
    from app.review import review_app
    from app.review_ui import PAGE

    assert "Save MP4" in PAGE
    assert "?download=1" in PAGE
    assert "/api/media/" in PAGE

    mp4_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    f_review = tmp_path / "review.mp4"
    f_review.write_bytes(mp4_bytes)
    f_posted = tmp_path / "posted.mp4"
    f_posted.write_bytes(mp4_bytes)

    with session_scope() as db:
        v_review = Video(
            source="youtube",
            source_id="dl_rev_1",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            title="Review Short",
            status=VideoStatus.PENDING,
            transformed_path=str(f_review),
            duration_sec=22,
            meta={"clip_rendered": True, "clip_score": 0.85},
        )
        v_posted_local = Video(
            source="youtube",
            source_id="dl_post_1",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            title="Posted With Local File",
            status=VideoStatus.PUBLISHED,
            youtube_video_id="yt_real_local_1",
            published_at=datetime.now(timezone.utc),
            transformed_path=str(f_posted),
            views=1200,
            likes=90,
            meta={"clip_rendered": True},
        )
        v_posted_gone = Video(
            source="youtube",
            source_id="dl_post_2",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            title="Posted Cleaned Up",
            status=VideoStatus.PUBLISHED,
            youtube_video_id="yt_real_gone_2",
            published_at=datetime.now(timezone.utc),
            transformed_path=str(tmp_path / "missing.mp4"),
            views=800,
            likes=40,
            meta={"clip_rendered": True},
        )
        db.add_all([v_review, v_posted_local, v_posted_gone])
        db.flush()
        rev_id = v_review.id
        post_local_id = v_posted_local.id
        post_gone_id = v_posted_gone.id

    unauth = TestClient(review_app)
    assert unauth.get(f"/api/media/{rev_id}?download=1").status_code == 401
    assert unauth.get(f"/media/{rev_id}?download=1").status_code == 401

    c = _client()
    inline_resp = c.get(f"/api/media/{rev_id}")
    assert inline_resp.status_code == 200
    assert inline_resp.content == mp4_bytes
    assert "attachment" not in (inline_resp.headers.get("content-disposition") or "").lower()

    dl_resp = c.get(f"/api/media/{rev_id}?download=1")
    assert dl_resp.status_code == 200
    assert dl_resp.content == mp4_bytes
    cd = dl_resp.headers.get("content-disposition") or ""
    assert "attachment" in cd.lower()
    assert f"short-{rev_id[:64]}" in cd and ".mp4" in cd

    dl_legacy = c.get(f"/media/{rev_id}?download=true")
    assert dl_legacy.status_code == 200
    assert "attachment" in (dl_legacy.headers.get("content-disposition") or "").lower()

    q = c.get("/api/queue").json()
    by_yt = {p["url"]: p for p in q["posted"]}
    p_local = by_yt["https://youtube.com/shorts/yt_real_local_1"]
    p_gone = by_yt["https://youtube.com/shorts/yt_real_gone_2"]
    assert p_local["id"] == post_local_id and p_local["has_media"] is True
    assert p_gone["id"] == post_gone_id and p_gone["has_media"] is False


def test_batch_approve_clean_channel_scoping_min_score_and_legacy_default(tmp_path, monkeypatch):
    from app.growth import approval
    from app.models import VideoStatus
    from app.review import review_app
    from app.review_ui import PAGE

    assert "/api/clips/approve-clean" in PAGE
    assert "unflagged" in PAGE.lower()

    unauth = TestClient(review_app)
    assert unauth.post("/api/clips/approve-clean").status_code == 401

    _write(
        tmp_path,
        monkeypatch,
        {
            "channels": [
                {"slug": "default"},
                {"slug": "tech", "name": "Tech", "feeds": ["https://www.youtube.com/@mkbhd"]},
            ]
        },
    )
    f_ok = tmp_path / "clip.mp4"
    f_ok.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
        assert tech is not None
        c_def_high = Video(
            source="youtube",
            source_id="ac_def_hi",
            source_url="https://youtu.be/ac_def_hi",
            title="Default High Clean",
            status=VideoStatus.PENDING,
            channel_id=None,
            transformed_path=str(f_ok),
            meta={"clip_rendered": True, "clip_score": 0.84},
        )
        c_def_low = Video(
            source="youtube",
            source_id="ac_def_lo",
            source_url="https://youtu.be/ac_def_lo",
            title="Default Low Clean",
            status=VideoStatus.PENDING,
            channel_id=None,
            transformed_path=str(f_ok),
            meta={"clip_rendered": True, "clip_score": 0.42},
        )
        c_def_flag = Video(
            source="youtube",
            source_id="ac_def_fl",
            source_url="https://youtu.be/ac_def_fl",
            title="Default Flagged",
            status=VideoStatus.PENDING,
            channel_id=None,
            transformed_path=str(f_ok),
            meta={"clip_rendered": True, "clip_score": 0.95, "growth_flagged": "safety: gambling"},
        )
        c_scraped = Video(
            source="snapchat",
            source_id="ac_scraped",
            source_url="https://www.snapchat.com/spotlight/ac_scraped",
            title="Classic Scraped Candidate",
            status=VideoStatus.PENDING,
            channel_id=None,
            local_path=str(f_ok),
            meta={"platform": "snapchat"},
        )
        c_tech_clean = Video(
            source="youtube",
            source_id="ac_tech_ok",
            source_url="https://youtu.be/ac_tech_ok",
            title="Tech Clean",
            status=VideoStatus.PENDING,
            channel_id=tech.id,
            transformed_path=str(f_ok),
            meta={"clip_rendered": True, "clip_score": 0.91},
        )
        db.add_all([c_def_high, c_def_low, c_def_flag, c_scraped, c_tech_clean])
        db.flush()
        id_def_high = c_def_high.id
        id_def_low = c_def_low.id
        id_def_flag = c_def_flag.id
        id_scraped = c_scraped.id
        id_tech_clean = c_tech_clean.id

    c = _client()
    # 1. Scoped to "default" (including legacy channel_id=None) with min_score=0.7
    r1 = c.post("/api/clips/approve-clean", json={"channel_slug": "default", "min_score": 0.7})
    assert r1.status_code == 200
    assert r1.json() == {"approved": 1, "ids": [id_def_high]}

    # 2. Direct Python API `approval.approve_clean(db, channel_slug="default")` approves remaining clean default clip
    with session_scope() as db:
        r2 = approval.approve_clean(db, channel_slug="default")
        assert r2 == {"approved": 1, "ids": [id_def_low]}

    # 3. Unscoped approves remaining clean tech clip while leaving flagged & classic scraped untouched
    r3 = c.post("/api/clips/approve-clean", json={})
    assert r3.status_code == 200
    assert r3.json() == {"approved": 1, "ids": [id_tech_clean]}

    with session_scope() as db:
        assert db.get(Video, id_def_high).status == VideoStatus.TRANSFORMED
        assert db.get(Video, id_def_low).status == VideoStatus.TRANSFORMED
        assert db.get(Video, id_tech_clean).status == VideoStatus.TRANSFORMED
        assert db.get(Video, id_def_flag).status == VideoStatus.PENDING
        assert db.get(Video, id_scraped).status == VideoStatus.PENDING

    assert c.post("/api/clips/approve-clean", json={"channel_slug": "nope"}).status_code == 400
    assert c.post("/api/clips/approve-clean", json={"min_score": 1.5}).status_code == 400



def _shelf_clip(tmp_path, n, *, status=None, transformed_at=None, **meta):
    from app.models import VideoStatus

    path = tmp_path / f"shelf{n}.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    with session_scope() as db:
        video = Video(
            source="youtube",
            source_id=f"pin_{n}",
            source_url=f"https://youtu.be/pin_{n}",
            title=f"Shelf clip {n}",
            status=status or VideoStatus.TRANSFORMED,
            transformed_path=str(path),
            transformed_at=transformed_at,
            meta={"clip_rendered": True, "clip_parent": f"par{n}", "clip_score": 0.5, **meta},
        )
        db.add(video)
        db.flush()
        return video.id


def test_publish_next_pin_orders_shelf_and_wins_pick_clip_until_unpinned(tmp_path):
    import random
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from app.growth import approval, autopilot
    from app.models import VideoStatus
    from app.review import review_app
    from app.review_ui import PAGE

    assert "Publish next" in PAGE and "Unpin" in PAGE
    assert "/pin" in PAGE

    assert TestClient(review_app).post("/api/clips/x/pin", json={"pinned": True}).status_code == 401

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = _shelf_clip(tmp_path, 1, transformed_at=base)
    b = _shelf_clip(tmp_path, 2, transformed_at=base + timedelta(hours=1))
    c_id = _shelf_clip(tmp_path, 3, transformed_at=base + timedelta(hours=2))
    pending = _shelf_clip(tmp_path, 4, status=VideoStatus.PENDING)
    failing = _shelf_clip(tmp_path, 5, transformed_at=base + timedelta(hours=3), publish_failures=2)

    def shelf_order():
        with session_scope() as db:
            return [v.id for v in autopilot.ready_clips(db)]

    assert shelf_order() == [a, b, c_id, failing]

    client = _client()
    first = client.post(f"/api/clips/{b}/pin", json={"pinned": True})
    assert first.status_code == 200
    assert first.json()["pinned"] is True
    time.sleep(0.01)
    assert client.post(f"/api/clips/{c_id}/pin", json={}).json()["pinned"] is True  # default is pin

    # most recently pinned first, then the oldest unpinned
    assert shelf_order() == [c_id, b, a, failing]
    cards = {card["id"]: card for card in client.get("/api/queue").json()["queued"]}
    assert cards[c_id]["pinned"] is True and cards[b]["pinned"] is True and cards[a]["pinned"] is False
    assert [card["id"] for card in client.get("/api/queue").json()["queued"]][:2] == [c_id, b]

    class _Draw:
        def score(self, arms, factors):
            return 0.0

    current = SimpleNamespace(rate=0.9, focus=frozenset(), rng=random.Random(7), draw=_Draw(), tried={})
    with session_scope() as db:
        clips = autopilot.ready_clips(db)
        assert all(autopilot.pick_clip(clips, current, None).id == c_id for _ in range(25))
        # a pinned clip never jumps ahead of the failure ordering
        assert autopilot.pick_clip([v for v in clips if v.id in (failing, a)], current, None).id == a

    with session_scope() as db:
        only_failing_pinned = [db.get(Video, failing)]
        db.get(Video, failing).meta = {**db.get(Video, failing).meta, "pinned_at": datetime.now(timezone.utc).isoformat()}
        assert autopilot.pick_clip(only_failing_pinned + [db.get(Video, a)], current, None).id == a

    assert client.post(f"/api/clips/{c_id}/pin", json={"pinned": False}).json()["pinned"] is False
    with session_scope() as db:
        assert "pinned_at" not in (db.get(Video, c_id).meta or {})
        clips = autopilot.ready_clips(db)
        assert autopilot.pick_clip(clips, current, None).id == b

    # only unposted clips on the shelf can be pinned
    assert client.post(f"/api/clips/{pending}/pin", json={"pinned": True}).status_code == 409
    assert client.post("/api/clips/nope/pin", json={"pinned": True}).status_code == 409
    with session_scope() as db:
        posted = db.get(Video, a)
        posted.youtube_video_id = "yt_live_1"
        posted.status = VideoStatus.PUBLISHED
    assert client.post(f"/api/clips/{a}/pin", json={"pinned": True}).status_code == 409

    # pulling back to Review or rejecting from the shelf drops the pin
    assert client.post(f"/api/clips/{b}/undo").status_code == 200
    with session_scope() as db:
        assert "pinned_at" not in (db.get(Video, b).meta or {})
    with pytest.raises(approval.ReviewError):
        approval.pin_clip_next(b)  # back in review now


def test_channel_clone_and_portable_import_routes_are_safe(tmp_path, monkeypatch):
    from app.review import review_app
    from app.review_ui import PAGE

    assert "Import channels JSON" in PAGE
    assert "/api/channels/import" in PAGE
    assert "/clone" in PAGE and "Clone" in PAGE

    unauth = TestClient(review_app)
    assert unauth.post("/api/channels/tech/clone", json={"new_slug": "t2"}).status_code == 401
    assert unauth.post("/api/channels/import", json={"payload": {"channels": []}}).status_code == 401

    _write(tmp_path, monkeypatch, {"channels": [
        {"slug": "tech", "name": "Tech", "niche": "gadgets", "uploads_per_day": 2,
         "feeds": ["https://www.youtube.com/@mkbhd"]}]})
    with session_scope() as db:
        channels.sync_channels(db)

    c = _client()

    def overview():
        return {row["slug"]: row for row in c.get("/api/channels").json()["channels"]}

    cloned = c.post("/api/channels/tech/clone", json={"new_slug": "tech2", "name": "Tech Two"})
    assert cloned.status_code == 200, cloned.text
    assert cloned.json()["slug"] == "tech2"
    row = overview()["tech2"]
    assert row["enabled"] is False and row["connected"] is False
    assert row["niche"] == "gadgets" and row["uploads_per_day"] == 2

    assert c.post("/api/channels/tech/clone", json={"new_slug": "tech2"}).status_code == 400  # exists
    assert c.post("/api/channels/nope/clone", json={"new_slug": "zzz"}).status_code == 400
    assert c.post("/api/channels/tech/clone", json={}).status_code == 400  # no new slug
    assert c.post("/api/channels/tech/clone", json={"new_slug": "Bad Slug!"}).status_code == 400

    smuggled = {"channels": [{
        "slug": "comedy2", "name": "Comedy Two", "niche": "pranks", "uploads_per_day": 4, "enabled": True,
        "token_path": "C:/secret/tok.json", "client_secrets": "C:/secret/cs.json", "youtube_channel_id": "UCevil",
        "feeds": ["https://www.youtube.com/@KSI"],
    }]}
    dry = c.post("/api/channels/import", json={"payload": smuggled, "dry_run": True})
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True and dry.json()["created"] == ["comedy2"]
    assert "comedy2" not in overview()

    applied = c.post("/api/channels/import", json={"payload": smuggled, "dry_run": False})
    assert applied.status_code == 200, applied.text
    assert applied.json()["created"] == ["comedy2"]
    assert "C:/secret" not in applied.text and "UCevil" not in applied.text
    imported = overview()["comedy2"]
    assert imported["enabled"] is False  # imports never switch a channel on by themselves
    assert imported["uploads_per_day"] == 4
    assert "C:/secret" not in c.get("/api/channels").text

    # a /api/backup JSON (default row, status extras) is accepted as-is
    backup = {"exported_at": "2026-01-01T00:00:00+00:00", "queue": {"clips": []}, "channels": [
        {"slug": "default", "name": "default", "default": True, "enabled": True, "feeds": []},
        {"slug": "tech", "name": "Tech Renamed", "niche": "gadgets", "uploads_per_day": 2, "default": False,
         "enabled": True, "waiting": 3, "approved": 1, "paused": False, "connected": False,
         "feeds": ["https://www.youtube.com/@mkbhd"]},
    ]}
    restored = c.post("/api/channels/import", json={"payload": backup, "dry_run": False})
    assert restored.status_code == 200, restored.text
    assert restored.json()["updated"] == ["tech"] and restored.json()["created"] == []
    assert overview()["tech"]["name"] == "Tech Renamed"

    assert c.post("/api/channels/import", json={"payload": {"channels": "nope"}}).status_code == 400
    assert c.post("/api/channels/import", json={}).status_code == 400
    assert c.post("/api/channels/import", json={"payload": {"channels": [{"slug": "x", "timezone": "Not/AZone"}]}}).status_code == 400


def test_system_doctor_routes_are_guarded_no_store_and_secret_free(monkeypatch):
    from scripts import doctor as sys_doctor
    from app.review import review_app
    from app.review_ui import PAGE

    assert "System health" in PAGE
    assert "Fix missing folders" in PAGE
    assert "/api/system/doctor" in PAGE

    unauth = TestClient(review_app)
    assert unauth.get("/api/system/doctor").status_code == 401
    assert unauth.post("/api/system/doctor/fix").status_code == 401

    c = _client()
    real = c.get("/api/system/doctor")
    assert real.status_code == 200
    assert "no-store" in (real.headers.get("cache-control") or "").lower()
    assert "panel-test-key" not in real.text
    data = real.json()
    assert isinstance(data["env"]["review_token_set"], bool)
    assert all(isinstance(v, bool) for v in data["env"]["optional_keys"].values())
    assert data["fixes_applied"] == []  # a plain check never creates anything

    calls: list[bool] = []

    def fake_run_doctor(*, fix=False, **_kw):
        calls.append(fix)
        return {"ok": True, "fixes_applied": ["created logs/"] if fix else [], "warnings": []}

    monkeypatch.setattr(sys_doctor, "run_doctor", fake_run_doctor)
    checked = c.get("/api/system/doctor")
    assert checked.json()["fixes_applied"] == []
    fixed = c.post("/api/system/doctor/fix")
    assert fixed.status_code == 200
    assert fixed.json()["fixes_applied"] == ["created logs/"]
    assert "no-store" in (fixed.headers.get("cache-control") or "").lower()
    assert calls == [False, True]
