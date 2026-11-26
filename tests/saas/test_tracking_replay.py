"""Session replay: consent-gated, sampled, bounded, never for bots, never for someone else's page."""
from __future__ import annotations

import gzip
import json
import time

import pytest
from sqlalchemy import func, select

CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")
H = {"Origin": "http://testserver", "User-Agent": CHROME}
PV = "p_" + "c" * 24


def rr_events(n=3):
    t = int(time.time() * 1000)
    evs = [{"type": 4, "timestamp": t, "data": {"href": "http://testserver/reset/" + "Q" * 30 + "?x=1",
                                                "width": 1, "height": 1}}]
    evs += [{"type": 3, "timestamp": t + i, "data": {"source": 1}} for i in range(1, n)]
    return evs


def chunks(db):
    from saas.models.tracking import ReplayChunk

    return db.execute(select(func.count()).select_from(ReplayChunk)).scalar_one()


def start_page(client, *, ua=CHROME, sig=None):
    body = {"v": 1, "pv": PV, "now": time.time() * 1000, "url": "/", "e": [{"t": "pageview"}]}
    if sig:
        body["sig"] = sig
    return client.post("/api/t", json=body, headers={**H, "User-Agent": ua})


@pytest.fixture()
def settings():
    from saas.config import settings as s

    return s


def test_no_replay_stored_without_consent(client, saas_db):
    # eu_strict + unknown country: consent pending, nothing may be recorded.
    r = client.post(f"/api/t/replay?pv={PV}&seq=0", content=gzip.compress(json.dumps(rr_events()).encode()), headers=H)
    assert r.status_code == 403 and r.json()["error"] == "no_consent"
    assert chunks(saas_db) == 0


def test_replay_needs_consent_on_the_visitor_row_too(client, saas_db, settings, monkeypatch):
    """A forged ds_consent cookie for a visitor the server never saw is not enough."""
    client.cookies.set("ds_consent", "granted.x")
    client.cookies.set("vid", "v_" + "9" * 32)
    r = client.post(f"/api/t/replay?pv={PV}&seq=0", content=json.dumps(rr_events()).encode(), headers=H)
    assert r.status_code in (403, 404) and chunks(saas_db) == 0


def test_replay_stored_with_consent_and_redacted(client, saas_db):
    from saas.models.tracking import PageView, ReplayChunk
    from saas.tracking import replay

    client.post("/api/consent", json={"choice": "granted"}, headers=H)
    start_page(client)
    body = gzip.compress(json.dumps(rr_events(5)).encode())
    r = client.post(f"/api/t/replay?pv={PV}&seq=0", content=body, headers=H)
    assert r.status_code == 202, r.text
    row = saas_db.execute(select(ReplayChunk)).scalar_one()
    assert row.n_events == 5 and saas_db.get(PageView, PV).has_replay
    events = replay.load_events(saas_db, PV)
    assert events[0]["data"]["href"] == "/reset/:token"
    # A retried upload of the same sequence number is not stored twice.
    client.post(f"/api/t/replay?pv={PV}&seq=0", content=body, headers=H)
    assert chunks(saas_db) == 1


def test_uncompressed_final_chunk_accepted(client, saas_db):
    client.post("/api/consent", json={"choice": "granted"}, headers=H)
    start_page(client)
    r = client.post(f"/api/t/replay?pv={PV}&seq=1", content=json.dumps(rr_events()).encode(),
                    headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 202 and chunks(saas_db) == 1


def test_gzip_bomb_rejected(client, saas_db):
    client.post("/api/consent", json={"choice": "granted"}, headers=H)
    start_page(client)
    bomb = gzip.compress(b"[" + b" " * (20 * 1024 * 1024) + b"]")  # ~20 KB on the wire, 20 MB inflated
    assert len(bomb) < 1024 * 1024
    r = client.post(f"/api/t/replay?pv={PV}&seq=0", content=bomb, headers=H)
    assert r.status_code == 413 and chunks(saas_db) == 0


@pytest.mark.parametrize("payload", [b"not json", b"{}", b"[1,2,3]", json.dumps([{"type": "x"}]).encode()])
def test_malformed_chunks_rejected(client, saas_db, payload):
    client.post("/api/consent", json={"choice": "granted"}, headers=H)
    start_page(client)
    r = client.post(f"/api/t/replay?pv={PV}&seq=0", content=payload, headers=H)
    assert r.status_code == 400 and chunks(saas_db) == 0


def test_unknown_page_rejected(client, saas_db):
    client.post("/api/consent", json={"choice": "granted"}, headers=H)
    r = client.post(f"/api/t/replay?pv=p_{'d' * 24}&seq=0", content=json.dumps(rr_events()).encode(), headers=H)
    assert r.status_code == 404 and chunks(saas_db) == 0


def test_sampling_enforced_server_side(client, saas_db, settings, monkeypatch):
    monkeypatch.setattr(settings, "replay_sample_rate", 0.0)
    client.post("/api/consent", json={"choice": "granted"}, headers=H)
    start_page(client)
    assert client.get("/api/t/config").json()["replay"] is False
    r = client.post(f"/api/t/replay?pv={PV}&seq=0", content=json.dumps(rr_events()).encode(), headers=H)
    assert r.status_code == 403 and r.json()["error"] == "not_sampled"


def test_bots_are_not_recorded(client, saas_db):
    client.post("/api/consent", json={"choice": "granted"}, headers=H)
    start_page(client, sig={"auto": ["webdriver"]})
    r = client.post(f"/api/t/replay?pv={PV}&seq=0", content=json.dumps(rr_events()).encode(), headers=H)
    assert r.status_code == 403 and chunks(saas_db) == 0


def test_session_byte_cap(client, saas_db, settings, monkeypatch):
    from saas.models.tracking import TrackSession

    client.post("/api/consent", json={"choice": "granted"}, headers=H)
    start_page(client)
    sess = saas_db.execute(select(TrackSession)).scalar_one()
    sess.replay_bytes = settings.replay_max_session_mb * 1024 * 1024
    saas_db.commit()
    r = client.post(f"/api/t/replay?pv={PV}&seq=0", content=json.dumps(rr_events()).encode(), headers=H)
    assert r.status_code == 413


def test_sampling_is_deterministic():
    from saas.tracking import replay

    vid = "v_" + "1" * 32
    assert replay.sampled(vid, 0.5) == replay.sampled(vid, 0.5)
    hits = sum(replay.sampled(f"v_{i:032x}", 0.3) for i in range(2000))
    assert 450 < hits < 750
