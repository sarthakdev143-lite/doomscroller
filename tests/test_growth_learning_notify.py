"""Wave 9 TDD audit tests for collect, learn, model, factors, recipes, and notify."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels, collect, factors, learn, notify, recipes, safety
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
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "use_mock", True)


def _make_published(
    tmp_path: Path,
    idx: int,
    *,
    yt_id: str | None = None,
    published_at: datetime = START,
    views: int | None = None,
    age_hours: float = 72.0,
    channel_id: str | None = None,
    parent: str = "src",
    source_url: str = "https://example.com/source",
) -> str:
    p = tmp_path / f"v_{idx}.mp4"
    if not p.exists():
        p.write_bytes(b"mp4")
    with session_scope() as db:
        v = Video(
            source="youtube",
            source_id=f"sid_{idx}",
            source_url=source_url,
            title=f"Clip {idx}",
            author="@creator",
            duration_sec=30,
            transformed_path=str(p),
            local_path=str(p),
            status=VideoStatus.PUBLISHED,
            youtube_video_id=f"yt_{idx}" if yt_id is None else yt_id,
            published_at=published_at,
            channel_id=channel_id,
            meta={"clip_rendered": True, "clip_kind": "speech", "clip_parent": parent, "yt_privacy": "public"},
        )
        db.add(v)
        db.flush()
        if views is not None:
            db.add(VideoSnapshot(video_id=v.id, age_hours=age_hours, views=views))
        return v.id


# ---------- 1. collect.py: >50 IDs batching, strike safety, naive timestamps ----------


def test_collect_batches_over_50_ids_skips_blank_protects_brand_new_upload_and_stamps_channel(tmp_path, monkeypatch):
    ch_file = tmp_path / "channels.json"
    ch_file.write_text(json.dumps({"channels": [{"slug": "tech", "name": "Tech"}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(ch_file))
    with session_scope() as db:
        channels.sync_channels(db)
        tech_id = channels.channel_by_slug(db, "tech").id

    now = START + timedelta(hours=3)
    # 62 normal published videos (>50 API limit)
    for i in range(62):
        _make_published(tmp_path, i, yt_id=f"yt_{i}", published_at=START, channel_id=tech_id, parent=f"p_{i}")
    # 1 older video genuinely missing from YouTube -> should get a missing event WITH channel_id stamped
    gone_id = _make_published(tmp_path, 900, yt_id="yt_gone", published_at=START, channel_id=tech_id, parent="p_gone")
    # 1 brand-new upload published 60s ago not yet indexed by YouTube -> must NOT get struck as missing!
    fresh_id = _make_published(
        tmp_path, 901, yt_id="yt_just_uploaded", published_at=now - timedelta(seconds=60), channel_id=tech_id, parent="p_fresh"
    )
    # 1 corrupt blank youtube_video_id="" -> must NOT be queried or struck
    blank_id = _make_published(tmp_path, 902, yt_id="   ", published_at=START, channel_id=tech_id, parent="p_blank")

    batch_sizes: list[int] = []

    class StrictBatchInsights:
        def channel_identity(self):
            return None

        def stats(self, ids: list[str]) -> dict[str, dict]:
            assert len(ids) <= 50, f"YouTube videos.list rejects >50 IDs, got {len(ids)}"
            assert all(i.strip() for i in ids), "Blank YouTube ID sent to stats()"
            batch_sizes.append(len(ids))
            return {
                i: {"views": 42, "likes": 2, "comments": 0, "privacy": "public", "upload_status": "processed"}
                for i in ids
                if i.startswith("yt_") and i not in ("yt_gone", "yt_just_uploaded")
            }

        def retention(self, ids, start, end):
            return {}

    monkeypatch.setattr(collect, "insights_for", lambda ch: StrictBatchInsights())
    out = collect.collect_job(now=now)
    assert out["tech"]["snapshots"] == 62
    assert len(batch_sizes) == 2 and max(batch_sizes) == 50

    with session_scope() as db:
        assert db.get(Video, gone_id).meta.get("yt_missing") is True
        assert not db.get(Video, fresh_id).meta.get("yt_missing")
        assert not db.get(Video, blank_id).meta.get("yt_missing")
        missing_ev = db.execute(select(GrowthEvent).where(GrowthEvent.kind == "missing")).scalar_one()
        assert missing_ev.channel_id == tech_id

    # If batch 2 fails mid-flight, NO video may be falsely marked missing
    calls = 0

    class FlakySecondBatchInsights(StrictBatchInsights):
        def stats(self, ids: list[str]) -> dict[str, dict]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("503 backendError on page 2")
            return super().stats(ids)

    monkeypatch.setattr(collect, "insights_for", lambda ch: FlakySecondBatchInsights())
    err_out = collect.collect_job(now=now + timedelta(hours=1))
    assert "503" in err_out["tech"]["error"]
    with session_scope() as db:
        missing_count = sum(
            1 for v in db.execute(select(Video)).scalars() if (v.meta or {}).get("yt_missing")
        )
        assert missing_count == 1  # still only yt_gone, none from the aborted run


def test_collect_naive_timestamps_and_zero_enabled_channels(tmp_path, monkeypatch):
    vid = _make_published(tmp_path, 1, yt_id="yt_1", published_at=datetime(2026, 11, 2, 12, 0, 0))
    with session_scope() as db:
        v = db.get(Video, vid)
        # Naive ISO updated timestamp stored in yt_analytics
        v.meta = {**v.meta, "yt_analytics": {"averageViewPercentage": 65.0, "updated": "2026-11-05T10:00:00"}}

    # Naive status timestamp + naive now in analytics_warning
    warn = collect.analytics_warning(
        {"analytics_failing_since": "2026-11-01T00:00:00", "analytics_error": "403"},
        datetime(2026, 11, 6, 0, 0, 0),
    )
    assert warn is not None and "5 days" in warn

    class SimpleInsights:
        def stats(self, ids):
            return {"yt_1": {"views": 10, "likes": 0, "comments": 0, "privacy": "public", "upload_status": "processed"}}

        def retention(self, ids, start, end):
            return {}

    monkeypatch.setattr(collect, "get_insights", lambda: SimpleInsights())
    res = collect.collect_job(now=datetime(2026, 11, 5, 12, 0, 0))  # naive now
    assert res["snapshots"] == 1

    # Zero enabled channels
    ch_file = tmp_path / "channels.json"
    ch_file.write_text(json.dumps({"channels": [{"slug": "tech", "enabled": False}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(ch_file))
    with session_scope() as db:
        channels.sync_channels(db)
    assert collect.collect_job(now=START) == {"skipped": "no enabled channels"}


# ---------- 2. learn.py: 0/negative views, small/tied cohorts, winner-mining bounds ----------


def test_learn_zero_and_negative_views_and_scrubbed_snapshots_never_crash():
    assert learn.views_at([(24.0, 100), (96.0, 400)], 0.0) is None
    assert learn.views_at([(24.0, 100), (96.0, 400)], -10.0) is None
    # YouTube scrubbed bot views so 96h count is lower than 24h, or corrupt negative count
    scrubbed = learn.views_at([(24.0, 100), (96.0, -20)], 72.0)
    assert scrubbed is not None and scrubbed >= 0.0

    neg_res = learn.Result("v0", "Title", -5, {"slot": "00-04", "kind": "speech"}, 0.0, 0.5, -2.0, "", False)
    zero_res = learn.Result("v1", "Title", 0, {"slot": "08-12", "kind": "visual"}, 1.0, 0.0, 0.0, "", False)
    assert learn.reward(neg_res, 0.5) >= 0.0
    assert learn.reward(zero_res, 0.0) == pytest.approx(0.0)

    summary = learn.summarise([neg_res, zero_res])
    assert "slot" in summary

    # All-zero cohort diagnosis does not crash or mislabel 0 views as a winner
    zero_cohort = [learn.Result(f"z{i}", "T", 0, {"length": "20-35"}, float(i), 0.0, 0.0, "", False) for i in range(8)]
    diag = learn.diagnose(zero_res, zero_cohort)
    assert diag.code == "typical"


def test_learn_small_and_tied_cohorts_deterministic_baseline(tmp_path, monkeypatch):
    single = [learn.Result("only", "T", 100, {"slot": "00-04"}, 0.0, 0.6, 1.0, "", False)]
    assert learn.summarise(single) == {}
    assert learn.diagnose(single[0], []).code == "too_early"

    # 40 videos published at exact tied day=1.0 -> _baseline must be deterministic across orderings
    tied = [learn.Result(f"vid_{i:02d}", "T", 100 + i, {}, 1.0, None, None, "", False) for i in range(40)]
    target = tied[20]
    b1 = [r.video_id for r in learn._baseline(target, tied)]
    b2 = [r.video_id for r in learn._baseline(target, list(reversed(tied)))]
    assert b1 == b2 and len(b1) == 30

    # Zero enabled channels skips cleanly
    ch_file = tmp_path / "channels.json"
    ch_file.write_text(json.dumps({"channels": [{"slug": "tech", "enabled": False}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(ch_file))
    with session_scope() as db:
        channels.sync_channels(db)
    assert learn.learn_job(now=START) == {"skipped": "no enabled channels"}


def test_learn_winner_mining_respects_pending_approval_stock_blocked_parent_and_naive_now(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_require_approval", True)
    monkeypatch.setattr(settings, "growth_min_inventory", 2)

    for i in range(9):
        _make_published(tmp_path, i, views=100, parent=f"base_{i}", source_url=f"https://example.com/base_{i}")
    winner_id = _make_published(
        tmp_path, 99, views=5000, parent="win_parent", source_url="https://example.com/winner_long"
    )

    # 2 clips already sitting in PENDING awaiting owner approval -> shelf is full, must NOT mine!
    for j in range(2):
        p = tmp_path / f"pend_{j}.mp4"
        p.write_bytes(b"mp4")
        with session_scope() as db:
            db.add(
                Video(
                    source="youtube",
                    source_id=f"pend_{j}",
                    source_url="https://example.com/other",
                    title=f"Pending {j}",
                    duration_sec=25,
                    transformed_path=str(p),
                    local_path=str(p),
                    status=VideoStatus.PENDING,
                    meta={"clip_rendered": True, "clip_kind": "speech"},
                )
            )

    naive_judged = datetime(2026, 11, 7, 12, 0, 0)  # naive datetime!
    out = learn.learn_job(now=naive_judged)
    assert out["diagnosed"]["winner"] == 1
    with session_scope() as db:
        assert db.execute(select(ClipJob)).first() is None

    # Clear PENDING clips, but quarantine the parent via another video with a different source_url and blank author
    with session_scope() as db:
        for v in db.execute(select(Video).where(Video.status == VideoStatus.PENDING)).scalars():
            v.status = VideoStatus.REJECTED
        struck = Video(
            source="youtube",
            source_id="sibling_struck",
            source_url="https://youtu.be/different_shortlink",
            title="Struck sibling",
            author="",
            status=VideoStatus.PUBLISHED,
            youtube_video_id="yt_struck_sib",
            meta={"clip_rendered": True, "clip_parent": "win_parent"},
        )
        db.add(struck)
        db.flush()
        safety.record_strike(struck, "copyright", now=START)

    learn.learn_job(now=naive_judged + timedelta(hours=6))
    with session_scope() as db:
        # Winner's parent is blocked, so it still must NOT be mined!
        assert db.execute(select(ClipJob)).first() is None
        assert "mined_job" not in db.get(Video, winner_id).meta["growth_diagnosis"]


# ---------- 3. model.py & factors.py: collinearity, outliers, timezone/NaN, numpy-free import ----------


def test_model_handles_collinear_arms_nan_observations_extreme_outliers_and_zero_draws():
    # Perfectly collinear arms (kind=speech <-> pace=tight, kind=visual <-> pace=relaxed) + constant day=0
    obs = [
        Observation({"kind": "speech", "pace": "tight"}, 5.0, 0.0),
        Observation({"kind": "visual", "pace": "relaxed"}, 4.0, 0.0),
    ] * 6 + [
        Observation({"kind": "speech", "pace": "tight"}, float("nan"), 0.0),
        Observation({"kind": "visual", "pace": "relaxed"}, 1000.0, float("inf")),
    ]
    m = Model(obs)
    s0 = m.summary(draws=0)
    # Valid JSON (no NaN / Infinity)
    serialized = json.dumps(s0)
    assert "NaN" not in serialized and "Infinity" not in serialized

    # Even with extreme finite reward outlier, multipliers remain finite JSON-serializable numbers
    extreme = Model([Observation({"slot": "00-04"}, 900.0, 0.0), Observation({"slot": "08-12"}, 0.0, 0.0)] * 4)
    dumped = json.dumps(extreme.summary())
    assert "Infinity" not in dumped


def test_factors_invalid_timezone_nan_duration_recipe_validation_and_numpy_free_idle_import(monkeypatch):
    monkeypatch.setattr(settings, "growth_timezone", "   ")
    assert str(factors.channel_zone()) == "UTC"
    monkeypatch.setattr(settings, "growth_timezone", "Mars/Olympus_Mons")
    assert str(factors.channel_zone()) == "UTC"

    assert factors.length_of(float("nan")) == "under20"
    assert factors.length_of(float("inf")) == "under20"
    assert factors.length_of(-15.0) == "under20"
    assert factors.length_of(None) == "under20"

    dummy = Video(
        source="youtube",
        source_id="x",
        source_url="u",
        title="t",
        duration_sec=25,
        published_at=None,
        meta={"clip_kind": "speech", "growth_recipe": {"pace": "invalid_pace", "sfx": "off", "broll": "on"}},
    )
    arms = factors.published_arms(dummy)
    assert "pace" not in arms
    assert arms["sfx"] == "off" and arms["broll"] == "on"

    # Subprocess verification: idle import of app.main, app.review, and non-model growth modules stays numpy-free
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, app.main, app.review, app.growth.factors, app.growth.collect, app.growth.notify, app.growth.recipes; "
            "assert 'numpy' not in sys.modules, f'numpy eagerly loaded: {list(sys.modules)}'",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


# ---------- 4. recipes.py: never enabling owner-disabled settings & partial recipe safety ----------


def test_recipes_choose_and_applied_never_enable_owner_disabled_settings_and_handle_partial_recipe(monkeypatch):
    monkeypatch.setattr(settings, "clip_sfx", False)
    monkeypatch.setattr(settings, "clip_broll", False)

    chosen = recipes.choose()
    assert chosen["sfx"] == "off"
    assert chosen["broll"] == "off"

    with recipes.applied({"pace": "tight", "sfx": "on", "broll": "on"}):
        assert settings.clip_sfx is False
        assert settings.clip_broll is False
        assert settings.clip_speed == factors.PACES["tight"][0]

    # Partial or unknown recipe keys must never raise KeyError and must restore settings cleanly
    orig_speed = settings.clip_speed
    with recipes.applied({"broll": "off"}):
        assert settings.clip_speed == orig_speed
    with recipes.applied({"pace": "nonexistent_pace"}):
        assert settings.clip_speed == orig_speed


# ---------- 5. notify.py: secret redaction, digest bounds/priority, send_test_notification ----------


def test_notify_never_leaks_telegram_or_review_token_in_links_digests_logs_or_invalid_url(tmp_path, monkeypatch, caplog):
    bot_secret = "999888:SUPER_SECRET_BOT_TOKEN"
    review_secret = "SUPER_SECRET_REVIEW_KEY_123"
    monkeypatch.setattr(settings, "telegram_bot_token", bot_secret)
    monkeypatch.setattr(settings, "telegram_chat_id", "777")
    monkeypatch.setattr(settings, "review_token", review_secret)
    monkeypatch.setattr(settings, "ntfy_topic", "my-topic")
    monkeypatch.setattr(settings, "brand_name", "🔥 Clips\r\nInjected: bad")

    tunnel_file = tmp_path / "tunnel_url.txt"
    tunnel_file.write_text(
        f"https://admin:{review_secret}@demo.trycloudflare.com/review?key={review_secret}#frag\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(notify, "TUNNEL_URL_FILE", tunnel_file)

    link = notify.review_link()
    assert link == "https://demo.trycloudflare.com/review"
    assert review_secret not in link and "admin" not in link

    # GrowthEvent summary accidentally containing both tokens -> digest must redact both
    with session_scope() as db:
        db.add(
            GrowthEvent(
                kind="publish_failed",
                summary=f"Failed calling bot{bot_secret} with key={review_secret}",
                created_at=START,
            )
        )
    with session_scope() as db:
        lines = notify.digest(db, START - timedelta(minutes=1))
    joined = "\n".join(lines)
    assert bot_secret not in joined and "SUPER_SECRET_BOT_TOKEN" not in joined
    assert review_secret not in joined

    # httpx.InvalidURL (NOT an httpx.HTTPError!) carrying the full bot URL must never raise or leak in logs
    captured_headers: list[dict] = []

    def boom_post(url, **kwargs):
        if "ntfy" in url:
            captured_headers.append(dict(kwargs.get("headers") or {}))
            raise ValueError(f"bad ntfy call with {review_secret}")
        raise httpx.InvalidURL(f"Invalid URL '{url}' with key {review_secret}")

    monkeypatch.setattr(notify.httpx, "post", boom_post)
    with caplog.at_level("WARNING"):
        assert notify.send(f"hello {review_secret}", link=link) is False
    assert bot_secret not in caplog.text and "SUPER_SECRET_BOT_TOKEN" not in caplog.text
    assert review_secret not in caplog.text
    # Header CR/LF injection and raw non-Latin-1 chars stripped on ntfy Title header
    assert "\r" not in captured_headers[0]["Title"] and "\n" not in captured_headers[0]["Title"]
    captured_headers[0]["Title"].encode("latin-1")


def test_notify_digest_bounds_lines_and_prioritizes_problems_over_routine_posts(tmp_path):
    vids = [_make_published(tmp_path, i, yt_id=f"yt_p{i}", published_at=START) for i in range(18)]
    with session_scope() as db:
        for i, vid in enumerate(vids):
            db.add(GrowthEvent(kind="posted", video_id=vid, created_at=START + timedelta(seconds=i)))
        # Critical strike & pause arrive AFTER 18 routine posts
        db.add(
            GrowthEvent(
                kind="strike",
                summary="yt_bad copyright takedown",
                created_at=START + timedelta(minutes=5),
            )
        )
        db.add(
            GrowthEvent(
                kind="paused",
                summary="Posting paused due to copyright",
                created_at=START + timedelta(minutes=6),
            )
        )

    with session_scope() as db:
        lines = notify.digest(db, START - timedelta(minutes=1))
    assert len(lines) <= 12
    text = "\n".join(lines)
    assert "copyright takedown" in text
    assert "Posting paused due to copyright" in text
    assert "more" in lines[-1]


def test_send_test_notification_returns_per_channel_status_without_leaking_secrets(monkeypatch, caplog):
    monkeypatch.setattr(settings, "ntfy_topic", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    unconf = notify.send_test_notification()
    assert unconf["ok"] is False and unconf["configured"] is False and unconf["channels"] == {}

    bot_secret = "111222:BOT_SECRET_XYZ"
    monkeypatch.setattr(settings, "ntfy_topic", "ops-alerts")
    monkeypatch.setattr(settings, "telegram_bot_token", bot_secret)
    monkeypatch.setattr(settings, "telegram_chat_id", "999")

    class OkResp:
        def raise_for_status(self):
            return None

    def mixed_post(url, **kwargs):
        if "telegram.org" in url:
            raise httpx.ConnectError(f"DNS lookup failed for {url}")
        return OkResp()

    monkeypatch.setattr(notify.httpx, "post", mixed_post)
    with caplog.at_level("WARNING"):
        res = notify.send_test_notification()
    assert res["configured"] is True
    assert res["ok"] is True
    assert res["channels"]["ntfy"]["ok"] is True
    assert res["channels"]["telegram"]["ok"] is False
    dumped = json.dumps(res)
    assert bot_secret not in dumped and "BOT_SECRET_XYZ" not in dumped
    assert bot_secret not in caplog.text and "BOT_SECRET_XYZ" not in caplog.text
