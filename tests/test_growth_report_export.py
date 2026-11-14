"""Markdown/JSON analytics report export and the `notify-test` CLI command."""
from __future__ import annotations

import json
import runpy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels, notify, report
from app.models import (
    Channel,
    ClipJob,
    Feed,
    GrowthEvent,
    GrowthState,
    MetadataDraft,
    QuotaUsage,
    Video,
    VideoSnapshot,
    VideoStatus,
)

START = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "growth.py"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (Feed, Channel, VideoSnapshot, MetadataDraft, GrowthEvent, GrowthState, ClipJob, QuotaUsage, Video):
            db.execute(delete(table))
    monkeypatch.setattr(settings, "channels_file", "")
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_require_approval", False)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    monkeypatch.setattr(settings, "growth_horizon_hours", 72)
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "use_mock", True)
    monkeypatch.setattr(settings, "review_token", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    monkeypatch.setattr(settings, "ntfy_topic", "")


def _write_channels(tmp_path, monkeypatch, entries: list[dict]) -> Path:
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"channels": entries}), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(path))
    return path


def _published(tmp_path, n: int, views: int, *, title: str = "", channel_id: str | None = None) -> str:
    path = tmp_path / f"pub_{n}.mp4"
    path.write_bytes(b"mp4")
    with session_scope() as db:
        video = Video(
            source="youtube", source_id=f"pub_{n}", source_url=f"https://example.com/{n}",
            title=title or f"Title {n}", author="@creator", duration_sec=30,
            transformed_path=str(path), local_path=str(path), status=VideoStatus.PUBLISHED,
            channel_id=channel_id, youtube_video_id=f"yt_{n}",
            published_at=START - timedelta(days=5) + timedelta(hours=n),
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_parent": f"p_{n}", "yt_privacy": "public"},
        )
        db.add(video)
        db.flush()
        db.add(VideoSnapshot(video_id=video.id, age_hours=72.0, views=views))
        return video.id


def _cli(monkeypatch, capsys, *args: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *args])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    return int(exc_info.value.code or 0), capsys.readouterr().out


def _cli_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("growth_cli_export", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- Markdown rendering ----------


def test_markdown_report_lists_winners_with_views_velocity_and_escapes_untrusted_text(tmp_path):
    _published(tmp_path, 1, 7200, title="Big | win\n# injected <script>alert(1)</script>")
    _published(tmp_path, 2, 720, title="Quiet one")
    md = report.export_markdown_report()
    assert md.startswith("# Doomscroller growth report")
    assert "Top winning Shorts" in md
    assert "7,200" in md and "100.0" in md  # 7200 views over the 72h horizon = 100 views/h
    assert "Quiet one" in md and md.index("Big") < md.index("Quiet one")
    # A title cannot break the table, open a heading, or inject markup.
    assert "<script>" not in md and "\n# injected" not in md
    assert "Big \\| win" in md


def test_markdown_report_with_no_matured_posts_says_so_and_does_not_crash():
    md = report.export_markdown_report()
    assert "No Shorts have matured yet" in md
    assert "Traceback" not in md and "None\n" not in md and ": None" not in md and "nan" not in md.lower()


def test_markdown_report_shows_learned_preferences_and_focus(tmp_path):
    _published(tmp_path, 1, 5000)
    with session_scope() as db:
        db.add(GrowthState(key="summary", value={"length": [
            {"arm": "20-30s", "multiplier": 1.4, "low": 1.1, "high": 1.8, "p_best": 0.72, "n": 9},
            {"arm": "30-45s", "multiplier": 0.8, "low": 0.5, "high": 1.1, "p_best": 0.1, "n": 4}]}))
        db.add(GrowthState(key="focus", value={"factors": ["length"]}))
    md = report.export_markdown_report()
    assert "Learned preferences" in md
    assert "20-30s" in md and "1.4x" in md and "72%" in md
    assert "Testing harder" in md and "length" in md


def test_markdown_report_carries_safety_and_supply_notes(monkeypatch):
    monkeypatch.setattr(report.safety, "block_list", lambda db: [
        {"authors": ["@bad|guy"], "source": "https://example.com/bad", "reason": "copyright", "at": "2026-11-01T00:00:00+00:00"}])
    with session_scope() as db:
        db.add(GrowthState(key="paused", value={"reason": "copyright strike on yt_9", "at": "2026-11-01T00:00:00+00:00"}))
        db.add(GrowthEvent(kind="strike", summary="Takedown on yt_9"))
        db.add(GrowthEvent(kind="posted", summary="Posted a clip"))
    md = report.export_markdown_report()
    assert "PAUSED" in md and "copyright strike on yt_9" in md
    assert "@bad\\|guy" in md and "Takedown blocks" in md
    assert "Takedown on yt_9" in md and "Problems" in md
    assert "Posted a clip" in md and "Recent" in md
    assert "No finished clips in stock" in md  # supply warning is part of the digest


def test_markdown_report_redacts_secrets_in_titles_and_events(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "review_token", "hunter2-review-secret")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABCdef_ghiJKL")
    _published(tmp_path, 1, 900, title="see https://x.test/p?token=abc123SECRET now")
    with session_scope() as db:
        db.add(GrowthEvent(kind="posted", summary="leak hunter2-review-secret and 123456:ABCdef_ghiJKL"))
    md = report.export_markdown_report()
    for leaked in ("hunter2-review-secret", "ABCdef_ghiJKL", "abc123SECRET"):
        assert leaked not in md


def test_markdown_report_fleet_has_a_section_per_channel_and_can_be_scoped(tmp_path, monkeypatch):
    _write_channels(tmp_path, monkeypatch, [{"slug": "tech", "name": "Tech"}, {"slug": "comedy", "name": "Comedy"}])
    with session_scope() as db:
        channels.sync_channels(db)
        tech_id = channels.channel_by_slug(db, "tech").id
    _published(tmp_path, 1, 3000, title="Tech banger", channel_id=tech_id)
    fleet = report.export_markdown_report()
    assert "Tech (`tech`)" in fleet and "Comedy (`comedy`)" in fleet and "Tech banger" in fleet
    only = report.export_markdown_report(channel_slug="comedy")
    assert "Comedy" in only and "Tech banger" not in only and "(`tech`)" not in only
    with pytest.raises(LookupError, match="unknown channel 'nope'"):
        report.export_markdown_report(channel_slug="nope")


def test_build_report_accepts_a_caller_session_and_still_works_with_no_arguments(tmp_path):
    _published(tmp_path, 1, 800)
    assert report.build_report()["matured"] == 1
    with session_scope() as db:
        assert report.build_report(db)["matured"] == 1
        assert "Doomscroller growth report" in report.export_markdown_report(db)


# ---------- CLI: report ----------


def test_report_cli_markdown_prints_markdown_and_default_stays_plain(tmp_path, monkeypatch, capsys):
    _published(tmp_path, 1, 800, title="Plain title")
    code, out = _cli(monkeypatch, capsys, "report", "--markdown")
    assert code == 0 and out.startswith("# Doomscroller growth report") and "Plain title" in out
    code, out = _cli(monkeypatch, capsys, "report")
    assert code == 0 and "# Doomscroller" not in out and "Published 1" in out


def test_report_cli_out_writes_markdown_file_and_confirms(tmp_path, monkeypatch, capsys):
    _published(tmp_path, 1, 800, title="Filed title")
    target = tmp_path / "nested" / "weekly.md"
    code, out = _cli(monkeypatch, capsys, "report", "--out", str(target))
    assert code == 0 and str(target) in out and "# Doomscroller" not in out
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Doomscroller growth report") and "Filed title" in text
    assert not list(target.parent.glob("*.tmp"))


def test_report_cli_json_out_writes_json_and_prints_a_status_object(tmp_path, monkeypatch, capsys):
    _published(tmp_path, 1, 800)
    target = tmp_path / "weekly.json"
    code, out = _cli(monkeypatch, capsys, "report", "--json", "--out", str(target))
    assert code == 0
    status = json.loads(out)
    assert status["ok"] is True and status["out"] == str(target)
    assert json.loads(target.read_text(encoding="utf-8"))["matured"] == 1


def test_report_cli_unknown_channel_and_unwritable_out_fail_cleanly(tmp_path, monkeypatch, capsys):
    code, out = _cli(monkeypatch, capsys, "report", "--markdown", "--channel", "nope")
    assert code == 1 and "unknown channel" in out
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    code, out = _cli(monkeypatch, capsys, "report", "--json", "--out", str(blocker / "child.json"))
    assert code == 1 and json.loads(out)["ok"] is False


def test_out_and_markdown_are_refused_on_other_commands(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "status", "--out", "x.md"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc_info.value.code == 2


# ---------- CLI: notify-test ----------


def test_notify_test_reports_per_channel_status_in_text_and_json(monkeypatch, capsys):
    monkeypatch.setattr(notify, "send_test_notification", lambda *a, **k: {
        "ok": False, "configured": True,
        "channels": {"ntfy": {"ok": True}, "telegram": {"ok": False, "error": "HTTP 401"}}})
    code, out = _cli(monkeypatch, capsys, "notify-test")
    assert code == 0 and "ntfy: ok" in out and "telegram: FAILED (HTTP 401)" in out
    code, out = _cli(monkeypatch, capsys, "notify-test", "--json")
    data = json.loads(out)
    assert code == 0 and data["ok"] is False and data["channels"]["ntfy"]["ok"] is True


def test_notify_test_when_nothing_is_configured_explains_and_exits_zero(monkeypatch, capsys):
    code, out = _cli(monkeypatch, capsys, "notify-test")
    assert code == 0 and "NTFY_TOPIC" in out and "TELEGRAM" in out
    code, out = _cli(monkeypatch, capsys, "notify-test", "--json")
    assert code == 0 and json.loads(out)["configured"] is False


def test_notify_test_crash_exits_nonzero_without_leaking_the_token(monkeypatch, capsys):
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABCdef_ghiJKL")

    def boom(*a, **k):
        raise RuntimeError("connect failed for bot123456:ABCdef_ghiJKL")

    monkeypatch.setattr(notify, "send_test_notification", boom)
    code, out = _cli(monkeypatch, capsys, "notify-test", "--json")
    assert code == 1 and json.loads(out)["ok"] is False and "ABCdef_ghiJKL" not in out
    code, out = _cli(monkeypatch, capsys, "notify-test")
    assert code == 1 and "ABCdef_ghiJKL" not in out


def test_show_report_function_signature_stays_backward_compatible(tmp_path, capsys):
    _published(tmp_path, 1, 800)
    cli = _cli_module()
    assert cli.show_report() == 0
    assert "Published 1" in capsys.readouterr().out
