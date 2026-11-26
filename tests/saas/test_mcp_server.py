"""The remote MCP server at /mcp: Bearer API keys, every tool end to end with MockEngine, isolation
between two users' interleaved calls, scopes, plan gate, revocation."""
from __future__ import annotations

import itertools
import json

import pytest

from core_fixtures import core, make_key, make_user  # noqa: F401
from test_jobs_lifecycle import URL, sources, worker  # noqa: F401

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "mcp-protocol-version": "2025-06-18"}
_ids = itertools.count(1)


def rpc(client, key, method, params=None):
    headers = dict(HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers)


def tool(client, key, name, **arguments):
    r = rpc(client, key, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        payload = text.split(": ", 1)[-1] if not text.startswith("{") else text
        return {"_error": json.loads(payload[payload.index("{"):])}
    return json.loads(text)


@pytest.fixture()
def studio(core, saas_db):
    u = make_user(saas_db, "mcp@example.com", plan="studio", credits=30)
    return u, make_key(saas_db, u)


def test_initialize_and_list_tools(client, studio):
    _u, key = studio
    r = rpc(client, key, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "pytest", "version": "1"}})
    assert r.status_code == 200
    info = r.json()["result"]
    assert "credits" in info["instructions"].lower()
    names = {t["name"] for t in rpc(client, key, "tools/list").json()["result"]["tools"]}
    assert names == {"get_balance", "estimate_video", "create_clips", "get_job", "list_jobs", "list_clips",
                     "get_clip_links", "get_publish_package", "cancel_job"}


def test_auth_is_required_and_checked(client, core, saas_db, studio):
    r = rpc(client, None, "tools/list")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["error"] == "invalid_api_key"
    assert rpc(client, "dsk_live_nope", "tools/list").status_code == 401
    pro = make_user(saas_db, "pro-mcp@example.com", plan="pro")
    pro_key = make_key(saas_db, pro)
    r = rpc(client, pro_key, "tools/list")
    assert r.status_code == 403 and r.json()["error"] == "plan_required"


def test_full_flow_with_mock_engine(client, core, saas_db, studio, sources):
    _u, key = studio
    assert tool(client, key, "get_balance") == {"balance": 30, "plan": "studio"}
    est = tool(client, key, "estimate_video", url=URL)
    assert est["ok"] is True and est["credits"] == 5
    bad = tool(client, key, "estimate_video", url="http://169.254.169.254/latest/meta-data")
    assert bad["ok"] is False and bad["reason"] == "source_not_allowed"
    job = tool(client, key, "create_clips", url=URL, clips=2, music=True)
    assert job["status"] == "queued" and job["credits_reserved"] == 5
    assert tool(client, key, "get_balance")["balance"] == 25
    worker().run_one()
    done = tool(client, key, "get_job", job_id=job["id"])
    assert done["status"] == "done" and len(done["clips"]) == 2
    clip_id = done["clips"][0]["id"]
    links = tool(client, key, "get_clip_links", clip_id=clip_id)
    path = links["download_url"].split("://", 1)[1].split("/", 1)[1]
    client.cookies.clear()
    assert client.get("/" + path).status_code == 200  # the signed link works with no key at all
    assert len(tool(client, key, "list_jobs")["jobs"]) == 1
    assert len(tool(client, key, "list_clips", job_id=job["id"])["clips"]) == 2
    pkg = tool(client, key, "get_publish_package", clip_id=clip_id, timezone="Europe/London")
    assert pkg["timezone"] == "Europe/London" and pkg["files"]["zip_url"].startswith("http")
    assert tool(client, key, "cancel_job", job_id=job["id"])["status"] == "done"


def test_two_users_interleaved_see_only_their_own(client, core, saas_db, sources):
    a = make_user(saas_db, "mcp-a@example.com", plan="studio", credits=30)
    b = make_user(saas_db, "mcp-b@example.com", plan="studio", credits=12)
    ka, kb = make_key(saas_db, a), make_key(saas_db, b)
    ja = tool(client, ka, "create_clips", url=URL)
    assert tool(client, kb, "get_balance")["balance"] == 12
    jb = tool(client, kb, "create_clips", url=URL)
    assert tool(client, ka, "get_balance")["balance"] == 25
    assert tool(client, kb, "get_job", job_id=ja["id"])["_error"]["error"] == "not_found"
    assert tool(client, ka, "cancel_job", job_id=jb["id"])["_error"]["error"] == "not_found"
    assert [j["id"] for j in tool(client, ka, "list_jobs")["jobs"]] == [ja["id"]]
    assert [j["id"] for j in tool(client, kb, "list_jobs")["jobs"]] == [jb["id"]]
    assert tool(client, kb, "get_balance")["balance"] == 7


def test_scopes_and_revocation(client, core, saas_db, sources):
    from saas.models.core import ApiKey

    u = make_user(saas_db, "mcp-scope@example.com", plan="studio", credits=30)
    read_only = make_key(saas_db, u, ["jobs:read", "clips:read"])
    out = tool(client, read_only, "create_clips", url=URL)
    assert out["_error"]["error"] == "insufficient_scope"
    assert tool(client, read_only, "get_balance")["_error"]["error"] == "insufficient_scope"
    assert tool(client, read_only, "list_jobs") == {"jobs": []}
    key_row = saas_db.query(ApiKey).one()
    from saas.common import utcnow

    key_row.revoked_at = utcnow()
    saas_db.commit()
    assert rpc(client, read_only, "tools/list").status_code == 401


def test_mcp_rate_limit_shared_with_key(client, core, saas_db, studio, monkeypatch):
    _u, key = studio
    monkeypatch.setattr(core, "api_rate_per_minute", 2)
    codes = [rpc(client, key, "tools/list").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
