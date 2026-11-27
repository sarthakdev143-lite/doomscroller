"""The clip editor API against the rerender seam (MockEngine stands in for app.clipper.standalone)."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from core_fixtures import core, csrf, login, make_user  # noqa: F401
from test_jobs_lifecycle import URL, sources, worker  # noqa: F401


def _gif() -> bytes:
    frames = [Image.new("RGB", (32, 32), (i * 80, 0, 0)) for i in range(2)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buf.getvalue()


@pytest.fixture()
def made(client, core, saas_db, sources):
    """User A (Pro, 20 credits) with a finished 2-clip job; returns (clip_id, headers, engine)."""
    from saas.jobs.engine import MockEngine

    make_user(saas_db, "editor@example.com", plan="pro", credits=20)
    h = login(client, "editor@example.com")
    r = client.post("/api/jobs", json={"source_url": URL, "options": {"clips": 2}}, headers=h)
    eng = MockEngine()
    worker(eng).run_one()
    clips = client.get(f"/api/jobs/{r.json()['id']}").json()["clips"]
    return clips[0]["id"], h, eng


def test_edit_view_shows_words_memes_logo_and_limits(client, core, saas_db, made):
    clip_id, h, _eng = made
    client.post("/api/brand/memes", files={"file": ("lol.gif", _gif(), "image/gif")}, headers=h)
    view = client.get(f"/api/clips/{clip_id}/edit").json()
    assert view["version"] == 1 and view["spec"] == {}
    assert [w["text"] for w in view["words"]] == ["this", "is", "the", "moment"]
    ids = [m["id"] for m in view["memes"]["available"]]
    assert ids[0] == "giphy-mock1" and ids[1].startswith("custom-") and len(ids[1]) == len("custom-") + 20
    assert "path" not in view["memes"]["available"][0]  # internal file paths never leave the server
    assert view["logo"] == {"available": False, "url": None}
    assert view["limits"]["free_left"] == core.rerender_free_per_clip and view["limits"]["max_memes"] == 10


def test_rerender_makes_a_new_version_and_keeps_the_old(client, core, saas_db, made):
    from saas.jobs import storage
    from saas.models.jobs import Clip

    clip_id, h, eng = made
    client.post("/api/brand/logo", files={"file": ("logo.png", _png(), "image/png")}, headers=h)
    old_path = saas_db.get(Clip, clip_id).path
    spec = {"hook": {"text": "New hook"}, "trim": {"start_s": 1.0, "end_s": 20.0},
            "memes": [{"id": "giphy-mock1", "start_s": 3.0}]}
    r = client.post(f"/api/clips/{clip_id}/render", json={"spec": spec}, headers=h)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["kind"] == "rerender" and job["clip_id"] == clip_id and job["funding"] == "included"
    assert client.post(f"/api/clips/{clip_id}/render", json={"spec": {}}, headers=h).json()["error"] == \
        "rerender_in_progress"
    w = worker(eng)
    assert w.run_one() == job["id"]
    assert eng.rerenders[0]["spec"] == spec and eng.rerenders[0]["logo_path"].endswith(".png")
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "done"
    saas_db.expire_all()
    clip = saas_db.get(Clip, clip_id)
    assert clip.version == 2 and clip.path.endswith("_v2.mp4") and clip.duration_s == 19.0 and clip.spec == spec
    vs = client.get(f"/api/clips/{clip_id}/versions").json()
    assert [v["version"] for v in vs["versions"]] == [2, 1] and vs["versions"][0]["current"] is True
    assert storage.resolve(old_path).is_file()
    old = client.get(f"/api/clips/{clip_id}/versions/1/video")
    new = client.get(f"/api/clips/{clip_id}/video")
    assert old.status_code == new.status_code == 200 and old.content != new.content
    assert client.get(f"/api/clips/{clip_id}/edit").json()["spec"] == spec
    assert client.get("/api/jobs").json()["jobs"][0]["kind"] == "clip"  # re-renders don't clutter the job list


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (0, 200, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_free_rerenders_then_credits(client, core, saas_db, made, monkeypatch):
    from saas.billing import ledger
    from saas.jobs.engine import EngineError, MockEngine
    from saas.models.core import User

    clip_id, h, eng = made
    monkeypatch.setattr(core, "rerender_free_per_clip", 1)
    monkeypatch.setattr(core, "rerender_credit_cost", 2)
    user = saas_db.query(User).filter_by(email="editor@example.com").one()
    start = ledger.balance(saas_db, user.id)
    # A failed re-render (spec the engine refuses) is free and doesn't use up the included one.
    bad = worker(MockEngine(rerender_fail_with=EngineError("invalid_spec", "memes[0].id must be one of the clip's memes")))
    client.post(f"/api/clips/{clip_id}/render", json={"spec": {"memes": [{"id": "nope"}]}}, headers=h)
    bad.run_one()
    failed = client.get("/api/jobs?status=failed").json()["jobs"]
    assert failed == []  # rerender jobs aren't in the default (clip) list
    assert client.get(f"/api/clips/{clip_id}/edit").json()["limits"]["free_left"] == 1
    r = client.post(f"/api/clips/{clip_id}/render", json={"spec": {}}, headers=h)
    assert r.json()["funding"] == "included" and r.json()["credits_reserved"] == 0
    worker(eng).run_one()
    r = client.post(f"/api/clips/{clip_id}/render", json={"spec": {"sfx": {"enabled": False}}}, headers=h)
    assert r.json()["funding"] == "credits" and r.json()["credits_reserved"] == 2
    worker(eng).run_one()
    saas_db.expire_all()
    assert ledger.balance(saas_db, user.id) == start - 2
    user.plan_slug = "pro"
    ledger.debit(saas_db, user.id, ledger.balance(saas_db, user.id), kind="admin_adjust", idempotency_key="zero")
    saas_db.commit()
    r = client.post(f"/api/clips/{clip_id}/render", json={"spec": {}}, headers=h)
    assert r.status_code == 402 and r.json()["error"] == "insufficient_credits"


def test_invalid_specs_are_refused_early(client, core, saas_db, made):
    clip_id, h, _eng = made
    assert client.post(f"/api/clips/{clip_id}/render", json={"spec": {"rm -rf": 1}}, headers=h).json()["error"] == \
        "invalid_spec"
    assert client.post(f"/api/clips/{clip_id}/render", json={"spec": "x"}, headers=h).status_code == 422
    big = {"captions": {"edits": [{"i": 0, "text": "x" * 40}] * 2000}}
    assert client.post(f"/api/clips/{clip_id}/render", json={"spec": big}, headers=h).status_code == 413


def test_editor_has_no_idor(client, core, saas_db, made):
    clip_id, _h, _eng = made
    make_user(saas_db, "nosy@example.com", plan="pro", credits=20)
    h2 = login(client, "nosy@example.com")
    checks = [client.get(f"/api/clips/{clip_id}/edit"),
              client.post(f"/api/clips/{clip_id}/render", json={"spec": {}}, headers=h2),
              client.get(f"/api/clips/{clip_id}/versions"),
              client.get(f"/api/clips/{clip_id}/versions/1/video")]
    assert [r.status_code for r in checks] == [404, 404, 404, 404]


def test_delete_removes_versions_and_bundle(client, core, saas_db, made):
    from saas.jobs import storage
    from saas.models.jobs import Clip

    clip_id, h, eng = made
    client.post(f"/api/clips/{clip_id}/render", json={"spec": {}}, headers=h)
    worker(eng).run_one()
    saas_db.expire_all()
    clip = saas_db.get(Clip, clip_id)
    manifest = storage.resolve(clip.manifest_path)
    files = [manifest, storage.resolve(clip.source_path), storage.resolve(clip.path),
             manifest.with_name(manifest.name.replace("_manifest.json", "_assets"))]
    assert all(p.exists() for p in files)
    assert client.delete(f"/api/clips/{clip_id}", headers=h).status_code == 200
    assert not any(p.exists() for p in files)


def test_clips_without_a_manifest_are_not_editable(client, core, saas_db, made):
    from saas.models.jobs import Clip

    clip_id, h, _eng = made
    saas_db.get(Clip, clip_id).manifest_path = None
    saas_db.commit()
    assert client.get(f"/api/clips/{clip_id}/edit").json()["error"] == "not_editable"
    assert client.post(f"/api/clips/{clip_id}/render", json={"spec": {}}, headers=h).status_code == 409


def test_limits_mirror_the_pipeline():
    editor = pytest.importorskip("app.clipper.editor")
    from saas.jobs.editor import SPEC_KEYS, SPEC_LIMITS

    assert SPEC_KEYS == set(editor._SPEC_SHAPE)
    assert SPEC_LIMITS["max_memes"] == editor.MAX_MEMES and SPEC_LIMITS["max_caption_chars"] == editor.MAX_CAPTION_CHARS
    assert SPEC_LIMITS["max_hook_chars"] == editor.MAX_HOOK_CHARS
    assert SPEC_LIMITS["max_watermark_chars"] == editor.MAX_WATERMARK_CHARS
    assert SPEC_LIMITS["min_clip_s"] == editor.MIN_CLIP_SEC
    assert tuple(SPEC_LIMITS["meme_duration_s"]) == editor._MEME_SEC_RANGE
    assert tuple(SPEC_LIMITS["logo_scale"]) == editor._LOGO_SCALE_RANGE
    assert tuple(SPEC_LIMITS["music_volume_db"]) == editor._MUSIC_DB_RANGE
    csrf  # noqa: B018
