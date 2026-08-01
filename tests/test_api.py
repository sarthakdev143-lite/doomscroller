"""Tests for the FastAPI endpoints using TestClient."""
from fastapi.testclient import TestClient

from app.db import init_db, session_scope
from app.models import Video, VideoStatus
from app.main import app


def setup_module(module):
    init_db()


def _client() -> TestClient:
    """TestClient wrapped in lifespan context so startup event fires."""
    client = TestClient(app)
    client.__enter__()
    return client


def test_health():
    client = _client()
    try:
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["use_mock"] is True
    finally:
        client.__exit__(None, None, None)


def test_pending_list():
    client = _client()
    try:
        # Seed directly so the in-memory DB has something
        with session_scope() as db:
            db.add(Video(
                source="tiktok", source_id="p1", source_url="http://x",
                author="@a", title="T", description="d",
                views=1000, likes=10, comments=1, shares=2, duration_sec=10,
                status=VideoStatus.PENDING,
            ))
        r = client.get("/videos/pending")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
    finally:
        client.__exit__(None, None, None)


def test_approve_flow():
    client = _client()
    try:
        with session_scope() as db:
            v = Video(
                source="tiktok", source_id="appr1", source_url="http://x",
                author="@a", title="T", description="d",
                views=1000, likes=10, comments=1, shares=2, duration_sec=10,
                status=VideoStatus.PENDING,
            )
            db.add(v)
            db.flush()
            vid = v.id
        r = client.post(f"/videos/{vid}/approve", json={"approved": True})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"
    finally:
        client.__exit__(None, None, None)
