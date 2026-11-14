"""3-channel end-to-end isolation tests (qa-4).

Covers multi-channel isolation across:
1. Timezones, local calendar day boundaries, slot/day_type attribution, and A/B learning
2. Per-channel daily upload quotas and collision-proof QuotaUsage row keys
3. Per-channel owner pauses, soft/hard takedown strikes, account holds, and global source quarantine
4. Feed deduplication, supply, Atom watch, scoped approval/backstop/pinning, and stuck-publish recovery
5. Legacy NULL channel_id fallback and hybrid (default + named channels) coexistence
6. Portable export/import/clone roundtrips and secret/OAuth-binding stripping
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app import quota
from app.config import settings
from app.db import init_db, session_scope
from app.growth import approval, autopilot, channels, collect, learn, notify, report, safety, supply, watch
from app.growth.factors import channel_zone, published_arms
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


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Reset every multi-channel table and isolate config/filesystem paths per test."""
    init_db()
    with session_scope() as db:
        for table in (
            VideoSnapshot,
            MetadataDraft,
            Feed,
            Channel,
            ClipJob,
            GrowthEvent,
            GrowthState,
            QuotaUsage,
            Video,
        ):
            db.execute(delete(table))

    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "growth_inbox_dir", str(inbox))
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "use_mock", True)
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    monkeypatch.setattr(settings, "youtube_token_path", str(tmp_path / "legacy_token.json"))
    monkeypatch.setattr(settings, "youtube_client_secrets", "")
    monkeypatch.setattr(settings, "growth_uploads_per_day", 3)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "growth_require_approval", True)
    monkeypatch.setattr(settings, "growth_min_inventory", 4)
    monkeypatch.setattr(settings, "growth_min_gap_minutes", 30)
    monkeypatch.setattr(settings, "growth_horizon_hours", 48)
    monkeypatch.setattr(settings, "max_uploads_per_day", 10)
    monkeypatch.setattr(settings, "brand_name", "@legacybrand")
    monkeypatch.setattr(settings, "review_token", "qa4-secret-review-token")


def _write_fleet(tmp_path: Path, entries: list[dict]) -> Path:
    cfg = tmp_path / "channels.json"
    cfg.write_text(json.dumps({"channels": entries}, indent=2), encoding="utf-8")
    return cfg


def _seed_three_channels(tmp_path: Path) -> dict[str, Channel]:
    """Create 3 distinct channels spanning UTC-5, UTC+5:30, and UTC+9."""
    _write_fleet(
        tmp_path,
        [
            {
                "slug": "alpha",
                "name": "Alpha Tech",
                "niche": "tech",
                "brand_name": "@alphatech",
                "client_secrets": str(tmp_path / "secrets" / "alpha_cs.json"),
                "token_path": str(tmp_path / "secrets" / "alpha_tok.json"),
                "uploads_per_day": 3,
                "timezone": "America/New_York",
                "require_approval": False,
                "auto_approve_score": 0.0,
                "cadence_backstop": False,
                "enabled": True,
                "feeds": ["https://www.youtube.com/@AlphaCreator"],
            },
            {
                "slug": "beta",
                "name": "Beta Science",
                "niche": "science",
                "brand_name": "@betasci",
                "client_secrets": str(tmp_path / "secrets" / "beta_cs.json"),
                "token_path": str(tmp_path / "secrets" / "beta_tok.json"),
                "uploads_per_day": 4,
                "timezone": "Asia/Kolkata",
                "require_approval": True,
                "auto_approve_score": 0.75,
                "cadence_backstop": True,
                "enabled": True,
                "feeds": ["https://www.youtube.com/@BetaCreator"],
            },
            {
                "slug": "gamma",
                "name": "Gamma Gaming",
                "niche": "gaming",
                "brand_name": "",
                "client_secrets": str(tmp_path / "secrets" / "gamma_cs.json"),
                "token_path": str(tmp_path / "secrets" / "gamma_tok.json"),
                "uploads_per_day": 2,
                "timezone": "Asia/Tokyo",
                "require_approval": True,
                "auto_approve_score": 0.0,
                "cadence_backstop": False,
                "enabled": True,
                "feeds": ["https://www.youtube.com/@GammaCreator"],
            },
        ],
    )
    with session_scope() as db:
        channels.sync_channels(db)
        by_slug = {c.slug: c for c in channels.all_channels(db)}
        by_slug["alpha"].youtube_channel_id = "UC_ALPHA_11111111111111"
        by_slug["beta"].youtube_channel_id = "UC_BETA_222222222222222"
        by_slug["gamma"].youtube_channel_id = "UC_GAMMA_33333333333333"
        db.flush()
        db.expunge_all()
    with session_scope() as db:
        out = {c.slug: c for c in channels.all_channels(db)}
        db.expunge_all()
        return out


def _make_clip(
    tmp_path: Path,
    slug_tag: str,
    channel_id: str | None,
    *,
    status: VideoStatus = VideoStatus.TRANSFORMED,
    score: float = 0.85,
    ai_titled: bool = True,
    flagged: str = "",
    author: str = "@creator",
    parent: str | None = None,
    source_url: str = "https://www.youtube.com/watch?v=src123",
    published_at: datetime | None = None,
    youtube_video_id: str | None = None,
    duration_sec: int = 28,
    extra_meta: dict | None = None,
) -> str:
    media_dir = tmp_path / "clips"
    media_dir.mkdir(parents=True, exist_ok=True)
    mp4 = media_dir / f"{slug_tag}.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 2048)
    meta = {
        "clip_rendered": True,
        "clip_kind": "speech",
        "clip_parent": parent or f"parent_{slug_tag}",
        "clip_score": score,
        "clip_ai_titled": ai_titled,
        "clip_hook": f"Hook {slug_tag}",
        "clip_opening_line": f"Opening {slug_tag}",
    }
    if flagged:
        meta["growth_flagged"] = flagged
    if extra_meta:
        meta.update(extra_meta)
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        v = Video(
            source="youtube",
            source_id=slug_tag,
            source_url=source_url,
            author=author,
            author_name=author.lstrip("@"),
            title=f"Title {slug_tag}",
            description=f"Desc {slug_tag}",
            language="en",
            duration_sec=duration_sec,
            width=1080,
            height=1920,
            local_path=str(mp4),
            transformed_path=str(mp4),
            status=status,
            channel_id=channel_id,
            transformed_at=now if status == VideoStatus.TRANSFORMED else None,
            published_at=published_at,
            youtube_video_id=youtube_video_id,
            meta=meta,
        )
        db.add(v)
        db.flush()
        return v.id


def _due_plan(local_day: str, now: datetime, slot: str = "12-16") -> dict:
    due = now - timedelta(minutes=25)
    return {
        "day": local_day,
        "posts": [
            {
                "slot": slot,
                "due": due.isoformat(),
                "ends": (due + timedelta(hours=3)).isoformat(),
                "state": "waiting",
                "attempts": 0,
            }
        ],
    }


def _atom_xml(*entries: tuple[str, str]) -> str:
    body = "".join(
        f"<entry><yt:videoId>{vid}</yt:videoId><published>{ts}</published></entry>"
        for vid, ts in entries
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        f'xmlns:yt="http://www.youtube.com/xml/schemas/2015">{body}</feed>'
    )


# ---------------------------------------------------------------------------
# 1. Timezone, Day-Boundary, Slot Attribution & Per-Channel A/B Model Isolation
# ---------------------------------------------------------------------------


def test_three_channels_plan_and_rollover_across_independent_timezones(tmp_path):
    _seed_three_channels(tmp_path)

    # 2026-11-06T22:30:00Z is:
    # - alpha (America/New_York, UTC-5): 2026-11-06 17:30 (Friday)
    # - beta  (Asia/Kolkata, UTC+5:30):  2026-11-07 04:00 (Saturday)
    # - gamma (Asia/Tokyo, UTC+9):       2026-11-07 07:30 (Saturday)
    t0 = datetime(2026, 11, 6, 22, 30, tzinfo=timezone.utc)
    out = autopilot.autopilot_job(now=t0, rng=random.Random(42))
    assert set(out) == {"alpha", "beta", "gamma"}
    assert out["alpha"]["day"] == "2026-11-06"
    assert out["beta"]["day"] == "2026-11-07"
    # Gamma's first window is already due at 07:30 JST, so with an empty shelf it
    # reports skipped instead of waiting; its plan day is verified via state below.
    assert out["gamma"].get("day") == "2026-11-07" or out["gamma"].get("skipped") == "no finished clips in stock"

    with session_scope() as db:
        alpha_plan = db.get(GrowthState, "alpha:plan").value
        beta_plan = db.get(GrowthState, "beta:plan").value
        gamma_plan = db.get(GrowthState, "gamma:plan").value
        assert db.get(GrowthState, "plan") is None  # legacy un-namespaced key untouched

    assert alpha_plan["day"] == "2026-11-06"
    assert beta_plan["day"] == "2026-11-07"
    assert gamma_plan["day"] == "2026-11-07"
    assert 1 <= len(alpha_plan["posts"]) <= 3
    assert 1 <= len(beta_plan["posts"]) <= 4
    assert 1 <= len(gamma_plan["posts"]) <= 2

    # Advance +7h to 2026-11-07T05:30:00Z:
    # - alpha is now 2026-11-07 00:30 EST -> rolls over to 2026-11-07!
    # - beta  is 2026-11-07 11:00 IST -> still 2026-11-07 (same plan day)
    # - gamma is 2026-11-07 14:30 JST -> still 2026-11-07 (same plan day)
    beta_dues_before = [p["due"] for p in beta_plan["posts"]]
    gamma_dues_before = [p["due"] for p in gamma_plan["posts"]]

    t1 = datetime(2026, 11, 7, 5, 30, tzinfo=timezone.utc)
    autopilot.autopilot_job(now=t1, rng=random.Random(99))
    with session_scope() as db:
        assert db.get(GrowthState, "alpha:plan").value["day"] == "2026-11-07"
        assert db.get(GrowthState, "beta:plan").value["day"] == "2026-11-07"
        assert db.get(GrowthState, "gamma:plan").value["day"] == "2026-11-07"
        assert [p["due"] for p in db.get(GrowthState, "beta:plan").value["posts"]] == beta_dues_before
        assert [p["due"] for p in db.get(GrowthState, "gamma:plan").value["posts"]] == gamma_dues_before


def test_published_arms_and_learning_isolate_timezones_and_baselines_across_three_channels(tmp_path):
    chs = _seed_three_channels(tmp_path)

    # Same UTC upload instant maps to different slot AND different weekday/weekend per channel.
    shared_pub = datetime(2026, 11, 6, 22, 30, tzinfo=timezone.utc)  # Fri NY, Sat Kolkata/Tokyo
    dummy = Video(
        source="youtube",
        source_id="probe",
        title="Probe",
        duration_sec=28,
        published_at=shared_pub,
        meta={"clip_rendered": True, "clip_kind": "speech", "growth_title_style": "question"},
    )
    alpha_arms = published_arms(dummy, channel_zone(chs["alpha"]))
    beta_arms = published_arms(dummy, channel_zone(chs["beta"]))
    gamma_arms = published_arms(dummy, channel_zone(chs["gamma"]))

    assert (alpha_arms["slot"], alpha_arms["day_type"]) == ("16-20", "weekday")
    assert (beta_arms["slot"], beta_arms["day_type"]) == ("04-08", "weekend")
    assert (gamma_arms["slot"], gamma_arms["day_type"]) == ("04-08", "weekend")

    # Seed 9 matured public uploads on alpha (baseline ~100 views, 1st is 1,000 -> winner on alpha)
    # and 9 matured public uploads on beta (baseline ~50,000 views, 1st is 1,000 -> low_views on beta).
    # Gamma has 0 uploads.
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    alpha_target = None
    beta_target = None
    for idx in range(9):
        pub = now - timedelta(days=5, hours=idx * 3)
        a_views = 1_000 if idx == 0 else 100
        b_views = 1_000 if idx == 0 else 50_000

        aid = _make_clip(
            tmp_path,
            f"alpha_m_{idx}",
            chs["alpha"].id,
            status=VideoStatus.PUBLISHED,
            published_at=pub,
            youtube_video_id=f"YT_ALPHA_{idx}",
            source_url=f"https://www.youtube.com/watch?v=alpha_src_{idx}",
            extra_meta={"yt_privacy": "public", "yt_upload_status": "processed", "growth_title_style": "plain"},
        )
        bid = _make_clip(
            tmp_path,
            f"beta_m_{idx}",
            chs["beta"].id,
            status=VideoStatus.PUBLISHED,
            published_at=pub,
            youtube_video_id=f"YT_BETA_{idx}",
            source_url=f"https://www.youtube.com/watch?v=beta_src_{idx}",
            extra_meta={"yt_privacy": "public", "yt_upload_status": "processed", "growth_title_style": "curiosity"},
        )
        if idx == 0:
            alpha_target, beta_target = aid, bid
        with session_scope() as db:
            db.add(VideoSnapshot(video_id=aid, taken_at=now, age_hours=48.0, views=a_views))
            db.add(VideoSnapshot(video_id=bid, taken_at=now, age_hours=48.0, views=b_views))

    res = learn.learn_job(now=now)
    assert set(res) == {"alpha", "beta", "gamma"}
    assert res["alpha"]["matured"] == 9
    assert res["beta"]["matured"] == 9
    assert res["gamma"]["matured"] == 0
    assert res["alpha"]["diagnosed"].get("winner") == 1
    assert res["beta"]["diagnosed"].get("low_views") == 1

    with session_scope() as db:
        a_diag = db.get(Video, alpha_target).meta["growth_diagnosis"]
        b_diag = db.get(Video, beta_target).meta["growth_diagnosis"]
        assert a_diag["code"] == "winner"
        assert b_diag["code"] == "low_views"

        # Winner mining queued a ClipJob ONLY for alpha's winning source, stamped with alpha's channel_id & auto_queue.
        mined_jobs = db.execute(select(ClipJob).where(ClipJob.mined.is_(True))).scalars().all()
        assert len(mined_jobs) == 1
        assert mined_jobs[0].channel_id == chs["alpha"].id
        assert mined_jobs[0].source == "https://www.youtube.com/watch?v=alpha_src_0"
        assert mined_jobs[0].auto_queue is True  # alpha has require_approval=False


# ---------------------------------------------------------------------------
# 2. Per-Channel Quota Accounting & Long Channel ID Primary-Key Isolation
# ---------------------------------------------------------------------------


def test_quota_isolates_three_channels_legacy_null_and_long_shared_prefix_ids(tmp_path, monkeypatch):
    from tasks.pipeline import publish_task

    chs = _seed_three_channels(tmp_path)
    monkeypatch.setattr(settings, "max_uploads_per_day", 1)

    now = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
    a_clip1 = _make_clip(tmp_path, "a_q1", chs["alpha"].id)
    a_clip2 = _make_clip(tmp_path, "a_q2", chs["alpha"].id)
    b_clip1 = _make_clip(tmp_path, "b_q1", chs["beta"].id)
    g_clip1 = _make_clip(tmp_path, "g_q1", chs["gamma"].id)

    with session_scope() as db:
        db.add(GrowthState(key="alpha:plan", value=_due_plan("2026-11-10", now)))
        db.add(GrowthState(key="beta:plan", value=_due_plan("2026-11-10", now)))
        db.add(GrowthState(key="gamma:plan", value=_due_plan("2026-11-10", now)))
        # Exhaust ONLY alpha's daily quota on wall-clock today & simulated day.
        quota.consume_one(db, note="alpha manual", channel_id=chs["alpha"].id)
        quota.consume_one(db, note="alpha sim", channel_id=chs["alpha"].id, now=now)

    # Direct publish_task refuses alpha, while autopilot publishes beta & gamma.
    denied = publish_task(a_clip1)
    assert denied["ok"] is False and "quota" in denied["error"]

    out = autopilot.autopilot_job(now=now, rng=random.Random(7))
    assert "daily upload cap reached" in out["alpha"].get("skipped", "")
    assert out["beta"].get("ok") is True and out["beta"]["video_id"] == b_clip1
    assert out["gamma"].get("ok") is True and out["gamma"]["video_id"] == g_clip1

    with session_scope() as db:
        assert db.get(Video, a_clip1).status == VideoStatus.TRANSFORMED
        assert db.get(Video, a_clip2).status == VideoStatus.TRANSFORMED
        assert db.get(Video, b_clip1).status == VideoStatus.PUBLISHED
        assert db.get(Video, g_clip1).status == VideoStatus.PUBLISHED
        assert quota.snapshot(db, chs["beta"].id)["uploads"] == 1
        assert quota.snapshot(db, chs["gamma"].id)["uploads"] == 1
        assert quota.snapshot(db, None)["uploads"] == 0
        assert quota.snapshot(db, "   ")["uploads"] == 0

        # Long 32-char IDs sharing a 24-char prefix must hash to distinct QuotaUsage.id primary keys.
        long_1 = "shared_channel_prefix_0000000001"
        long_2 = "shared_channel_prefix_0000000002"
        quota.consume_one(db, channel_id=long_1, now=now)
        quota.consume_one(db, channel_id=long_2, now=now)
        quota.consume_one(db, channel_id=long_2, now=now)
        assert quota.snapshot(db, long_1, now=now)["uploads"] == 1
        assert quota.snapshot(db, long_2, now=now)["uploads"] == 2


# ---------------------------------------------------------------------------
# 3. Pauses, Soft/Hard Strikes, Account Holds & Global Source Quarantine
# ---------------------------------------------------------------------------


def test_pauses_holds_strikes_and_global_quarantine_across_three_channels(tmp_path, monkeypatch):
    chs = _seed_three_channels(tmp_path)
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)

    # 1. Owner pause on alpha stops ONLY alpha's posting; supply still stocks alpha.
    assert safety.pause("alpha") is True
    a_clip = _make_clip(tmp_path, "a_p1", chs["alpha"].id)
    b_clip = _make_clip(tmp_path, "b_p1", chs["beta"].id)
    g_clip = _make_clip(tmp_path, "g_p1", chs["gamma"].id)

    # Put gamma on a 1-hour account hold as well.
    with session_scope() as db:
        safety.set_hold(db, now, "quotaExceeded on gamma project", chs["gamma"])
        db.add(GrowthState(key="alpha:plan", value=_due_plan("2026-11-10", now)))
        db.add(GrowthState(key="beta:plan", value=_due_plan("2026-11-10", now)))
        db.add(GrowthState(key="gamma:plan", value=_due_plan("2026-11-10", now)))

    tick = autopilot.autopilot_job(now=now, rng=random.Random(3))
    assert "paused" in tick["alpha"].get("skipped", "")
    assert tick["beta"].get("ok") is True and tick["beta"]["video_id"] == b_clip
    assert "on hold" in tick["gamma"].get("skipped", "")

    with session_scope() as db:
        assert db.get(Video, a_clip).status == VideoStatus.TRANSFORMED
        assert db.get(Video, b_clip).status == VideoStatus.PUBLISHED
        assert db.get(Video, g_clip).status == VideoStatus.TRANSFORMED

    # 2. Soft strikes ("missing") on beta: 2 do NOT pause; 3rd pauses ONLY beta; clear_missing lifts block.
    b_m1 = _make_clip(tmp_path, "bm1", chs["beta"].id, status=VideoStatus.PUBLISHED, youtube_video_id="YTB1", published_at=now - timedelta(days=2))
    b_m2 = _make_clip(tmp_path, "bm2", chs["beta"].id, status=VideoStatus.PUBLISHED, youtube_video_id="YTB2", published_at=now - timedelta(days=2))
    b_m3 = _make_clip(tmp_path, "bm3", chs["beta"].id, status=VideoStatus.PUBLISHED, youtube_video_id="YTB3", published_at=now - timedelta(days=2))
    with session_scope() as db:
        safety.record_strike(db.get(Video, b_m1), "missing", now=now)
        safety.record_strike(db.get(Video, b_m2), "missing", now=now)
        assert safety.pause_reason(db, chs["beta"]) is None
        safety.record_strike(db.get(Video, b_m3), "missing", now=now)
        assert safety.pause_reason(db, chs["beta"]) is not None
        assert safety.pause_reason(db, chs["gamma"]) is None
        # Video YTB3 reappears on YouTube -> its strike and block clear, though pause waits for owner resume.
        safety.clear_missing(db.get(Video, b_m3))
        assert "growth_strike" not in (db.get(Video, b_m3).meta or {})

    # 3. Hard copyright strike on alpha replaces alpha's owner pause AND quarantines shared parent/author globally!
    toxic_pub = _make_clip(
        tmp_path,
        "a_toxic_pub",
        chs["alpha"].id,
        status=VideoStatus.PUBLISHED,
        youtube_video_id="YT_TOXIC",
        author="@ToxicAuthor",
        parent="yt_toxic_source",
        source_url="https://www.youtube.com/watch?v=toxic999",
        published_at=now - timedelta(days=1),
    )
    # Unposted clip from the SAME struck parent sitting on gamma's shelf must be quarantined immediately!
    gamma_sibling = _make_clip(
        tmp_path,
        "g_toxic_sib",
        chs["gamma"].id,
        status=VideoStatus.TRANSFORMED,
        author="@ToxicAuthor",
        parent="yt_toxic_source",
        source_url="https://www.youtube.com/watch?v=toxic999",
    )
    with session_scope() as db:
        safety.record_strike(db.get(Video, toxic_pub), "copyright", now=now)
        alpha_state = safety.pause_state(db, chs["alpha"])
        assert alpha_state is not None and alpha_state.get("by") != "owner"
        assert "copyright" in alpha_state["reason"]
        # Gamma's posting is NOT paused, yet the toxic sibling clip is quarantined out of gamma's ready_clips!
        assert safety.pause_reason(db, chs["gamma"]) is None
        gamma_ready_ids = {v.id for v in autopilot.ready_clips(db, channels.video_scope(chs["gamma"]))}
        assert gamma_sibling not in gamma_ready_ids
        assert g_clip in gamma_ready_ids

    # Watch & supply skip @ToxicAuthor across ALL channels until unblocked.
    monkeypatch.setattr(
        supply,
        "_read_feed",
        lambda feed, depth: {
            "uploader": "@ToxicAuthor",
            "entries": [{"url": "https://www.youtube.com/watch?v=toxic_next", "uploader": "@ToxicAuthor"}],
        },
    )
    assert list(supply._feed_links("https://www.youtube.com/@GammaCreator")) == []

    assert safety.unblock("@ToxicAuthor") == 1
    with session_scope() as db:
        gamma_ready_after = {v.id for v in autopilot.ready_clips(db, channels.video_scope(chs["gamma"]))}
        assert gamma_sibling in gamma_ready_after

    # Targeted resume clears ONLY beta; resume(None) clears remaining alpha pause.
    assert safety.resume("gamma") is False
    assert safety.resume("beta") is True
    with session_scope() as db:
        assert safety.pause_reason(db, chs["beta"]) is None
        assert safety.pause_reason(db, chs["alpha"]) is not None
    assert safety.resume(None) is True
    with session_scope() as db:
        assert safety.pause_reason(db, chs["alpha"]) is None


# ---------------------------------------------------------------------------
# 4. Feeds, Supply, Watch, Collect, Scoped Approval & Stuck-Publish Recovery
# ---------------------------------------------------------------------------


def test_shared_feed_variants_rejected_and_supply_watch_isolate_three_channels(tmp_path, monkeypatch):
    chs = _seed_three_channels(tmp_path)

    # Equivalent handle/mobile/tab feed URL listed on a second channel is rejected without retiring the fleet.
    _write_fleet(
        tmp_path,
        [
            {"slug": "alpha", "feeds": ["https://www.youtube.com/@AlphaCreator"]},
            {"slug": "beta", "feeds": ["https://m.youtube.com/@alphacreator/videos"]},
            {"slug": "gamma", "feeds": ["https://www.youtube.com/@GammaCreator"]},
        ],
    )
    with session_scope() as db:
        active = channels.sync_channels(db)
        assert {c.slug for c in active} == {"alpha", "beta", "gamma"}
        assert "already listed under 'alpha'" in channels.config_health(db)["error"]
        # Beta kept its original feed rather than adopting the conflicting config.
        assert channels.feeds_for(db, channels.channel_by_slug(db, "beta")) == [
            "https://www.youtube.com/@BetaCreator"
        ]

    # Restore clean 3-channel file and drop an inbox file on disk: named channels must ignore the inbox!
    _seed_three_channels(tmp_path)
    inbox_mp4 = Path(settings.growth_inbox_path) / "local_drop.mp4"
    inbox_mp4.write_bytes(b"0" * 4096)

    feed_map = {
        "https://www.youtube.com/@AlphaCreator": ["https://www.youtube.com/watch?v=alpha_v1"],
        "https://www.youtube.com/@BetaCreator": ["https://www.youtube.com/watch?v=beta_v1"],
    }

    def fake_feed_links(feed: str):
        if "GammaCreator" in feed:
            raise RuntimeError("RSS 503 upstream")
        return iter(feed_map.get(feed, []))

    monkeypatch.setattr(supply, "_feed_links", fake_feed_links)
    sup = supply.supply_job()
    assert sup["alpha"]["queued"] == "https://www.youtube.com/watch?v=alpha_v1"
    assert sup["beta"]["queued"] == "https://www.youtube.com/watch?v=beta_v1"
    assert "starved" in sup["gamma"]  # unread feed recorded on gamma without stopping alpha/beta

    with session_scope() as db:
        jobs = {j.source: j for j in db.execute(select(ClipJob)).scalars()}
        assert set(jobs) == {
            "https://www.youtube.com/watch?v=alpha_v1",
            "https://www.youtube.com/watch?v=beta_v1",
        }
        assert jobs["https://www.youtube.com/watch?v=alpha_v1"].channel_id == chs["alpha"].id
        assert jobs["https://www.youtube.com/watch?v=alpha_v1"].auto_queue is True
        assert jobs["https://www.youtube.com/watch?v=beta_v1"].channel_id == chs["beta"].id
        assert jobs["https://www.youtube.com/watch?v=beta_v1"].auto_queue is False

    # Watch sensor: watermark all 3 channels first, then queue fresh uploads per channel.
    rss_by_slug = {
        "AlphaCreator": "UC_FEED_ALPHA_0000000001",
        "BetaCreator": "UC_FEED_BETA_00000000002",
        "GammaCreator": "UC_FEED_GAMMA_0000000003",
    }
    phase = {"step": 1}

    def fake_watch_fetch(url: str):
        for handle, ucid in rss_by_slug.items():
            if handle in url:
                return f'<html>"externalId":"{ucid}"</html>'
        for handle, ucid in rss_by_slug.items():
            if ucid in url:
                if phase["step"] == 1:
                    return _atom_xml((f"OLD_{handle[:3]}", "2026-11-10T08:00:00+00:00"))
                return _atom_xml(
                    (f"NEW_{handle[:3]}", "2026-11-10T09:30:00+00:00"),
                    (f"OLD_{handle[:3]}", "2026-11-10T08:00:00+00:00"),
                )
        return ""

    monkeypatch.setattr(watch, "_fetch", fake_watch_fetch)
    monkeypatch.setattr(watch, "_probe", lambda link: {"duration": 600, "live_status": "not_live", "authors": set()})

    t_w1 = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    assert watch.watch_job(now=t_w1) == {"feeds": 3, "queued": 0}
    phase["step"] = 2
    t_w2 = datetime(2026, 11, 10, 9, 45, tzinfo=timezone.utc)
    assert watch.watch_job(now=t_w2) == {"feeds": 3, "queued": 3}

    with session_scope() as db:
        watch_jobs = {
            j.source: j
            for j in db.execute(select(ClipJob)).scalars()
            if "NEW_" in j.source
        }
        assert watch_jobs["https://www.youtube.com/watch?v=NEW_Alp"].channel_id == chs["alpha"].id
        assert watch_jobs["https://www.youtube.com/watch?v=NEW_Alp"].auto_queue is True
        assert watch_jobs["https://www.youtube.com/watch?v=NEW_Bet"].channel_id == chs["beta"].id
        assert watch_jobs["https://www.youtube.com/watch?v=NEW_Bet"].auto_queue is False
        assert watch_jobs["https://www.youtube.com/watch?v=NEW_Gam"].channel_id == chs["gamma"].id
        assert watch_jobs["https://www.youtube.com/watch?v=NEW_Gam"].auto_queue is False


def test_scoped_approval_backstop_pinning_collect_and_stuck_recovery_across_three_channels(tmp_path, monkeypatch):
    from tasks import pipeline

    chs = _seed_three_channels(tmp_path)
    now = datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc)

    # Scoped approve_clean & reject_flagged only touch the named channel.
    a_clean = _make_clip(tmp_path, "a_cln", chs["alpha"].id, status=VideoStatus.PENDING, score=0.9)
    b_clean_hi = _make_clip(tmp_path, "b_cln_hi", chs["beta"].id, status=VideoStatus.PENDING, score=0.7)
    b_clean_lo = _make_clip(tmp_path, "b_cln_lo", chs["beta"].id, status=VideoStatus.PENDING, score=0.4)
    g_clean = _make_clip(tmp_path, "g_cln", chs["gamma"].id, status=VideoStatus.PENDING, score=0.95)
    b_flag = _make_clip(tmp_path, "b_flg", chs["beta"].id, status=VideoStatus.PENDING, flagged="language: es")
    g_flag = _make_clip(tmp_path, "g_flg", chs["gamma"].id, status=VideoStatus.PENDING, flagged="blocked term: casino")

    app_res = approval.approve_clean(channel_slug="beta", min_score=0.6)
    assert app_res == {"approved": 1, "ids": [b_clean_hi]}
    rej_res = approval.reject_flagged("gamma")
    assert rej_res == {"rejected": 1, "ids": [g_flag]}

    with session_scope() as db:
        assert db.get(Video, a_clean).status == VideoStatus.PENDING
        assert db.get(Video, b_clean_hi).status == VideoStatus.TRANSFORMED
        assert db.get(Video, b_clean_lo).status == VideoStatus.PENDING
        assert db.get(Video, g_clean).status == VideoStatus.PENDING
        assert db.get(Video, b_flag).status == VideoStatus.PENDING
        assert db.get(Video, g_flag).status == VideoStatus.REJECTED

    # Pinning & per-channel minimum gap: alpha posted 5m ago (inside 30m gap), yet beta & gamma are NOT held back!
    _make_clip(
        tmp_path,
        "a_recent_pub",
        chs["alpha"].id,
        status=VideoStatus.PUBLISHED,
        published_at=now - timedelta(minutes=5),
        youtube_video_id="MOCK_a_recent",
    )
    _make_clip(tmp_path, "a_ready", chs["alpha"].id, status=VideoStatus.TRANSFORMED)
    g_old = _make_clip(tmp_path, "g_old", chs["gamma"].id, status=VideoStatus.TRANSFORMED, score=0.95)
    g_pin = _make_clip(tmp_path, "g_pin", chs["gamma"].id, status=VideoStatus.TRANSFORMED, score=0.30)
    approval.pin_clip_next(g_pin, pinned=True)

    # Empty beta's TRANSFORMED shelf so beta's cadence_backstop promotes b_clean_lo (never g_clean!).
    approval.reject_from_shelf(b_clean_hi)

    with session_scope() as db:
        db.add(GrowthState(key="alpha:plan", value=_due_plan("2026-11-10", now)))
        db.add(GrowthState(key="beta:plan", value=_due_plan("2026-11-10", now)))
        db.add(GrowthState(key="gamma:plan", value=_due_plan("2026-11-11", now)))  # 16:00Z = 01:00+1 in Tokyo

    ap_out = autopilot.autopilot_job(now=now, rng=random.Random(11))
    assert ap_out["alpha"] == {"waiting": "minimum gap between uploads"}
    assert ap_out["beta"].get("ok") is True and ap_out["beta"]["video_id"] == b_clean_lo
    assert ap_out["gamma"].get("ok") is True and ap_out["gamma"]["video_id"] == g_pin

    with session_scope() as db:
        assert db.get(Video, g_old).status == VideoStatus.TRANSFORMED
        assert db.get(Video, g_clean).status == VideoStatus.PENDING  # gamma has cadence_backstop=False

    # Collect per-channel isolation: alpha & beta read their own insights, gamma hits account_mismatch.
    a_real = _make_clip(
        tmp_path,
        "a_real",
        chs["alpha"].id,
        status=VideoStatus.PUBLISHED,
        published_at=now - timedelta(days=3),
        youtube_video_id="YT_REAL_ALPHA",
    )
    b_real = _make_clip(
        tmp_path,
        "b_real",
        chs["beta"].id,
        status=VideoStatus.PUBLISHED,
        published_at=now - timedelta(days=3),
        youtube_video_id="YT_REAL_BETA",
    )
    g_real = _make_clip(
        tmp_path,
        "g_real",
        chs["gamma"].id,
        status=VideoStatus.PUBLISHED,
        published_at=now - timedelta(days=3),
        youtube_video_id="YT_REAL_GAMMA",
    )

    class _FakeInsights:
        def __init__(self, yt_cid: str, stats_map: dict, retention_map: dict, uploads: list[dict]):
            self._cid = yt_cid
            self._stats = stats_map
            self._ret = retention_map
            self._uploads = uploads
            self.asked_stats: list[list[str]] = []

        def channel_identity(self):
            return (self._cid, f"Title {self._cid}")

        def stats(self, ids):
            self.asked_stats.append(list(ids))
            return {k: v for k, v in self._stats.items() if k in ids}

        def retention(self, ids, start, end):
            return {k: v for k, v in self._ret.items() if k in ids}

        def feed_share(self, video_id, start, end):
            return 0.82

        def uploads_since(self, since):
            return list(self._uploads)

    fake_by_slug = {
        "alpha": _FakeInsights(
            "UC_ALPHA_11111111111111",
            {"YT_REAL_ALPHA": {"views": 4200, "likes": 210, "comments": 15, "privacy": "public", "upload_status": "processed"}},
            {"YT_REAL_ALPHA": {"views": 4200, "averageViewPercentage": 94.0, "subscribersGained": 12}},
            [{"id": "YT_STUCK_ALPHA", "title": "Title a_stuck #Shorts", "published_at": (now - timedelta(hours=3)).isoformat()}],
        ),
        "beta": _FakeInsights(
            "UC_BETA_222222222222222",
            {"YT_REAL_BETA": {"views": 9100, "likes": 500, "comments": 40, "privacy": "public", "upload_status": "processed"}},
            {"YT_REAL_BETA": {"views": 9100, "averageViewPercentage": 108.0, "subscribersGained": 30}},
            [],
        ),
        "gamma": _FakeInsights("UC_WRONG_ACCOUNT_999999", {}, {}, []),
    }
    monkeypatch.setattr(collect, "insights_for", lambda ch: fake_by_slug[ch.slug])
    col_out = collect.collect_job(now=now)
    assert col_out["alpha"] == {"snapshots": 1, "analytics": 1}
    assert col_out["beta"] == {"snapshots": 1, "analytics": 1}
    assert "another YouTube channel" in col_out["gamma"]["error"]
    assert fake_by_slug["alpha"].asked_stats == [["YT_REAL_ALPHA"]]
    assert fake_by_slug["beta"].asked_stats == [["YT_REAL_BETA"]]

    with session_scope() as db:
        assert db.get(Video, a_real).views == 4200
        assert db.get(Video, b_real).views == 9100
        assert not (db.get(Video, g_real).meta or {}).get("yt_missing")

    # Stuck-publish recovery in non-mock mode checks each channel's own YouTube uploads independently.
    monkeypatch.setattr(settings, "use_mock", False)
    from app.growth import youtube as yt_mod

    monkeypatch.setattr(yt_mod, "insights_for", lambda ch: fake_by_slug[ch.slug])
    stale_claim = (now - timedelta(hours=3)).isoformat()
    a_stuck = _make_clip(
        tmp_path,
        "a_stuck",
        chs["alpha"].id,
        status=VideoStatus.PUBLISHING,
        extra_meta={"publish_claimed_at": stale_claim, "publish_title": "Title a_stuck #Shorts"},
    )
    b_stuck = _make_clip(
        tmp_path,
        "b_stuck",
        chs["beta"].id,
        status=VideoStatus.PUBLISHING,
        extra_meta={"publish_claimed_at": stale_claim, "publish_title": "Title b_stuck #Shorts"},
    )
    g_stuck = _make_clip(
        tmp_path,
        "g_stuck",
        chs["gamma"].id,
        status=VideoStatus.PUBLISHING,
        extra_meta={"publish_claimed_at": stale_claim, "publish_title": "Title g_stuck #Shorts"},
    )

    rec = pipeline.recover_stuck_publishes(now=now)
    assert rec == {"published": 1, "released": 1, "left": 1}
    with session_scope() as db:
        assert db.get(Video, a_stuck).status == VideoStatus.PUBLISHED
        assert db.get(Video, a_stuck).youtube_video_id == "YT_STUCK_ALPHA"
        assert db.get(Video, b_stuck).status == VideoStatus.TRANSFORMED
        assert db.get(Video, g_stuck).status == VideoStatus.PUBLISHING  # kept safe on account mismatch


# ---------------------------------------------------------------------------
# 5. Legacy NULL channel_id Fallback & Hybrid (default + Named) Coexistence
# ---------------------------------------------------------------------------


def test_legacy_null_channel_id_fallback_and_hybrid_three_channel_coexistence(tmp_path, monkeypatch):
    # Start in legacy mode: no channels.json on disk.
    monkeypatch.setattr(settings, "growth_feeds", json.dumps(["https://www.youtube.com/@LegacyCreator"]))
    monkeypatch.setattr(settings, "growth_uploads_per_day", 2)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "growth_require_approval", False)

    now = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
    legacy_null_clip = _make_clip(tmp_path, "legacy_null_1", None, status=VideoStatus.TRANSFORMED)

    with session_scope() as db:
        active = channels.active_channels(db)
        assert [c.slug for c in active] == ["default"]
        default_ch = active[0]
        assert channels.state_key(default_ch, "plan") == "plan"
        assert channels.event_channel(default_ch) is None
        db.add(GrowthState(key="plan", value=_due_plan("2026-11-10", now)))

    # Single legacy channel returns flat dict shapes across autopilot, report, and queue.
    flat_ap = autopilot.autopilot_job(now=now, rng=random.Random(5))
    assert flat_ap.get("ok") is True and flat_ap["video_id"] == legacy_null_clip
    assert "default" not in flat_ap
    flat_rep = report.build_report()
    assert "channels" not in flat_rep and flat_rep["published"] == 1
    assert approval.queue()["channels"] == []

    # Adding named channels onto a running legacy setup keeps "default" active alongside alpha & beta!
    channels.add_channel(
        "alpha",
        name="Alpha",
        timezone="America/New_York",
        feeds=("https://www.youtube.com/@AlphaOnly",),
    )
    channels.add_channel(
        "beta",
        name="Beta",
        timezone="Asia/Tokyo",
        feeds=("https://www.youtube.com/@BetaOnly",),
    )
    channels.set_enabled("alpha", True)
    channels.set_enabled("beta", True)

    with session_scope() as db:
        by_slug = {c.slug: c for c in channels.active_channels(db)}
        assert set(by_slug) == {"default", "alpha", "beta"}
        alpha_id, beta_id = by_slug["alpha"].id, by_slug["beta"].id

    # Seed another legacy NULL-channel clip + pending/ready clips on alpha & beta.
    legacy_null_2 = _make_clip(tmp_path, "legacy_null_2", None, status=VideoStatus.TRANSFORMED)
    legacy_pending = _make_clip(tmp_path, "legacy_pend", None, status=VideoStatus.PENDING, score=0.9)
    alpha_pending = _make_clip(tmp_path, "alpha_pend", alpha_id, status=VideoStatus.PENDING, score=0.9)
    beta_ready = _make_clip(tmp_path, "beta_ready", beta_id, status=VideoStatus.TRANSFORMED)

    # Scoped approve_clean("alpha") MUST NOT touch legacy_pending (channel_id IS NULL);
    # approve_clean("default") MUST approve legacy_pending without touching alpha_pending!
    assert approval.approve_clean(channel_slug="alpha")["ids"] == [alpha_pending]
    assert approval.approve_clean(channel_slug="default")["ids"] == [legacy_pending]

    # Pause default and run autopilot at t + 1h: alpha & beta CANNOT steal legacy_null_2 or legacy_pending!
    t_next = now + timedelta(hours=1)
    assert safety.pause("default") is True
    with session_scope() as db:
        for k in ("plan", "alpha:plan", "beta:plan"):
            db.execute(delete(GrowthState).where(GrowthState.key == k))
        db.add(GrowthState(key="plan", value=_due_plan("2026-11-10", t_next)))
        db.add(GrowthState(key="alpha:plan", value=_due_plan("2026-11-10", t_next)))
        db.add(GrowthState(key="beta:plan", value=_due_plan("2026-11-10", t_next)))

    hybrid_ap = autopilot.autopilot_job(now=t_next, rng=random.Random(12))
    assert "paused" in hybrid_ap["default"].get("skipped", "")
    assert hybrid_ap["alpha"]["video_id"] == alpha_pending
    assert hybrid_ap["beta"]["video_id"] == beta_ready

    with session_scope() as db:
        assert db.get(Video, legacy_null_2).status == VideoStatus.TRANSFORMED
        assert db.get(Video, legacy_pending).status == VideoStatus.TRANSFORMED

    # Fleet report & per-channel report/digest cleanly separate default, alpha, and beta.
    fleet_rep = report.build_report()
    assert set(fleet_rep["channels"]) == {"default", "alpha", "beta"}
    assert fleet_rep["channels"]["default"]["stock"] == 2
    assert fleet_rep["channels"]["alpha"]["published"] == 1
    assert fleet_rep["channels"]["beta"]["published"] == 1
    def_only = report.build_report(channel_slug="default")
    assert def_only["slug"] == "default" and def_only["stock"] == 2
    lines = None
    with session_scope() as db:
        lines = notify.digest(db, now - timedelta(minutes=5))
    assert any("[Alpha]" in ln for ln in lines) and any("[Beta]" in ln for ln in lines)


# ---------------------------------------------------------------------------
# 6. Export / Import / Clone Roundtrips & OAuth/Secret Sanitization
# ---------------------------------------------------------------------------


def test_three_channel_export_import_clone_roundtrips_and_secret_stripping(tmp_path):
    chs = _seed_three_channels(tmp_path)

    # Clone beta -> beta-2 (disabled, unlinked, fresh secret paths, template feeds copied).
    with session_scope() as db:
        cloned = channels.clone_channel(db, "beta", "beta-2", name="Beta Secondary")
        assert cloned.slug == "beta-2"
        assert cloned.enabled is False
        assert cloned.youtube_channel_id is None
        assert cloned.timezone == "Asia/Kolkata"
        assert cloned.uploads_per_day == 4
        assert cloned.auto_approve_score == pytest.approx(0.75)
        assert cloned.cadence_backstop is True
        assert cloned.client_secrets == "secrets/beta-2_client_secret.json"
        assert cloned.token_path == "secrets/beta-2_token.json"
        assert channels.feeds_for(db, cloned) == ["https://www.youtube.com/@BetaCreator"]

        snap = channels.export_channels_snapshot(db)

    assert snap["version"] == 1
    assert snap["count"] == 4
    serialized = json.dumps(snap)
    for secret_leak in (
        "token_path",
        "client_secrets",
        "youtube_channel_id",
        "UC_ALPHA_11111111111111",
        "UC_BETA_222222222222222",
        "UC_GAMMA_33333333333333",
        "alpha_tok.json",
    ):
        assert secret_leak not in serialized

    # Wipe DB & channels.json to simulate a brand-new host; verify dry_run then full import.
    (tmp_path / "channels.json").unlink()
    with session_scope() as db:
        for table in (Feed, Channel, GrowthState):
            db.execute(delete(table))

        dry = channels.import_channels_snapshot(db, snap, dry_run=True)
        assert dry["ok"] is True and dry["dry_run"] is True
        assert set(dry["created"]) == {"alpha", "beta", "beta-2", "gamma"}
        assert not (tmp_path / "channels.json").exists()

        live = channels.import_channels_snapshot(db, snap, dry_run=False)
        assert live["ok"] is True and live["dry_run"] is False
        assert set(live["active"]) == {"alpha", "beta", "gamma"}

        for slug, expected_tz, expected_upd in (
            ("alpha", "America/New_York", 3),
            ("beta", "Asia/Kolkata", 4),
            ("beta-2", "Asia/Kolkata", 4),
            ("gamma", "Asia/Tokyo", 2),
        ):
            ch = channels.channel_by_slug(db, slug)
            assert ch is not None
            assert ch.timezone == expected_tz
            assert ch.uploads_per_day == expected_upd
            assert ch.youtube_channel_id is None
            assert ch.token_path == f"secrets/{slug}_token.json"
            assert ch.client_secrets == f"secrets/{slug}_client_secret.json"

    # Enabling clone while source is still active with identical feeds is refused; handoff works once source is disabled.
    with pytest.raises(channels.ChannelsConfigError, match="already listed under 'beta'"):
        channels.set_enabled("beta-2", True)
    channels.set_enabled("beta", False)
    handoff = channels.set_enabled("beta-2", True)
    assert set(handoff["active"]) == {"alpha", "beta-2", "gamma"}

    # Upsert with smuggled OAuth paths preserves existing binding & custom paths on alpha.
    with session_scope() as db:
        alpha = channels.channel_by_slug(db, "alpha")
        alpha.youtube_channel_id = "UC_REBOUND_ALPHA"
        db.flush()
        channels.import_channels_snapshot(
            db,
            {
                "version": 1,
                "channels": [
                    {
                        "slug": "alpha",
                        "uploads_per_day": 5,
                        "token_path": "../../etc/shadow",
                        "client_secrets": "/root/evil.json",
                        "youtube_channel_id": "UC_ATTACKER",
                        "feeds": ["@AlphaCreator", "https://m.youtube.com/@ALPHACREATOR/videos"],
                    }
                ],
            },
        )
        alpha_after = channels.channel_by_slug(db, "alpha")
        assert alpha_after.uploads_per_day == 5
        assert alpha_after.youtube_channel_id == "UC_REBOUND_ALPHA"
        assert alpha_after.token_path == "secrets/alpha_token.json"
        assert alpha_after.client_secrets == "secrets/alpha_client_secret.json"
        assert channels.feeds_for(db, alpha_after) == ["https://www.youtube.com/@AlphaCreator"]
