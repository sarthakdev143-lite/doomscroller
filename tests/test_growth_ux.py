"""Maintainer UX, CLI conveniences, and edge-case reliability for the growth engine."""
from __future__ import annotations

import json
import random
import runpy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import init_db, session_scope
from app.growth import autopilot, channels, report, safety, supply
from app.growth.model import Model, Observation
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
        for table in (
            Feed,
            Channel,
            VideoSnapshot,
            MetadataDraft,
            GrowthEvent,
            GrowthState,
            ClipJob,
            QuotaUsage,
            Video,
        ):
            db.execute(delete(table))
    monkeypatch.setattr(settings, "channels_file", "")
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_require_approval", False)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "use_mock", True)


def _write_channels(tmp_path, monkeypatch, entries: list[dict]) -> Path:
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"channels": entries}), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(path))
    return path


def _make_clip(
    tmp_path,
    name: str,
    *,
    status: VideoStatus = VideoStatus.TRANSFORMED,
    channel_id: str | None = None,
    parent: str | None = "src",
    duration: int = 30,
    **meta,
) -> str:
    path = tmp_path / f"{name}.mp4"
    path.write_bytes(b"mp4")
    base_meta = {"clip_rendered": True, "clip_kind": "speech"}
    if parent is not None:
        base_meta["clip_parent"] = parent
    base_meta.update(meta)
    with session_scope() as db:
        video = Video(
            source="youtube",
            source_id=name,
            source_url=f"https://example.com/{name}",
            title=f"Title {name}",
            author="@creator",
            duration_sec=duration,
            transformed_path=str(path),
            local_path=str(path),
            status=status,
            channel_id=channel_id,
            meta=base_meta,
        )
        db.add(video)
        db.flush()
        return video.id


def _published_matured(tmp_path, n: int, views: int, *, channel_id: str | None = None) -> str:
    vid = _make_clip(
        tmp_path,
        f"pub_{n}",
        status=VideoStatus.PUBLISHED,
        channel_id=channel_id,
        parent=f"p_{n}",
        yt_privacy="public",
    )
    with session_scope() as db:
        v = db.get(Video, vid)
        v.published_at = START - timedelta(days=5) + timedelta(hours=n)
        v.youtube_video_id = f"yt_{n}"
        db.add(VideoSnapshot(video_id=vid, age_hours=72.0, views=views))
    return vid


def _run_cli(monkeypatch, capsys, *args: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *args])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    code = int(exc_info.value.code or 0)
    return code, capsys.readouterr().out


# ---------- 1. Timestamp safety (naive vs aware) ----------


def test_naive_timestamps_in_state_and_arguments_do_not_crash(tmp_path):
    naive_now = datetime(2026, 11, 2, 12, 0, 0)  # no tzinfo
    with session_scope() as db:
        # Naive "started", "hold", and "strikes" timestamps persisted from older state or hand edits
        db.add(GrowthState(key="started", value={"at": "2026-10-20T08:00:00", "video_id": "v1"}))
        db.add(GrowthState(key="hold", value={"until": "2026-11-02T13:00:00", "reason": "quota exceeded"}))
        db.add(
            GrowthState(
                key="strikes",
                value={"log": [{"video_id": "old", "reason": "missing", "hard": False, "at": "2026-10-25T00:00:00"}]},
            )
        )

    with session_scope() as db:
        started = autopilot.started_at(db)
        assert started is not None and started.tzinfo is not None
        assert autopilot.explore_rate(started, naive_now) == settings.growth_explore_rate
        assert safety.active_hold(db, naive_now) is not None

    pol = autopilot.policy(now=naive_now, rng=random.Random(1))
    plan = autopilot.build_plan(pol, naive_now)
    assert plan["day"] == "2026-11-02"

    built = report.build_report()
    assert built["day"] >= 1 and built["phase"] in ("testing", "exploiting")

    # fill_post with naive sent_at against aware plan timestamps (and vice versa)
    vid = _make_clip(tmp_path, "crash_rec", status=VideoStatus.PUBLISHED)
    with session_scope() as db:
        db.add(
            GrowthState(
                key="plan",
                value={
                    "day": "2026-11-02",
                    "posts": [
                        {
                            "slot": "08-12",
                            "due": "2026-11-02T11:00:00+00:00",
                            "ends": "2026-11-02T12:00:00+00:00",
                            "state": "waiting",
                            "attempts": 0,
                        }
                    ],
                },
            )
        )
    autopilot.fill_post(vid, datetime(2026, 11, 2, 11, 30, 0))  # naive sent_at
    with session_scope() as db:
        assert db.get(GrowthState, "plan").value["posts"][0]["state"] == "posted"

    # record_strike with naive now against naive strike log entry
    struck_vid = _published_matured(tmp_path, 99, 10)
    with session_scope() as db:
        safety.record_strike(db.get(Video, struck_vid), "missing", now=naive_now)
        assert len(db.get(GrowthState, "strikes").value["log"]) == 2

    # supply.can_take with naive now and a failed job missing timestamps
    orphan_job = ClipJob(source="https://example.com/1", status="failed", created_at=None, finished_at=None)
    assert supply.can_take([orphan_job], naive_now) is True
    timed_job = ClipJob(
        source="https://example.com/2",
        status="failed",
        created_at=datetime(2026, 11, 2, 1, 0, 0),
        finished_at=None,
    )
    assert supply.can_take([timed_job], naive_now) is True


# ---------- 2. Safety & Autopilot edge cases ----------


def test_unblock_blank_or_whitespace_never_lifts_unrelated_blocks(tmp_path):
    vid = _make_clip(tmp_path, "struck", status=VideoStatus.PUBLISHED, parent=None)
    with session_scope() as db:
        v = db.get(Video, vid)
        v.youtube_video_id = "yt_struck"
        v.source_url = ""  # blank source_url and None parent
        v.author = "@BadActor"
        safety.record_strike(v, "copyright", now=START)

    assert safety.unblock("") == 0
    assert safety.unblock("   ") == 0
    with session_scope() as db:
        assert safety.blocked_authors(db) == {"@badactor"}

    # Leading/trailing whitespace around a real handle still lifts cleanly
    assert safety.unblock("  @badactor  ") == 1
    with session_scope() as db:
        assert safety.blocked_authors(db) == set()


def test_pick_clip_with_no_last_parent_does_not_exclude_parentless_clip(tmp_path):
    # Model strongly prefers visual over speech
    known = Model([Observation({"kind": "visual"}, 9.0, 0.0), Observation({"kind": "speech"}, 1.0, 0.0)] * 6)
    pol = autopilot.Policy(known.draw(0), {}, rate=0.0, focus=frozenset(), rng=random.Random(0))

    best_id = _make_clip(tmp_path, "parentless", parent=None, clip_kind="visual")
    other_id = _make_clip(tmp_path, "parented", parent="src_b", clip_kind="speech")
    with session_scope() as db:
        clips = [db.get(Video, best_id), db.get(Video, other_id)]
        picked = autopilot.pick_clip(clips, pol, last_parent=None)
        assert picked.id == best_id


def test_autopilot_and_supply_handle_zero_enabled_channels_and_none_channel(tmp_path, monkeypatch):
    _write_channels(tmp_path, monkeypatch, [{"slug": "tech", "name": "Tech", "enabled": False}])
    with session_scope() as db:
        channels.sync_channels(db)

    assert autopilot.autopilot_job(now=START) == {"skipped": "no enabled channels"}
    assert supply.supply_job() == {"skipped": "no enabled channels"}

    # Direct _autopilot_one(None, ...) must not crash on approval sweep when channel is None
    out = autopilot._autopilot_one(None, START, random.Random(1))
    assert "waiting" in out


# ---------- 3. Supply multi-channel starvation isolation ----------


def test_supply_starvation_isolates_failed_jobs_per_channel_and_gives_named_hint(tmp_path, monkeypatch):
    _write_channels(
        tmp_path,
        monkeypatch,
        [
            {"slug": "tech", "name": "Tech", "feeds": ["https://yt.example/tech"]},
            {"slug": "comedy", "name": "Comedy", "feeds": ["https://yt.example/comedy"]},
        ],
    )
    monkeypatch.setattr(supply, "_feed_links", lambda feed: iter(()))
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
        db.add(
            ClipJob(
                source="https://yt.example/tech/1",
                channel_id=tech.id,
                status="failed",
                error="Deepgram 401 unauthorized",
                finished_at=START,
            )
        )

    out = supply.supply_job()
    assert "Deepgram 401" in out["tech"]["starved"]
    # Comedy had zero failed jobs of its own — must NOT blame Tech's failure!
    assert "Deepgram 401" not in out["comedy"]["starved"]
    with session_scope() as db:
        comedy_gap = db.get(GrowthState, "comedy:supply_gap").value
    assert "Deepgram" not in comedy_gap["cause"]
    assert "channels.py feed comedy --add" in comedy_gap["step"]


# ---------- 4. Report & Doctor edge cases and clarity ----------


def test_report_zero_and_few_matured_posts_no_stale_factors_and_no_best_worst_overlap(tmp_path):
    # Stale summary in DB while 0 matured videos exist -> factors must be {}
    with session_scope() as db:
        db.add(GrowthState(key="summary", value={"slot": [{"arm": "00-04", "multiplier": 2.0, "n": 5, "p_best": 0.9}]}))
    empty_rep = report.build_report()
    assert empty_rep["matured"] == 0
    assert empty_rep["factors"] == {}
    assert empty_rep["best"] == [] and empty_rep["worst"] == []

    # 6 matured videos -> top 5 in best, ONLY the 6th in worst (zero overlap!)
    for idx, views in enumerate((600, 500, 400, 300, 200, 100)):
        _published_matured(tmp_path, idx, views)
    six_rep = report.build_report()
    best_ids = {r["id"] for r in six_rep["best"]}
    worst_ids = {r["id"] for r in six_rep["worst"]}
    assert len(best_ids) == 5
    assert len(worst_ids) == 1
    assert best_ids.isdisjoint(worst_ids)


def test_multi_channel_fleet_report_aggregates_best_winners(tmp_path, monkeypatch):
    _write_channels(
        tmp_path,
        monkeypatch,
        [{"slug": "tech", "name": "Tech"}, {"slug": "comedy", "name": "Comedy"}],
    )
    with session_scope() as db:
        channels.sync_channels(db)
        tech_id = channels.channel_by_slug(db, "tech").id
        comedy_id = channels.channel_by_slug(db, "comedy").id
    _published_matured(tmp_path, 1, 1200, channel_id=tech_id)
    _published_matured(tmp_path, 2, 3500, channel_id=comedy_id)

    rep = report.build_report()
    assert [r["views"] for r in rep["best"]] == [3500, 1200]
    assert rep["best"][0]["channel"] == "Comedy"


def test_stock_warning_recognizes_unflagged_clips_waiting_in_review(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_require_approval", True)
    _make_clip(tmp_path, "waiting_clean", status=VideoStatus.PENDING)

    warnings = report.build_report()["warnings"]
    assert len(warnings) == 1
    assert "waiting for approval" in warnings[0]
    assert "GROWTH_FEEDS" not in warnings[0]


def test_doctor_clarity_missing_secret_disabled_fleet_and_named_only_live(tmp_path, monkeypatch):
    missing_secret = tmp_path / "does_not_exist_secret.json"
    _write_channels(
        tmp_path,
        monkeypatch,
        [
            {
                "slug": "tech",
                "name": "Tech",
                "client_secrets": str(missing_secret),
                "token_path": str(tmp_path / "missing_tok.json"),
                "enabled": False,
            }
        ],
    )
    with session_scope() as db:
        channels.sync_channels(db)

    offline = report.doctor(live=False)
    # 0 enabled channels -> cannot claim ready=True, and global legacy creds must not be required
    assert offline["ready"] is False
    by_name = {c["name"]: c for c in offline["checks"]}
    assert by_name["channels.json"]["ok"] is False and "none enabled" in by_name["channels.json"]["detail"]
    assert by_name["YOUTUBE_CLIENT_SECRETS"]["required"] is False

    # Enable tech with missing secret path -> client_secrets detail explicitly says "missing at ..."
    channels.set_enabled("tech", True)
    enabled_doc = report.doctor(live=False)
    tech_checks = {c["name"]: c for c in enabled_doc["channels"][0]["checks"]}
    assert tech_checks["client_secrets"]["ok"] is False
    assert "missing at" in tech_checks["client_secrets"]["detail"]

    # Live doctor with named-only active channels must NOT call legacy get_insights()
    monkeypatch.setattr(report, "get_insights", lambda: pytest.fail("legacy global get_insights should not be called"))
    monkeypatch.setattr(report, "insights_for", lambda ch: None)
    live_doc = report.doctor(live=True)
    assert "channels" in live_doc


# ---------- 5. CLI UX: --json, status, --channel, pause, typo-safe resume ----------


def test_cli_report_and_doctor_json_and_channel_filter(tmp_path, monkeypatch, capsys):
    _write_channels(
        tmp_path,
        monkeypatch,
        [{"slug": "tech", "name": "Tech"}, {"slug": "comedy", "name": "Comedy"}],
    )
    with session_scope() as db:
        channels.sync_channels(db)
        tech_id = channels.channel_by_slug(db, "tech").id
    _make_clip(tmp_path, "t1", channel_id=tech_id)

    code, out = _run_cli(monkeypatch, capsys, "report", "--json")
    assert code == 0
    parsed = json.loads(out)
    assert set(parsed["channels"]) == {"tech", "comedy"} and parsed["stock"] == 1

    code_ch, out_ch = _run_cli(monkeypatch, capsys, "report", "--channel", "tech", "--json")
    assert code_ch == 0
    tech_rep = json.loads(out_ch)
    assert tech_rep["slug"] == "tech" and tech_rep["stock"] == 1

    code_bad, _ = _run_cli(monkeypatch, capsys, "report", "--channel", "nonexistent")
    assert code_bad == 1

    code_doc, out_doc = _run_cli(monkeypatch, capsys, "doctor", "--offline", "--json")
    assert code_doc == 1  # missing credentials -> not ready, exit 1 preserved
    doc_json = json.loads(out_doc)
    assert doc_json["ready"] is False and "checks" in doc_json

    code_doc_ch, out_doc_ch = _run_cli(monkeypatch, capsys, "doctor", "--offline", "--channel", "tech", "--json")
    assert code_doc_ch == 1
    doc_ch = json.loads(out_doc_ch)
    assert doc_ch["slug"] == "tech" and doc_ch["ready"] is False


def test_cli_status_subcommand_default_and_json(tmp_path, monkeypatch, capsys):
    _write_channels(
        tmp_path,
        monkeypatch,
        [{"slug": "tech", "name": "Tech"}, {"slug": "comedy", "name": "Comedy"}],
    )
    now = datetime.now(timezone.utc)
    upcoming = (now + timedelta(minutes=45)).isoformat()
    expired = (now - timedelta(hours=10)).isoformat()
    expired_ends = (now - timedelta(hours=6)).isoformat()

    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
        comedy = channels.channel_by_slug(db, "comedy")
        tech_id, comedy_id = tech.id, comedy.id
        db.add(
            GrowthState(
                key="tech:plan",
                value={
                    "day": now.date().isoformat(),
                    "posts": [
                        # Expired window past grace must NOT appear as next_post
                        {"slot": "00-04", "due": expired, "ends": expired_ends, "state": "waiting", "attempts": 0},
                        {"slot": "16-20", "due": upcoming, "ends": (now + timedelta(hours=3)).isoformat(), "state": "waiting", "attempts": 0},
                    ],
                },
            )
        )
        # Paused channel's waiting post should not claim global next_post
        db.add(
            GrowthState(
                key="comedy:plan",
                value={
                    "day": now.date().isoformat(),
                    "posts": [
                        {"slot": "12-16", "due": (now + timedelta(minutes=10)).isoformat(), "ends": (now + timedelta(hours=2)).isoformat(), "state": "waiting", "attempts": 0}
                    ],
                },
            )
        )
    _make_clip(tmp_path, "ready1", channel_id=tech_id, status=VideoStatus.TRANSFORMED)
    _make_clip(tmp_path, "rev1", channel_id=tech_id, status=VideoStatus.PENDING)
    safety.pause("comedy")

    # Default invocation (no args) runs status
    code_default, text_out = _run_cli(monkeypatch, capsys)
    assert code_default == 0
    assert "tech" in text_out and "comedy" in text_out and "PAUSED" in text_out

    # Machine-readable JSON status
    code_json, json_out = _run_cli(monkeypatch, capsys, "status", "--json")
    assert code_json == 0
    st = json.loads(json_out)
    assert st["stock"] == 1 and st["awaiting_review"] == 1
    assert st["paused_channels"] == ["comedy"]
    assert st["next_post"] == upcoming
    by_slug = {c["slug"]: c for c in st["channels"]}
    assert by_slug["tech"]["next_post"]["slot"] == "16-20"
    assert by_slug["comedy"]["paused"] is not None


def test_cli_pause_and_typo_safe_resume(tmp_path, monkeypatch, capsys):
    _write_channels(tmp_path, monkeypatch, [{"slug": "tech", "name": "Tech"}])
    with session_scope() as db:
        channels.sync_channels(db)

    code_bad_pause, out_bad_pause = _run_cli(monkeypatch, capsys, "pause", "tehc")
    assert code_bad_pause == 1 and "tech" in out_bad_pause

    code_pause, out_pause = _run_cli(monkeypatch, capsys, "pause", "tech")
    assert code_pause == 0 and "Paused tech" in out_pause

    # Typo on resume must exit 1 and name known channels instead of silently exiting 0
    code_typo, out_typo = _run_cli(monkeypatch, capsys, "resume", "tehc")
    assert code_typo == 1 and "tech" in out_typo
    with session_scope() as db:
        assert safety.pause_reason(db, channels.channel_by_slug(db, "tech")) is not None

    code_res, out_res = _run_cli(monkeypatch, capsys, "resume", "tech")
    assert code_res == 0 and "Resumed tech" in out_res
    with session_scope() as db:
        assert safety.pause_reason(db, channels.channel_by_slug(db, "tech")) is None
