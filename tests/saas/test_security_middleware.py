"""Security middleware: headers, CSRF double-submit, origin checks, body caps, bans, global limits."""
from __future__ import annotations

import pytest

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture()
def settings():
    from saas.config import settings as s

    return s


def test_security_headers_on_every_response(client):
    r = client.get("/api/t/config")
    h = r.headers
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in h["permissions-policy"] and "microphone=()" in h["permissions-policy"]
    csp = h["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "https://challenges.cloudflare.com" in csp  # Turnstile
    assert "'unsafe-eval'" not in csp
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src and "'nonce-" in script_src
    assert "strict-transport-security" not in h  # dev
    assert h["cache-control"] == "no-store"


def test_hsts_only_in_prod(client, monkeypatch, settings):
    monkeypatch.setattr(settings, "env", "prod")
    r = client.get("/api/t/config")
    assert "max-age=" in r.headers["strict-transport-security"]
    assert "upgrade-insecure-requests" in r.headers["content-security-policy"]


def test_nonce_differs_per_request(client):
    a = client.get("/api/t/config").headers["content-security-policy"]
    b = client.get("/api/t/config").headers["content-security-policy"]
    assert a != b


def test_csrf_cookie_set_on_html_only(client):
    html = client.get("/waitlist/confirm/doesnotexist")
    assert html.headers["content-type"].startswith("text/html")
    assert "csrf=" in html.headers.get("set-cookie", "")
    js = client.get("/api/t/config")
    assert "csrf=" not in js.headers.get("set-cookie", "")


def test_csrf_cookie_not_reissued_when_present(client):
    client.cookies.set("csrf", "a" * 32)
    r = client.get("/waitlist/confirm/doesnotexist")
    assert "csrf=" not in r.headers.get("set-cookie", "")


def test_csrf_required_with_session_cookie(client):
    client.cookies.set("sid", "some-session")
    client.cookies.set("csrf", "tok_" + "x" * 28)
    r = client.post("/api/consent", json={"choice": "denied"}, headers=ORIGIN)
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"
    r = client.post("/api/consent", json={"choice": "denied"}, headers={**ORIGIN, "X-CSRF-Token": "wrong"})
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"
    r = client.post("/api/consent", json={"choice": "denied"},
                    headers={**ORIGIN, "X-CSRF-Token": "tok_" + "x" * 28})
    assert r.status_code == 200, r.text


def test_csrf_not_required_without_session(client):
    r = client.post("/api/consent", json={"choice": "denied"}, headers=ORIGIN)
    assert r.status_code == 200


def test_csrf_missing_cookie_fails_even_with_header(client):
    client.cookies.set("sid", "s")
    r = client.post("/api/consent", json={"choice": "denied"}, headers={**ORIGIN, "X-CSRF-Token": ""})
    assert r.status_code == 403


@pytest.mark.parametrize("path", ["/webhooks/stripe", "/api/v1/jobs", "/mcp", "/api/t", "/api/t/replay?pv=p_x&seq=0"])
def test_csrf_exempt_paths(client, path):
    client.cookies.set("sid", "s")
    r = client.post(path, content=b"{}", headers=ORIGIN)
    assert not (r.status_code == 403 and r.headers.get("content-type", "").startswith("application/json")
                and r.json().get("error") == "csrf_failed")


def test_cross_site_origin_refused(client):
    r = client.post("/api/consent", json={"choice": "granted"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    r = client.post("/api/consent", json={"choice": "granted"}, headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403


def test_public_url_origin_allowed(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://clips.example.com")
    r = client.post("/api/consent", json={"choice": "denied"}, headers={"Origin": "https://clips.example.com"})
    assert r.status_code == 200


def test_body_cap_by_content_length(client):
    big = b"x" * (300 * 1024)
    r = client.post("/api/consent", content=big, headers={**ORIGIN, "Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"


def test_body_cap_beacon_is_smaller(client):
    r = client.post("/api/t", content=b"x" * (70 * 1024), headers=ORIGIN)
    assert r.status_code == 413


def test_body_cap_on_streamed_body(client):
    def gen():
        for _ in range(100):
            yield b"y" * 8192  # 800 KB, no Content-Length

    r = client.post("/api/consent", content=gen(), headers={**ORIGIN, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_spoofed_forwarded_for_ignored_by_default(client, saas_db):
    from saas.security import bans

    bans.add(saas_db, "ip", "203.0.113.7", "test")
    saas_db.commit()
    r = client.get("/api/t/config", headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200  # header not trusted: the client is "testclient", not the banned IP


def test_trusted_proxy_uses_rightmost_hop(client, saas_db, settings, monkeypatch):
    from saas.security import bans

    monkeypatch.setattr(settings, "trust_proxy", "forwarded")
    bans.add(saas_db, "ip", "203.0.113.7", "test")
    saas_db.commit()
    # A client prepending a fake hop cannot escape: the proxy-appended right-most entry decides.
    r = client.get("/api/t/config", headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})
    assert r.status_code == 403 and r.json()["error"] == "banned"
    r = client.get("/api/t/config", headers={"X-Forwarded-For": "203.0.113.7, 198.51.100.2"})
    assert r.status_code == 200


def test_cidr_ban_and_static_bypass(client, saas_db, settings, monkeypatch):
    from saas.security import bans

    monkeypatch.setattr(settings, "trust_proxy", "forwarded")
    bans.add(saas_db, "ip", "198.51.100.0/24", "range")
    saas_db.commit()
    assert client.get("/api/t/config", headers={"X-Forwarded-For": "198.51.100.77"}).status_code == 403
    assert client.get("/ts/t.js", headers={"X-Forwarded-For": "198.51.100.77"}).status_code == 200


def test_visitor_ban(client, saas_db):
    from saas.security import bans

    vid = "v_" + "1" * 32
    bans.add(saas_db, "visitor", vid, "test")
    saas_db.commit()
    client.cookies.set("vid", vid)
    assert client.get("/api/t/config").status_code == 403


def test_expired_ban_ignored(client, saas_db, settings, monkeypatch):
    from datetime import timedelta

    from saas.common import utcnow
    from saas.security import bans

    monkeypatch.setattr(settings, "trust_proxy", "forwarded")
    ban = bans.add(saas_db, "ip", "203.0.113.9", "t", hours=1)
    ban.expires_at = utcnow() - timedelta(minutes=1)
    saas_db.commit()
    bans.invalidate()
    assert client.get("/api/t/config", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 200


def test_ban_validation():
    from saas.security import bans

    with pytest.raises(ValueError):
        bans.validate("ip", "not-an-ip")
    with pytest.raises(ValueError):
        bans.validate("ip", "0.0.0.0/0")  # would ban the internet
    with pytest.raises(ValueError):
        bans.validate("nonsense", "x")
    assert bans.validate("asn", "AS14061") == "14061"
    assert bans.validate("email", " Bad@Example.COM ") == "bad@example.com"


def test_global_rate_limit(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_global_per_min", 5)
    codes = [client.get("/api/t/config").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5
    assert codes[-1] == 429
    r = client.get("/api/t/config")
    assert r.json()["error"] == "rate_limited" and int(r.headers["retry-after"]) >= 1


def test_static_assets_skip_global_limit(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_global_per_min", 2)
    assert all(client.get("/ts/t.js").status_code == 200 for _ in range(5))


def test_rate_limited_events_are_deduped(client, saas_db, settings, monkeypatch):
    from sqlalchemy import func, select

    from saas.models.security import SecurityEvent

    monkeypatch.setattr(settings, "rate_limit_global_per_min", 1)
    for _ in range(6):
        client.get("/api/t/config")
    n = saas_db.execute(select(func.count()).select_from(SecurityEvent)
                        .where(SecurityEvent.kind == "rate_limited")).scalar_one()
    assert n == 1


def test_request_state_exposes_nonce_and_csrf(client):
    r = client.get("/waitlist/confirm/whatever-token-value")
    nonce = r.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert r.status_code == 404  # invalid token page still renders
    assert nonce
