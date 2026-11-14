"""TDD suite for Wave 12 clip queue helpers (job.py) and clip.py --inbox/--batch/--retry/--clear-finished."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text

from app.clipper import job as clip_job_mod
from app.clipper.ingest import file_fingerprint
from app.config import settings
from app.db import init_db, session_scope
from app.models import Channel, ClipJob
from scripts import clip as clip_cli


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    init_db()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(settings, "growth_inbox_dir", str(inbox))
    # No channels.json, so the legacy "default" channel is always seeded, whatever earlier
    # tests left in the shared in-memory database.
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "no-channels.json"))
    from app.growth import channels

    with session_scope() as db:
        db.execute(delete(ClipJob))
        db.execute(delete(Channel).where(Channel.slug == "tech"))
        channels.sync_channels(db)
    yield
    with session_scope() as db:
        db.execute(delete(ClipJob))
        db.execute(delete(Channel).where(Channel.slug == "tech"))


def _inbox() -> Path:
    return Path(settings.growth_inbox_dir)


def _put(name: str, data: bytes | None = None) -> Path:
    path = _inbox() / name
    path.write_bytes(data if data is not None else f"video-bytes-{name}".encode())
    return path


def _tech_channel(require_approval: bool = True) -> str:
    from app.growth import channels

    with session_scope() as db:
        channels._ensure_seeded(db)
        ch = Channel(slug="tech", name="Tech", niche="tech", brand_name="@tech", uploads_per_day=3,
                     timezone="UTC", require_approval=require_approval, enabled=True)
        db.add(ch)
        db.flush()
        return ch.id


def _jobs() -> list[ClipJob]:
    with session_scope() as db:
        rows = db.execute(select(ClipJob).order_by(ClipJob.created_at, text("rowid"), ClipJob.id)).scalars().all()
        for row in rows:
            db.expunge(row)
        return list(rows)


def _fake_result(source, ok=True, **extra):
    base = {"ok": ok, "source": source, "kind": "speech", "queued": False,
            "clips": [{"id": "c", "path": "x.mp4", "title": "T", "duration": 20.0, "score": 0.5}] if ok else [],
            "warnings": []}
    if not ok:
        base["error"] = "boom"
    base.update(extra)
    return base


# ---------- job.py ----------


def test_clear_finished_jobs_dismisses_terminal_only_case_insensitively():
    with session_scope() as db:
        for status in ("done", "succeeded", "failed", "cancelled", "CANCELED", "queued", "running"):
            db.add(ClipJob(source=f"https://example.com/{status}", status=status))
    out = clip_job_mod.clear_finished_jobs()
    assert out["cleared"] == 5
    assert len(out["ids"]) == 5
    left = sorted(j.status for j in _jobs() if not (j.result or {}).get("dismissed"))
    assert left == ["queued", "running"]
    assert clip_job_mod.clear_finished_jobs() == {"cleared": 0, "ids": []}


def test_scan_inbox_sources_filters_extensions_partials_and_known_jobs():
    a = _put("a.mp4")
    b = _put("b.MOV")
    _put("notes.txt")
    _put("c.mp4.part")
    _put(".hidden.mp4")
    (_inbox() / "folder.mp4").mkdir()
    e = _put("e.mkv", b"unique-e-bytes")
    f = _put("f.webm")

    found = clip_job_mod.scan_inbox_sources()
    assert [Path(p).name for p in found] == ["a.mp4", "b.MOV", "e.mkv", "f.webm"]
    assert all(Path(p).is_absolute() for p in found)

    # A queued job for a.mp4 (by path) and a finished job for e.mkv's bytes (renamed copy) hide both.
    with session_scope() as db:
        db.add(ClipJob(source=str(a.resolve()), status="queued"))
        db.add(ClipJob(source="C:/elsewhere/old-name.mp4", status="done",
                       result={"fingerprint": file_fingerprint(e)}))
    found2 = clip_job_mod.scan_inbox_sources()
    assert [Path(p).name for p in found2] == ["b.MOV", "f.webm"]
    assert b.exists() and f.exists()


def test_scan_inbox_sources_missing_inbox_and_channel_scope(monkeypatch, tmp_path):
    real_inbox = settings.growth_inbox_dir
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path / "nope"))
    assert clip_job_mod.scan_inbox_sources() == []
    monkeypatch.setattr(settings, "growth_inbox_dir", real_inbox)

    a = _put("a.mp4")
    tech_id = _tech_channel()
    with session_scope() as db:
        db.add(ClipJob(source=str(a.resolve()), status="done", channel_id=tech_id))

    assert clip_job_mod.scan_inbox_sources() == []  # any channel's job counts globally
    assert [Path(p).name for p in clip_job_mod.scan_inbox_sources("tech")] == []
    assert [Path(p).name for p in clip_job_mod.scan_inbox_sources("default")] == ["a.mp4"]
    with pytest.raises(ValueError):
        clip_job_mod.scan_inbox_sources("no-such-channel")


# ---------- scripts/clip.py ----------


def test_cli_inbox_enqueue_creates_jobs_routes_channel_and_is_idempotent(capsys):
    tech_id = _tech_channel(require_approval=True)
    _put("one.mp4")
    _put("two.mov")

    rc = clip_cli.main(["--inbox", "--enqueue", "--channel", "tech", "--count", "2", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True
    assert payload["mode"] == "enqueue"
    assert [Path(item["source"]).name for item in payload["enqueued"]] == ["one.mp4", "two.mov"]
    rows = _jobs()
    assert len(rows) == 2
    assert all(r.channel_id == tech_id and r.count == 2 and r.auto_queue is False for r in rows)

    rc2 = clip_cli.main(["--inbox", "--enqueue", "--channel", "tech", "--json"])
    again = json.loads(capsys.readouterr().out)
    assert rc2 == 0 and again["ok"] is True and again["enqueued"] == []
    assert len(_jobs()) == 2

    capsys.readouterr()
    rc3 = clip_cli.main(["--inbox", "--enqueue"])
    assert rc3 == 0
    assert "nothing new" in capsys.readouterr().out.lower()


def test_cli_inbox_foreground_clips_sequentially_continues_after_failure_and_keeps_json_pure(monkeypatch, capsys):
    _put("a.mp4")
    _put("b.mp4")
    calls: list[str] = []

    def fake_clip_source(source, **kwargs):
        calls.append(Path(source).name)
        if kwargs.get("progress"):
            kwargs["progress"]("downloading")
        return _fake_result(source, ok=not source.endswith("a.mp4"))

    monkeypatch.setattr(clip_cli, "clip_source", fake_clip_source)
    rc = clip_cli.main(["--inbox", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert calls == ["a.mp4", "b.mp4"]
    assert rc == 1
    assert payload["ok"] is False
    assert payload["total"] == 2 and payload["succeeded"] == 1 and payload["failed"] == 1
    assert [r["ok"] for r in payload["results"]] == [False, True]
    assert "downloading" in captured.err
    assert _jobs() == []  # foreground mode never touches the queue


def test_cli_batch_parses_comments_blanks_bom_duplicates_and_enqueues(tmp_path, capsys):
    batch = tmp_path / "list.txt"
    batch.write_bytes(
        "\ufeff# my sources\r\nhttps://youtu.be/aaa\r\n\r\n   \r\n  # indented comment\r\n"
        "https://youtu.be/bbb#t=10\r\nhttps://youtu.be/aaa\r\n".encode("utf-8")
    )
    rc = clip_cli.main(["--batch", str(batch), "--enqueue", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True
    assert [i["source"] for i in payload["enqueued"]] == ["https://youtu.be/aaa", "https://youtu.be/bbb#t=10"]
    assert [j.source for j in _jobs()] == ["https://youtu.be/aaa", "https://youtu.be/bbb#t=10"]


def test_cli_batch_foreground_and_input_validation(monkeypatch, tmp_path, capsys):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(clip_cli, "clip_source",
                        lambda source, **kw: seen.append((source, kw)) or _fake_result(source))
    batch = tmp_path / "list.txt"
    batch.write_text("https://youtu.be/x1\n# skip\nhttps://youtu.be/x2\n", encoding="utf-8")

    rc = clip_cli.main(["--batch", str(batch), "--count", "2", "--queue", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["total"] == 2 and payload["failed"] == 0
    assert [s for s, _ in seen] == ["https://youtu.be/x1", "https://youtu.be/x2"]
    assert all(kw["count"] == 2 and kw["auto_queue"] is True for _, kw in seen)

    # Missing file, empty file, and directory give clean errors (no traceback), JSON or human.
    assert clip_cli.main(["--batch", str(tmp_path / "missing.txt"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    empty = tmp_path / "empty.txt"
    empty.write_text("# only comments\n\n", encoding="utf-8")
    assert clip_cli.main(["--batch", str(empty)]) == 1
    assert "no sources" in capsys.readouterr().err.lower()
    assert clip_cli.main(["--batch", str(tmp_path)]) == 1
    capsys.readouterr()
    assert len(seen) == 2  # nothing clipped for the bad inputs


def test_cli_retry_accepts_full_id_or_prefix_and_rejects_bad_targets(capsys):
    with session_scope() as db:
        failed = ClipJob(source="https://youtu.be/f", status="failed", attempts=3, error="stopped")
        done = ClipJob(source="https://youtu.be/d", status="done")
        db.add_all([failed, done])
        db.flush()
        failed_id, done_id = failed.id, done.id

    rc = clip_cli.main(["--retry", failed_id[:8], "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True and payload["job"]["id"] == failed_id
    row = next(j for j in _jobs() if j.id == failed_id)
    assert row.status == "queued" and row.attempts == 0 and row.error is None

    assert clip_cli.main(["--retry", done_id]) == 1  # cannot retry a done job
    assert "cannot retry" in capsys.readouterr().err
    assert clip_cli.main(["--retry", "deadbeefdeadbeef"]) == 1
    assert clip_cli.main(["--retry", "   ", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert clip_cli.main(["--retry", "%"]) == 1  # LIKE wildcards are never expanded
    capsys.readouterr()


def test_cli_clear_finished_and_conflicting_actions(capsys):
    with session_scope() as db:
        db.add_all([ClipJob(source="s1", status="done"), ClipJob(source="s2", status="failed"),
                    ClipJob(source="s3", status="queued"), ClipJob(source="s4", status="running")])
    rc = clip_cli.main(["--clear-finished", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True and payload["cleared"] == 2
    assert sorted(j.status for j in _jobs() if not (j.result or {}).get("dismissed")) == ["queued", "running"]

    assert clip_cli.main(["--clear-finished"]) == 0
    assert "0" in capsys.readouterr().out

    for argv in (["--inbox", "--clear-finished"], ["https://youtu.be/x", "--inbox"],
                 ["--jobs", "--retry", "abc"], ["https://youtu.be/x", "--batch", "f.txt"]):
        assert clip_cli.main([*argv, "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False


# ---------- soft dismiss: history stays, the list does not show it ----------


def test_clearing_finished_jobs_keeps_history_for_inbox_scan_but_hides_them_everywhere(capsys):
    from fastapi.testclient import TestClient

    from app.main import app

    x = _put("x.mp4")
    with session_scope() as db:
        done = ClipJob(source=str(x.resolve()), status="done", result={"fingerprint": file_fingerprint(x)})
        failed = ClipJob(source="https://youtu.be/gone", status="failed", attempts=3, error="boom")
        queued = ClipJob(source="https://youtu.be/waiting", status="queued")
        db.add_all([done, failed, queued])
        db.flush()
        done_id, failed_id, queued_id = done.id, failed.id, queued.id

    assert clip_job_mod.clear_finished_jobs()["cleared"] == 2

    # The rows stay (supply and the inbox scan remember what was cut), marked dismissed.
    with session_scope() as db:
        assert db.get(ClipJob, done_id).result["dismissed"] is True
        assert db.get(ClipJob, failed_id).result["dismissed"] is True
        assert "dismissed" not in (db.get(ClipJob, queued_id).result or {})
    assert clip_job_mod.scan_inbox_sources() == []  # x.mp4 is still known as cut

    # ...but no list shows them: helper, CLI, and API.
    assert [j["id"] for j in clip_job_mod.list_jobs(limit=20)] == [queued_id]
    rc = clip_cli.main(["--jobs", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [j["id"] for j in payload["jobs"]] == [queued_id]
    assert payload["counts"] == {"queued": 1, "running": 0, "done": 0, "failed": 0}
    client = TestClient(app)
    assert [j["id"] for j in client.get("/clip/jobs").json()] == [queued_id]


def test_dismiss_job_is_soft_for_finished_hard_for_queued_and_retry_unhides():
    with session_scope() as db:
        done = ClipJob(source="https://youtu.be/d", status="done")
        failed = ClipJob(source="https://youtu.be/f", status="failed", attempts=3)
        queued = ClipJob(source="https://youtu.be/q", status="queued")
        running = ClipJob(source="https://youtu.be/r", status="running")
        db.add_all([done, failed, queued, running])
        db.flush()
        done_id, failed_id, queued_id, running_id = done.id, failed.id, queued.id, running.id

    assert clip_job_mod.dismiss_job(done_id) == {"id": done_id, "dismissed": True}
    assert clip_job_mod.dismiss_job(done_id) == {"id": done_id, "dismissed": True}  # idempotent
    assert clip_job_mod.dismiss_job(queued_id) == {"id": queued_id, "dismissed": True}
    assert clip_job_mod.dismiss_job(failed_id)["dismissed"] is True
    with pytest.raises(ValueError):
        clip_job_mod.dismiss_job(running_id)
    with pytest.raises(LookupError):
        clip_job_mod.dismiss_job("nope")

    with session_scope() as db:
        assert db.get(ClipJob, queued_id) is None  # never ran: nothing to remember
        assert db.get(ClipJob, done_id) is not None
    assert [j["id"] for j in clip_job_mod.list_jobs(limit=20)] == [running_id]

    view = clip_job_mod.retry_job(failed_id)
    assert view["status"] == "queued"
    assert {j["id"] for j in clip_job_mod.list_jobs(limit=20)} == {running_id, failed_id}
    with session_scope() as db:
        assert "dismissed" not in (db.get(ClipJob, failed_id).result or {})


def test_list_jobs_fills_the_limit_past_dismissed_rows():
    with session_scope() as db:
        base = datetime.now(timezone.utc)
        for i in range(130):  # newer than the live ones, so a plain LIMIT would return only these
            db.add(ClipJob(source=f"https://youtu.be/old{i}", status="done", result={"dismissed": True},
                           created_at=base + timedelta(seconds=10 + i)))
        for i in range(3):
            db.add(ClipJob(source=f"https://youtu.be/live{i}", status="queued", created_at=base - timedelta(seconds=i)))
    shown = clip_job_mod.list_jobs(limit=3)
    assert len(shown) == 3
    assert all(j["status"] == "queued" for j in shown)


