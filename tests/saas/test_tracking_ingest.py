"""Tracker config/consent, ingest validation and enrichment, identity links and lead scoring."""
from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import func, select

CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")
H = {"Origin": "http://testserver", "User-Agent": CHROME}


@pytest.fixture()
def settings():
    from saas.config import settings as s

    return s


@pytest.fixture()
def notice(settings, monkeypatch):
    """Notice-only consent: analytics on unless the visitor opts out (the non-EU experience)."""
    monkeypatch.setattr(settings, "consent_mode", "notice_only")


def batch(pv="p_" + "a" * 24, events=None, **extra):
    now = time.time() * 1000
    b = {"v": 1, "pv": pv, "now": now, "url": "/", "title": "Home",
         "e": events if events is not None else [{"t": "pageview", "ts": now}]}
    b.update(extra)
    return b


def count(db, model):
    return db.execute(select(func.count()).select_from(model)).scalar_one()


# ---- consent ------------------------------------------------------------------------------------
def test_eu_strict_unknown_country_must_ask(client):
    r = client.get("/api/t/config")
    body = r.json()
    assert body["consent_required"] is True and body["consent"] == "pending"
    assert "vid" not in body and "vid=" not in r.headers.get("set-cookie", "")


def test_eu_visitor_asked_and_us_visitor_noticed(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", "cloudflare")
    de = client.get("/api/t/config", headers={"CF-IPCountry": "DE", "CF-Connecting-IP": "198.51.100.4"}).json()
    assert de["consent"] == "pending"
    us = client.get("/api/t/config", headers={"CF-IPCountry": "US", "CF-Connecting-IP": "198.51.100.5"})
    assert us.json()["consent"] == "implied" and us.json()["notice"] is True
    assert "vid=" in us.headers["set-cookie"] and "HttpOnly" in us.headers["set-cookie"]


def test_gpc_means_declined(client, notice):
    assert client.get("/api/t/config", headers={"Sec-GPC": "1"}).json()["consent"] == "denied"


def test_no_events_stored_without_consent(client, saas_db):
    from saas.models.tracking import TrackEvent, Visitor

    r = client.post("/api/t", json=batch(), headers=H)
    assert r.status_code == 202 and r.json()["stored"] == 0 and r.json()["reason"] == "consent"
    assert count(saas_db, TrackEvent) == 0 and count(saas_db, Visitor) == 0


def test_accepting_consent_creates_visitor_and_cookie(client, saas_db):
    from saas.models.tracking import Visitor

    r = client.post("/api/consent", json={"choice": "granted"}, headers=H)
    assert r.status_code == 200 and r.json()["consent"] == "granted"
    assert "ds_consent=granted" in r.headers["set-cookie"] and "vid=" in r.headers["set-cookie"]
    v = saas_db.execute(select(Visitor)).scalar_one()
    assert v.consent == "granted" and v.browser == "Chrome" and v.os == "Windows"
    assert client.post("/api/t", json=batch(), headers=H).json()["stored"] == 1


def test_declining_erases_and_stops(client, saas_db, notice):
    from saas.models.tracking import TrackEvent, Visitor

    client.get("/api/t/config")
    client.post("/api/t", json=batch(), headers=H)
    assert count(saas_db, TrackEvent) == 1
    r = client.post("/api/consent", json={"choice": "denied"}, headers=H)
    assert "vid=" in r.headers["set-cookie"]  # cleared
    saas_db.expire_all()
    assert count(saas_db, TrackEvent) == 0
    assert saas_db.execute(select(Visitor)).scalar_one().consent == "denied"
    assert client.post("/api/t", json=batch(pv="p_" + "b" * 24), headers=H).json()["stored"] == 0


def test_bad_consent_choice(client):
    assert client.post("/api/consent", json={"choice": "maybe"}, headers=H).status_code == 400


# ---- ingest validation ---------------------------------------------------------------------------
def test_requires_our_origin(client, notice):
    assert client.post("/api/t", json=batch(), headers={"User-Agent": CHROME}).status_code == 403
    r = client.post("/api/t", json=batch(), headers={"Origin": "https://evil.example", "User-Agent": CHROME})
    assert r.status_code == 403


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"v":1}', json.dumps({"pv": "bad", "now": 1, "url": "/"}).encode(),
                                  json.dumps(batch(e=[{"t": "x"}] * 301)).encode()])
def test_malformed_batches_rejected(client, notice, body):
    r = client.post("/api/t", content=body, headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 400 and r.json()["error"] == "bad_payload"


def test_oversized_batch_rejected(client, notice):
    r = client.post("/api/t", content=b"{" + b" " * (65 * 1024) + b"}", headers=H)
    assert r.status_code == 413


def test_beacon_text_plain_accepted(client, notice):
    r = client.post("/api/t", content=json.dumps(batch()).encode(), headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 202 and r.json()["stored"] == 1


def test_unknown_event_types_dropped_not_fatal(client, notice):
    r = client.post("/api/t", json=batch(events=[{"t": "pageview"}, {"t": "keylog", "n": "secret"}]), headers=H)
    assert r.json()["stored"] == 1


def test_secret_paths_and_query_strings_are_redacted(client, saas_db, notice):
    from saas.models.tracking import PageView, TrackEvent

    token = "Zx9" * 12
    client.post("/api/t", json=batch(url=f"/reset/{token}?email=a@b.c&utm_source=nl"), headers=H)
    pv = saas_db.execute(select(PageView)).scalar_one()
    assert pv.path == "/reset/:token"
    stored = " ".join(str(x) for x in saas_db.execute(select(TrackEvent.path, TrackEvent.props)).all())
    assert token not in stored and "a@b.c" not in stored


def test_props_are_capped_primitives(client, saas_db, notice):
    from saas.models.tracking import TrackEvent

    props = {f"k{i}": "v" * 500 for i in range(30)} | {"nested": {"password": "hunter2"}}
    client.post("/api/t", json=batch(events=[{"t": "custom", "n": "x", "d": props}]), headers=H)
    ev = saas_db.execute(select(TrackEvent).where(TrackEvent.type == "custom")).scalar_one()
    assert len(ev.props) <= 12 and all(len(v) <= 120 for v in ev.props.values())
    assert "hunter2" not in json.dumps(ev.props)


def test_pageview_counts_once_per_page_id(client, saas_db, notice):
    from saas.models.tracking import Visitor

    client.post("/api/t", json=batch(), headers=H)
    client.post("/api/t", json=batch(events=[{"t": "pageview"}, {"t": "pageview"}]), headers=H)
    assert saas_db.execute(select(Visitor)).scalar_one().pageviews == 1


def test_foreign_page_id_cannot_be_hijacked(client, saas_db, notice):
    from fastapi.testclient import TestClient

    client.post("/api/t", json=batch(), headers=H)
    other = TestClient(client.app)
    other.get("/api/t/config")
    r = other.post("/api/t", json=batch(events=[{"t": "click", "n": "x"}]), headers=H)
    assert r.json()["stored"] == 0


def test_enrichment_sessions_and_attribution(client, saas_db, notice):
    from saas.models.tracking import TrackSession, Visitor

    client.get("/api/t/config")
    client.post("/api/t", json=batch(url="/pricing?utm_source=newsletter&utm_campaign=launch",
                                     ref="https://news.ycombinator.com/item?id=1", sig={"sw": 1440, "auto": []},
                                     fp="ef" * 16), headers=H)
    v = saas_db.execute(select(Visitor)).scalar_one()
    s = saas_db.execute(select(TrackSession)).scalar_one()
    assert v.device_type == "desktop" and not v.is_bot
    assert v.first_source == "newsletter" and v.first_campaign == "launch" and v.first_landing == "/pricing"
    assert s.referrer_host == "news.ycombinator.com" and s.referrer == "https://news.ycombinator.com/item"
    assert v.fingerprint == "ef" * 16 and v.client["sw"] == 1440


def test_bot_classification(client, saas_db, notice):
    from saas.models.tracking import Visitor

    client.post("/api/t", json=batch(sig={"auto": ["webdriver"]}), headers=H)
    v = saas_db.execute(select(Visitor)).scalar_one()
    assert v.is_bot and "webdriver" in v.bot_reasons and v.lead_label == "bot" and v.lead_score == 0


def test_headless_ua_is_bot(client, saas_db, notice):
    from saas.models.tracking import Visitor

    ua = CHROME.replace("Chrome/", "HeadlessChrome/")
    client.post("/api/t", json=batch(), headers={**H, "User-Agent": ua})
    assert saas_db.execute(select(Visitor)).scalar_one().is_bot


def test_aggregates_feed_lead_score(client, saas_db, notice):
    from saas.models.tracking import Visitor

    now = time.time() * 1000
    events = [{"t": "pageview"}, {"t": "hb", "v": 30000}, {"t": "dwell", "n": "pricing", "v": 70000},
              {"t": "click", "n": "cta-join-waitlist", "x": 0.5, "y": 0.2, "s": "hero"},
              {"t": "focus", "n": "waitlist", "d": {"f": "email"}}, {"t": "scroll", "v": 90},
              {"t": "heat", "d": {"p": [[0.1, 0.2], [0.3, 0.4], ["bad"]]}},
              {"t": "vital", "n": "LCP", "v": 1234}, {"t": "vital", "n": "BOGUS", "v": 1}]
    client.post("/api/t", json=batch(url="/pricing", events=events, now=now), headers=H)
    v = saas_db.execute(select(Visitor)).scalar_one()
    assert v.stats["sections"]["pricing"] == 70000 and v.stats["cta"]["cta-join-waitlist"] == 1
    assert v.stats["forms"]["start"] == 1 and v.stats["scroll"]["/pricing"] == 90
    assert v.lead_score > 30 and any("pricing" in r for r in v.lead_reasons)
    assert v.interests and v.interests[0]["topic"] == "pricing & plans"


def test_identity_link(client, saas_db, notice):
    from saas.models.tracking import Visitor, VisitorLink
    from saas.tracking import identity

    client.post("/api/t", json=batch(), headers=H)
    v = saas_db.execute(select(Visitor)).scalar_one()
    assert identity.link(saas_db, v.id, user_id="usr_1") is True
    assert identity.link(saas_db, v.id, user_id="usr_1") is True  # idempotent
    saas_db.commit()
    assert count(saas_db, VisitorLink) == 1
    assert identity.link(saas_db, "garbage", user_id="usr_1") is False
    assert identity.visitors_for(saas_db, user_id="usr_1") == [v.id]


def test_tracking_disabled_drops_everything(client, saas_db, settings, monkeypatch, notice):
    from saas.models.tracking import TrackEvent

    monkeypatch.setattr(settings, "tracking_enabled", False)
    assert client.post("/api/t", json=batch(), headers=H).json()["stored"] == 0
    assert count(saas_db, TrackEvent) == 0


def test_per_visitor_rate_limit(client, notice):
    client.get("/api/t/config")
    codes = {client.post("/api/t", json=batch(events=[{"t": "custom", "n": "x"}]), headers=H).status_code
             for _ in range(125)}
    assert 429 in codes


def test_csp_reports_are_logged_once(client, saas_db):
    from saas.models.security import SecurityEvent

    rep = {"csp-report": {"violated-directive": "script-src", "blocked-uri": "https://evil.example/x.js",
                          "document-uri": "http://testserver/pricing"}}
    for _ in range(3):
        assert client.post("/api/t/csp", json=rep, headers={"Content-Type": "application/csp-report"}).status_code == 204
    evs = saas_db.execute(select(SecurityEvent).where(SecurityEvent.kind == "csp_violation")).scalars().all()
    assert len(evs) == 1 and evs[0].detail["page"] == "/pricing"


def test_retention_cleanup(saas_db):
    from datetime import timedelta

    from saas.common import utcnow
    from saas.models.tracking import ReplayChunk, TrackEvent
    from saas.tracking import retention

    old = utcnow() - timedelta(days=120)
    saas_db.add(TrackEvent(visitor_id="v_x", session_id="ts_x", ts=old, type="click"))
    saas_db.add(TrackEvent(visitor_id="v_x", session_id="ts_x", ts=utcnow(), type="click"))
    saas_db.add(ReplayChunk(visitor_id="v_x", session_id="ts_x", page_id="p_x", seq=0, data=b"x",
                            created_at=utcnow() - timedelta(days=31)))
    saas_db.commit()
    out = retention.cleanup(saas_db)
    saas_db.commit()
    assert out["events"] == 1 and out["replay_chunks"] == 1
    assert count(saas_db, TrackEvent) == 1
