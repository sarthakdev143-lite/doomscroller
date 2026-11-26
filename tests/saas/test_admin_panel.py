"""Python admin panel (parked after the edge launch pivot): access control, CSV safety, pages render."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

ADMIN = SimpleNamespace(id="usr_admin", email="owner@example.com", role="admin", status="active")
PAGES = ["/admin", "/admin/live", "/admin/api/live", "/admin/visitors", "/admin/pages", "/admin/funnel",
         "/admin/sources", "/admin/waitlist", "/admin/waitlist/export.csv", "/admin/users", "/admin/security",
         "/admin/audit"]


@pytest.mark.parametrize("url", PAGES)
def test_denied_without_admin(client, url):
    r = client.get(url, headers={"Accept": "application/json"}, follow_redirects=False)
    assert r.status_code in (401, 403, 303)
    assert "owner@example.com" not in r.text


def test_denied_when_core_says_no_totp(client):
    from fastapi import HTTPException

    from saas.admin import guard

    def no_mfa():
        raise HTTPException(403, {"error": "mfa_required", "message": "needs 2FA"})

    client.app.dependency_overrides[guard.require_admin] = no_mfa
    assert client.get("/admin/api/live").status_code == 403
    client.app.dependency_overrides[guard.require_admin] = lambda: SimpleNamespace(id="u", role="user", status="active")
    assert client.get("/admin/api/live").status_code == 403


def test_ip_allowlist_hides_panel(client, monkeypatch):
    from saas.admin import guard
    from saas.config import settings

    client.app.dependency_overrides[guard.require_admin] = lambda: ADMIN
    monkeypatch.setattr(settings, "admin_ip_allowlist", "10.0.0.0/8")
    assert client.get("/admin/api/live").status_code == 404


def test_admin_mutation_needs_csrf_and_is_audited(client, saas_db):
    from sqlalchemy import select

    from saas.admin import guard
    from saas.models.security import AuditLog

    client.app.dependency_overrides[guard.require_admin] = lambda: ADMIN
    client.cookies.set("sid", "s")
    client.cookies.set("csrf", "c" * 32)
    body = {"kind": "ip", "value": "198.51.100.200", "reason": "t"}
    assert client.post("/admin/api/bans", json=body, headers={"Origin": "http://testserver"}).status_code == 403
    r = client.post("/admin/api/bans", json=body, headers={"Origin": "http://testserver", "X-CSRF-Token": "c" * 32})
    assert r.status_code == 200, r.text
    row = saas_db.execute(select(AuditLog).where(AuditLog.action == "ban_add")).scalar_one()
    assert row.actor_email == "owner@example.com" and row.target_id == "198.51.100.200"


@pytest.mark.parametrize("raw,expected", [("=HYPERLINK(\"x\")", "'=HYPERLINK(\"x\")"), ("+1", "'+1"), ("-2+3", "'-2+3"),
                                          ("@SUM(A1)", "'@SUM(A1)"), ("\t=1", "'\t=1"), ("ok@x.com", "ok@x.com"),
                                          (-5, "-5"), ("a\x00b", "ab")])
def test_csv_cells_are_formula_safe(raw, expected):
    from saas.admin.csvutil import safe_cell

    assert safe_cell(raw) == expected


def test_all_pages_render_with_data(client, monkeypatch, saas_db):
    from saas.admin import guard
    from saas.config import settings
    from saas.models.tracking import Visitor

    monkeypatch.setattr(settings, "consent_mode", "notice_only")
    client.app.dependency_overrides[guard.require_admin] = lambda: ADMIN
    h = {"Origin": "http://testserver", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"}
    now = time.time() * 1000
    client.get("/api/t/config")
    client.post("/api/t", headers=h, json={
        "v": 1, "pv": "p_" + "a" * 24, "now": now, "url": "/pricing?utm_source=hn",
        "layout": [{"n": "hero", "t": 0, "h": 0.3}],
        "e": [{"t": "pageview"}, {"t": "dwell", "n": "pricing", "v": 40000},
              {"t": "click", "n": "cta-join", "x": 0.4, "y": 0.2}, {"t": "imp", "n": "cta-join"}]})
    client.post("/api/waitlist", headers=h, json={"email": "=cmd@company.io", "fp": "ab" * 16, "ts": now - 5000,
                                                  "website": ""})
    vid = saas_db.query(Visitor).first().id
    for url in PAGES + [f"/admin/visitors/{vid}", "/admin/pages/view?path=/pricing"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    csv = client.get("/admin/waitlist/export.csv").text
    assert "'=cmd@company.io" in csv
