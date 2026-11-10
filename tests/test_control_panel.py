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
    from app.review import review_app

    c = TestClient(review_app)  # no key
    for path in ("/api/channels", "/api/report", "/api/doctor", "/api/controls"):
        assert c.get(path).status_code == 401, path
    assert c.post("/api/channels", json={"slug": "x"}).status_code == 401
    assert c.post("/api/controls/reload").status_code == 401


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
    assert {a["action"] for a in listing["actions"]} >= {"supply", "watch", "collect", "learn", "reload"}
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


def test_resume_all_and_unblock_endpoints():
    c = _client()
    assert c.post("/api/resume-all").json() == {"ok": False}  # nothing paused
    assert c.post("/api/unblock", json={"name": "nobody"}).json() == {"name": "nobody", "lifted": 0}
