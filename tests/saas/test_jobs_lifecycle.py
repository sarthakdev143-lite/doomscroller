"""Jobs end to end with MockEngine: reserve -> worker -> settle/release, free tier, cancel, stuck-job
recovery, IDOR on every job/clip route, ranged streaming, signed links, publish packages, engine seam."""
from __future__ import annotations

import io
import sys
import types
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

from core_fixtures import core, csrf, login, make_key, make_user  # noqa: F401

URL = "https://www.youtube.com/watch?v=abc123"


@pytest.fixture()
def sources(monkeypatch, core):
    """Public DNS and a stubbed yt-dlp; `meta['duration_s']` sets the source length."""
    from saas.jobs import probe, ssrf

    answers: dict = {}
    monkeypatch.setattr(ssrf, "resolve_host", lambda host, port: answers.get(host, ["142.250.190.78"]))
    meta = {"title": "Talk", "duration_s": 300.0, "thumbnail": None, "webpage_url": URL, "extractor": "Youtube",
            "uploader": "x"}
    monkeypatch.setattr(probe, "ytdlp_metadata", lambda url: (ssrf.validate_url(url), dict(meta))[1])
    monkeypatch.setattr(core, "job_heartbeat_s", 3600)
    return {"meta": meta, "dns": answers}


def worker(engine=None):
    from saas.jobs.engine import MockEngine
    from saas.jobs.worker import Worker

    return Worker(engine=engine or MockEngine())


def _create(client, h, **opts):
    r = client.post("/api/jobs", json={"source_url": URL, "options": {"clips": 3, **opts}}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def test_job_runs_and_settles(client, core, saas_db, sources):
    from saas.billing import ledger
    from saas.jobs.engine import MockEngine

    u = make_user(saas_db, "run@example.com", plan="pro", credits=20)
    h = login(client, "run@example.com")
    job = _create(client, h)
    assert job["status"] == "queued" and job["credits_reserved"] == 5 and job["funding"] == "credits"
    assert ledger.balance(saas_db, u.id) == 15
    eng = MockEngine()
    assert worker(eng).run_one() == job["id"]
    body = client.get(f"/api/jobs/{job['id']}").json()
    assert body["status"] == "done" and body["progress"] == 1.0 and body["credits_charged"] == 5
    assert len(body["clips"]) == 3 and body["clips"][0]["video_url"].endswith("/video")
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 15
    audit = ledger.audit(saas_db, u.id)
    assert audit["ledger_sum"] == audit["balance"]
    assert eng.calls[0]["watermark_text"] is None  # paid plan: no watermark
    assert worker(eng).run_one() is None


def test_shorter_actual_source_charges_less(client, core, saas_db, sources):
    from saas.billing import ledger
    from saas.jobs.engine import MockEngine

    u = make_user(saas_db, "short@example.com", plan="pro", credits=20)
    h = login(client, "short@example.com")
    _create(client, h)
    worker(MockEngine(source_duration_s=125)).run_one()
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 17  # 3 minutes charged, 2 of the 5 reserved returned


def test_free_tier_ends_at_whichever_meter_runs_out(client, core, saas_db, sources):
    from saas.billing import ledger
    from saas.jobs.engine import MockEngine

    u = make_user(saas_db, "free@example.com")
    ledger.grant_free(saas_db, u.id, 10, 10, idempotency_key="free-test")
    saas_db.commit()
    h = login(client, "free@example.com")
    sources["meta"]["duration_s"] = 240.0  # 4 minutes
    job = _create(client, h, clips=4)
    assert job["funding"] == "free" and job["watermark"] is True
    eng = MockEngine()
    worker(eng).run_one()
    assert eng.calls[0]["watermark_text"]  # free output is watermarked
    saas_db.expire_all()
    assert ledger.free_balance(saas_db, u.id) == (6, 6)
    _create(client, h, clips=5)
    worker(eng).run_one()
    saas_db.expire_all()
    assert ledger.free_balance(saas_db, u.id) == (2, 1)  # shorts ran out first
    r = client.post("/api/jobs", json={"source_url": URL, "options": {"clips": 2}}, headers=h)
    assert r.status_code == 402 and r.json()["error"] == "insufficient_credits" and r.json()["free_shorts"] == 1
    assert ledger.balance(saas_db, u.id) == 0


def test_fewer_clips_than_asked_are_explained(client, core, saas_db, sources):
    from saas.jobs.engine import MockEngine

    class OneGoodMoment(MockEngine):
        def run(self, **kw):
            out = super().run(**kw)
            out["clips"] = out["clips"][:1]
            out["warnings"] = ["clip 2 failed: no strong moment"]
            return out

    make_user(saas_db, "fewer@example.com", plan="pro", credits=20)
    h = login(client, "fewer@example.com")
    job = _create(client, h, clips=2)
    worker(OneGoodMoment()).run_one()
    body = client.get(f"/api/jobs/{job['id']}").json()
    assert body["status"] == "done" and len(body["clips"]) == 1
    assert body["notes"][0].startswith("Only 1 of the 2 clips") and "clip 2 failed: no strong moment" in body["notes"]


def test_engine_failure_releases_credits(client, core, saas_db, sources):
    from saas.billing import ledger
    from saas.jobs.engine import EngineError, MockEngine

    u = make_user(saas_db, "fail@example.com", plan="pro", credits=10)
    h = login(client, "fail@example.com")
    job = _create(client, h)
    worker(MockEngine(fail_with=EngineError("no_speech", "We couldn't find usable moments."))).run_one()
    body = client.get(f"/api/jobs/{job['id']}").json()
    assert body["status"] == "failed" and body["error"]["code"] == "no_speech" and body["credits_charged"] == 0
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 10


def test_cancel_queued_and_running(client, core, saas_db, sources):
    from saas.billing import ledger

    u = make_user(saas_db, "cancel@example.com", plan="pro", credits=20)
    h = login(client, "cancel@example.com")
    job = _create(client, h)
    r = client.post(f"/api/jobs/{job['id']}/cancel", headers=h)
    assert r.json()["status"] == "canceled"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 20
    job2 = _create(client, h)
    w = worker()
    assert w.claim() == job2["id"]
    assert client.post(f"/api/jobs/{job2['id']}/cancel", headers=h).json()["cancel_requested"] is True
    w.process(job2["id"])
    assert client.get(f"/api/jobs/{job2['id']}").json()["status"] == "canceled"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 20


def test_stuck_jobs_are_requeued_then_failed_once(client, core, saas_db, sources):
    from saas.billing import ledger
    from saas.common import utcnow
    from saas.models.jobs import Clip, Job

    u = make_user(saas_db, "stuck@example.com", plan="pro", credits=10)
    h = login(client, "stuck@example.com")
    job = _create(client, h)
    w = worker()
    for expected in ("queued", "failed"):
        assert w.claim() == job["id"]
        saas_db.expire_all()
        row = saas_db.get(Job, job["id"])
        row.heartbeat_at = utcnow() - timedelta(minutes=core.job_stuck_minutes + 1)
        saas_db.commit()
        w.recover_stuck()
        saas_db.expire_all()
        assert saas_db.get(Job, job["id"]).status == expected
    assert ledger.balance(saas_db, u.id) == 10
    # The "lost" worker finishing late must not resurrect the job or charge anyone.
    w._succeed(job["id"], {"clips": [{"path": "x"}], "source": {}}, Path("."))
    saas_db.expire_all()
    assert saas_db.get(Job, job["id"]).status == "failed"
    assert saas_db.query(Clip).count() == 0 and ledger.balance(saas_db, u.id) == 10


def test_claims_are_exclusive(client, core, saas_db, sources):
    make_user(saas_db, "claim@example.com", plan="pro", credits=10)
    h = login(client, "claim@example.com")
    job = _create(client, h)
    a, b = worker(), worker()
    assert a.claim() == job["id"] and b.claim() is None


def test_worker_revalidates_source_dns(client, core, saas_db, sources):
    from saas.billing import ledger

    u = make_user(saas_db, "dns@example.com", plan="pro", credits=10)
    h = login(client, "dns@example.com")
    job = _create(client, h)
    sources["dns"]["www.youtube.com"] = ["10.0.0.7"]  # rebinding between creation and fetch
    worker().run_one()
    body = client.get(f"/api/jobs/{job['id']}").json()
    assert body["status"] == "failed" and body["error"]["code"] == "source_not_allowed"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 10


def test_engine_output_outside_job_folder_is_ignored(client, core, saas_db, sources, tmp_path):
    from saas.billing import ledger
    from saas.jobs.engine import MockEngine

    class Escaping(MockEngine):
        def run(self, **kw):
            out = super().run(**kw)
            outside = tmp_path / "secret.mp4"
            outside.write_bytes(b"x")
            for c in out["clips"]:
                c["path"] = str(outside)
            return out

    u = make_user(saas_db, "escape@example.com", plan="pro", credits=10)
    h = login(client, "escape@example.com")
    job = _create(client, h)
    worker(Escaping()).run_one()
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "failed"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 10


def test_plan_caps(client, core, saas_db, sources, monkeypatch):
    make_user(saas_db, "caps@example.com", credits=100, paid=True)  # pack buyer: concurrency 1
    h = login(client, "caps@example.com")
    _create(client, h)
    r = client.post("/api/jobs", json={"source_url": URL}, headers=h)
    assert r.status_code == 429 and r.json()["error"] == "too_many_active_jobs"
    r = client.post("/api/jobs", json={"source_url": URL, "options": {"clips": 50}}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "invalid_options"
    r = client.post("/api/jobs", json={"source_url": URL, "options": {"bogus": 1}}, headers=h)
    assert r.json()["error"] == "invalid_options"
    make_user(saas_db, "broke@example.com", plan="pro", credits=1)
    h = login(client, "broke@example.com")
    r = client.post("/api/jobs", json={"source_url": URL}, headers=h)
    assert r.status_code == 402 and r.json()["error"] == "insufficient_credits"


# ---- read paths: IDOR, streaming, links, packages ---------------------------------------------------

@pytest.fixture()
def finished(client, core, saas_db, sources):
    """User A with one finished job of 3 clips; returns (job_id, clip ids)."""
    make_user(saas_db, "owner@example.com", plan="studio", credits=50)
    h = login(client, "owner@example.com")
    job = _create(client, h)
    worker().run_one()
    clips = client.get(f"/api/jobs/{job['id']}").json()["clips"]
    return job["id"], [c["id"] for c in clips]


def test_no_idor_on_jobs_and_clips(client, core, saas_db, finished):
    job_id, clip_ids = finished
    make_user(saas_db, "intruder@example.com", plan="studio", credits=50)
    h = login(client, "intruder@example.com")
    cid = clip_ids[0]
    checks = [
        client.get(f"/api/jobs/{job_id}"),
        client.post(f"/api/jobs/{job_id}/cancel", headers=h),
        client.get(f"/api/clips/{cid}"),
        client.patch(f"/api/clips/{cid}", json={"title": "pwned"}, headers=h),
        client.delete(f"/api/clips/{cid}", headers=h),
        client.get(f"/api/clips/{cid}/video"),
        client.get(f"/api/clips/{cid}/download"),
        client.get(f"/api/clips/{cid}/thumbnail"),
        client.get(f"/api/clips/{cid}/package"),
        client.get(f"/api/clips/{cid}/package.zip"),
    ]
    assert [r.status_code for r in checks] == [404] * len(checks)
    assert client.get("/api/jobs").json()["jobs"] == []
    assert client.get(f"/api/clips?job_id={job_id}").json()["clips"] == []
    from saas.models.core import User

    key = make_key(saas_db, saas_db.query(User).filter_by(email="intruder@example.com").one())
    auth = {"Authorization": f"Bearer {key}"}
    assert client.get(f"/api/v1/jobs/{job_id}", headers=auth).status_code == 404
    assert client.get(f"/api/v1/clips/{cid}/links", headers=auth).status_code == 404
    login(client, "owner@example.com")
    assert client.get(f"/api/clips/{cid}").json()["title"] != "pwned"


def test_ranged_video_streaming(client, core, saas_db, finished):
    _job, clip_ids = finished
    full = client.get(f"/api/clips/{clip_ids[0]}/video")
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    assert full.headers.get("accept-ranges") == "bytes"
    part = client.get(f"/api/clips/{clip_ids[0]}/video", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10
    assert part.headers["content-range"] == f"bytes 0-9/{len(full.content)}"
    assert part.content == full.content[:10]
    dl = client.get(f"/api/clips/{clip_ids[0]}/download")
    assert "attachment" in dl.headers["content-disposition"]


def test_signed_links(client, core, saas_db, finished):
    from saas.auth.crypto import sign
    from saas.jobs import storage

    _job, clip_ids = finished
    url, exp = storage.signed_file_url(clip_ids[0], "video")
    path = url.split("://", 1)[1].split("/", 1)[1]
    client.cookies.clear()
    assert client.get("/" + path).status_code == 200
    assert client.get("/" + path.replace("sig=", "sig=x")).status_code == 403
    assert client.get("/" + path.replace("/video?", "/thumbnail?")).status_code == 403  # sig binds the kind
    old = exp - 10_000
    stale = f"/files/clips/{clip_ids[0]}/video?exp={old}&sig={sign('clip-file', f'{clip_ids[0]}:video:{old}')}"
    assert client.get(stale).status_code == 403
    zurl, _ = storage.signed_file_url(clip_ids[0], "package")
    r = client.get("/" + zurl.split("://", 1)[1].split("/", 1)[1])
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"


def test_publish_package_json_and_zip(client, core, saas_db, finished):
    _job, clip_ids = finished
    pkg = client.get(f"/api/clips/{clip_ids[1]}/package?tz=Asia/Kolkata").json()
    assert pkg["timezone"] == "Asia/Kolkata" and pkg["schedule_model"] == "rules-v1"
    assert set(pkg["platforms"]) == {"youtube_shorts", "instagram_reels", "tiktok"}
    yt = pkg["platforms"]["youtube_shorts"]
    assert len(yt["title"]) <= 100 and "#Shorts" in yt["description"]
    times = yt["suggested_times"]
    assert len(times) == 3 and all(t["local"].endswith("+05:30") for t in times)
    assert times == sorted(times, key=lambda t: t["utc"])
    assert client.get(f"/api/clips/{clip_ids[1]}/package?tz=Not/AZone").json()["timezone"] == "UTC"
    h = csrf(client)
    client.patch("/api/me", json={"timezone": "America/New_York"}, headers=h)
    assert client.get(f"/api/clips/{clip_ids[1]}/package").json()["timezone"] == "America/New_York"
    assert client.patch("/api/me", json={"timezone": "Mars/Base"}, headers=h).json()["error"] == "invalid_timezone"
    z = client.get(f"/api/clips/{clip_ids[1]}/package.zip")
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert {"package.json", "youtube_shorts.txt", "instagram_reels.txt", "tiktok.txt", "README.txt"} <= set(names)
    assert any(n.endswith(".mp4") for n in names) and any(n.endswith("_thumbnail.jpg") for n in names)


def test_schedule_staggers_clips_of_one_job():
    from datetime import datetime, timezone

    from saas.jobs import schedule

    now = datetime(2026, 11, 27, 6, 0, tzinfo=timezone.utc)  # Friday
    first = schedule.suggest("Asia/Kolkata", now=now, offset=0)["platforms"]["tiktok"]["times"]
    second = schedule.suggest("Asia/Kolkata", now=now, offset=1)["platforms"]["tiktok"]["times"]
    assert first[1] == second[0]  # clip 2 starts one slot later
    assert first[0]["local"].startswith("2026-11-27T12:00")  # 11:30 IST now + 30 min lead -> next is noon


def test_clip_edits_are_sanitised(client, core, saas_db, finished):
    _job, clip_ids = finished
    h = csrf(client)
    r = client.patch(f"/api/clips/{clip_ids[0]}", json={"title": "x" * 150, "hashtags": ["fun", "#ok", "<b>bad</b>"]},
                     headers=h)
    body = r.json()
    assert len(body["title"]) == 100 and body["hashtags"] == ["#fun", "#ok", "#bbadb"]


# ---- the real engine seam ---------------------------------------------------------------------------

def test_clipper_engine_calls_the_pipeline_seam(core, tmp_path, monkeypatch):
    import app.clipper as clipper_pkg
    from app.clipper import metering

    from saas.jobs.engine import ClipperEngine, EngineError

    seen = {}

    class ClipError(RuntimeError):
        def __init__(self, code, message):
            super().__init__(message)
            self.code, self.message = code, message

    def clip_to_directory(source, out_dir, **kw):
        seen.update(source=source, out_dir=out_dir, **kw)
        metering.record("stt", provider="deepgram", model="nova-3", audio_seconds=120)
        if source == "boom":
            raise ClipError("no_speech", "Nothing to cut.")
        kw["on_stage"]("rendering", 0.5)
        return {"source": {"title": "t", "duration_s": 61}, "clips": []}

    fake = types.ModuleType("app.clipper.standalone")
    fake.clip_to_directory = clip_to_directory
    fake.ClipError = ClipError
    monkeypatch.setitem(sys.modules, "app.clipper.standalone", fake)
    monkeypatch.setattr(clipper_pkg, "standalone", fake, raising=False)
    stages = []
    out = ClipperEngine().run(source="/x/in.mp4", out_dir=tmp_path, options={"clips": 2, "music": True},
                              brand_name=None, watermark_text="Brand", recent_memes=["m1"],
                              on_stage=lambda s, p: stages.append(s), logo_path="/logo.png", custom_memes=["/a.gif"])
    assert seen["count"] == 2 and seen["music"] is True and seen["watermark_text"] == "Brand"
    assert seen["logo_path"] == "/logo.png" and seen["custom_memes"] == ("/a.gif",) and seen["recent_memes"] == ("m1",)
    assert seen["cache_dir"] == tmp_path.parent / "work" and stages == ["rendering"]
    assert out["meter"]["stt_seconds"] == {"deepgram:nova-3": 120.0}
    with pytest.raises(EngineError) as exc:
        ClipperEngine().run(source="boom", out_dir=tmp_path, options={}, brand_name=None, watermark_text=None,
                            recent_memes=(), on_stage=lambda s, p: None)
    assert exc.value.code == "no_speech"
