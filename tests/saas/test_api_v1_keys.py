"""API keys and the public v1 API: plan gate, hashing, scopes, revocation, per-key rate limit, IDOR."""
from __future__ import annotations

from core_fixtures import core, csrf, login, make_key, make_user  # noqa: F401


def test_key_creation_needs_studio_and_secret_is_shown_once(client, core, saas_db):
    from saas.models.core import ApiKey

    make_user(saas_db, "free@example.com")
    h = login(client, "free@example.com")
    r = client.post("/api/keys", json={"name": "ci"}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "plan_required"

    make_user(saas_db, "studio@example.com", plan="studio")
    h = login(client, "studio@example.com")
    r = client.post("/api/keys", json={"name": "ci", "scopes": ["jobs:read"]}, headers=h)
    assert r.status_code == 201
    raw = r.json()["key"]
    assert raw.startswith("dsk_live_") and len(raw) > 40
    row = saas_db.query(ApiKey).one()
    assert raw not in (row.secret_hash, row.prefix) and len(row.secret_hash) == 64
    listed = client.get("/api/keys").json()["keys"]
    assert "key" not in listed[0] and listed[0]["prefix"].endswith("…")
    assert client.post("/api/keys", json={"scopes": ["root"]}, headers=h).json()["error"] == "invalid_scope"


def test_v1_auth_errors_and_revocation(client, core, saas_db):
    u = make_user(saas_db, "v1@example.com", plan="studio", credits=10)
    raw = make_key(saas_db, u)
    assert client.get("/api/v1/balance").status_code == 401
    r = client.get("/api/v1/balance", headers={"Authorization": "Bearer dsk_live_wrong"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_api_key"
    assert r.headers.get("www-authenticate", "").startswith("Bearer")
    r = client.get("/api/v1/balance", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200 and r.json()["balance"] == 10
    from saas.models.core import ApiKey

    key = saas_db.query(ApiKey).one()
    h = login(client, "v1@example.com")
    assert client.delete(f"/api/keys/{key.id}", headers=h).status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/balance", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_v1_plan_downgrade_disables_existing_keys(client, core, saas_db):
    u = make_user(saas_db, "down@example.com", plan="studio")
    raw = make_key(saas_db, u)
    u.plan_slug = "pro"
    saas_db.commit()
    r = client.get("/api/v1/balance", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403 and r.json()["error"] == "plan_required"


def test_v1_scopes_are_enforced(client, core, saas_db):
    u = make_user(saas_db, "scope@example.com", plan="studio", credits=50)
    raw = make_key(saas_db, u, ["jobs:read"])
    auth = {"Authorization": f"Bearer {raw}"}
    r = client.post("/api/v1/jobs", json={"source_url": "https://www.youtube.com/watch?v=x"}, headers=auth)
    assert r.status_code == 403 and r.json()["error"] == "insufficient_scope"
    assert client.get("/api/v1/balance", headers=auth).json()["error"] == "insufficient_scope"
    assert client.get("/api/v1/jobs", headers=auth).status_code == 200


def test_v1_rate_limit_per_key(client, core, saas_db, monkeypatch):
    monkeypatch.setattr(core, "api_rate_per_minute", 3)
    u = make_user(saas_db, "rl@example.com", plan="studio")
    raw = make_key(saas_db, u)
    codes = [client.get("/api/v1/balance", headers={"Authorization": f"Bearer {raw}"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_keys_are_scoped_to_their_owner(client, core, saas_db):
    from saas.models.core import ApiKey

    a = make_user(saas_db, "ka@example.com", plan="studio")
    make_key(saas_db, a)
    a_key = saas_db.query(ApiKey).one()
    make_user(saas_db, "kb@example.com", plan="studio")
    h = login(client, "kb@example.com")
    assert client.delete(f"/api/keys/{a_key.id}", headers=h).status_code == 404
    assert client.get("/api/keys").json()["keys"] == []
    saas_db.refresh(a_key)
    assert a_key.revoked_at is None


def test_session_cookie_does_not_authenticate_v1(client, core, saas_db):
    make_user(saas_db, "cookie@example.com", plan="studio")
    login(client, "cookie@example.com")
    assert client.get("/api/v1/balance").status_code == 401
    csrf(client)
