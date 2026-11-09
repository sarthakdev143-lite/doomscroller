"""The growth loop: model, diagnosis, planning, collection, and a simulated month."""
import json
import math
import os
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError

import numpy as np
import pytest
from sqlalchemy import delete, func, select

from app import quota, scheduler
from app.config import settings
from app.db import init_db, session_scope
from app.growth import autopilot, collect, factors, learn, recipes, report, safety, supply, titles
from app.growth.model import Model, Observation
from app.models import (Channel, ClipJob, Feed, GrowthEvent, GrowthState, MetadataDraft, QuotaUsage, Video,
                        VideoSnapshot, VideoStatus)
from app.publishers.base import PublishResult, Publisher
from tasks.pipeline import clip_gate_reason

START = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)  # a Monday
JUDGED_AT = START + timedelta(days=5)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (Feed, Channel, VideoSnapshot, MetadataDraft, GrowthEvent, GrowthState, ClipJob, QuotaUsage, Video):
            db.execute(delete(table))
    monkeypatch.setattr(settings, "growth_enabled", True)
    # Most of these tests are about the unattended loop. The approval tests switch it back on.
    monkeypatch.setattr(settings, "growth_require_approval", False)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "enable_publish", True)


def make_clip(path, n=0, *, parent="p", kind="speech", author="a", recipe=None, status=VideoStatus.TRANSFORMED,
              duration=30, **meta):
    with session_scope() as db:
        video = Video(source="youtube", source_id=f"{parent}_{n}", source_url="https://example.com/v", title=f"Clip {n}",
                      author=author, duration_sec=duration, transformed_path=str(path), local_path=str(path),
                      status=status, meta={"clip_rendered": True, "clip_kind": kind, "clip_parent": parent,
                                           "clip_hook": "Nobody told me this", "growth_recipe": recipe or {}, **meta})
        db.add(video)
        db.flush()
        return video.id


def published_clip(path, n, views, *, published_at=START, retention=None, parent="p", slot_hour=None):
    video_id = make_clip(path, n, parent=parent, status=VideoStatus.PUBLISHED, yt_privacy="public")
    with session_scope() as db:
        video = db.get(Video, video_id)
        video.published_at = published_at
        video.youtube_video_id = f"yt{n}"
        if retention is not None:
            video.meta = {**video.meta, "yt_analytics": {"averageViewPercentage": retention * 100, "subscribersGained": 0}}
        db.add(VideoSnapshot(video_id=video_id, age_hours=100, views=views))
    return video_id


# ---------- model ----------
def test_model_separates_factors_that_were_changed_together():
    rng = np.random.default_rng(1)
    observations = []
    for day in range(120):
        slot, style = str(rng.choice(["am", "pm"])), str(rng.choice(["a", "b"]))
        # pm truly triples views and the title style does nothing.
        reward = 5 + (1.1 if slot == "pm" else 0) + rng.normal(0, 0.3)
        observations.append(Observation({"slot": slot, "style": style}, reward, day / 4))
    summary = Model(observations).summary()
    assert summary["slot"][0]["arm"] == "pm" and summary["slot"][0]["p_best"] > 0.99
    assert summary["style"][0]["p_best"] < 0.9


def test_trend_stops_channel_growth_being_credited_to_the_latest_arm():
    observations = [Observation({"slot": "old" if d < 30 else "new"}, 3 + d * 0.05, float(d)) for d in range(60)]
    multipliers = {r["arm"]: r["multiplier"] for r in Model(observations).summary()["slot"]}
    assert multipliers["new"] < 1.6  # a naive average gives about e^1.5 = 4.5x between them


def test_model_with_no_data_can_still_choose():
    draw = Model([]).draw(np.random.default_rng(0))
    assert isinstance(draw.arm("slot", "00-04"), float)
    assert draw.arm("slot", "00-04") == draw.arm("slot", "00-04")


# ---------- rewards ----------
def test_views_at_interpolates_and_waits_for_the_age():
    assert learn.views_at([(24, 100), (96, 400)], 72) == pytest.approx(300)
    assert learn.views_at([(24, 100)], 72) is None
    assert learn.views_at([], 72) is None


def test_reward_counts_missing_retention_as_typical():
    base = learn.Result("v", "", 100, {}, 0, None, None, "", False)
    assert learn.reward(base, 0.7) == pytest.approx(math.log1p(100))
    better = learn.Result("v", "", 100, {}, 0, 0.9, 5, "", False)
    assert learn.reward(better, 0.7) > learn.reward(base, 0.7)


def test_only_public_clips_are_learned_from(tmp_path):
    published_clip(tmp_path / "a.mp4", 1, 500)
    private = published_clip(tmp_path / "b.mp4", 2, 500)
    gone = published_clip(tmp_path / "c.mp4", 3, 500)
    with session_scope() as db:
        db.get(Video, private).meta = {**db.get(Video, private).meta, "yt_privacy": "private"}
        db.get(Video, gone).meta = {**db.get(Video, gone).meta, "yt_missing": True}
        assert len(learn.matured_results(db)) == 1


# ---------- diagnosis ----------
def result(views, retention=None):
    return learn.Result("v", "t", views, {}, 0, retention, None, "", False)


@pytest.mark.parametrize("video, code", [
    (result(1000, 0.7), "winner"),
    (result(200, 0.7), "typical"),
    (result(20, 0.7), "no_reach"),
    (result(200, 0.3), "weak_hold"),
    (result(20, 0.3), "weak_hold"),
])
def test_diagnosis_names_the_stage_that_failed(video, code):
    others = [result(200, 0.7) for _ in range(8)]
    assert learn.diagnose(video, others).code == code


def test_diagnosis_without_retention_does_not_claim_people_stayed():
    assert learn.diagnose(result(20), [result(200) for _ in range(8)]).code == "low_views"


def test_diagnosis_refuses_to_judge_without_a_baseline():
    assert learn.diagnose(result(0), [result(200)]).code == "too_early"


def test_tiny_channel_is_not_full_of_winners():
    assert learn.diagnose(result(4), [result(1) for _ in range(8)]).code != "winner"


def test_learn_mines_a_winner_once_and_keeps_diagnoses(tmp_path):
    for n in range(9):
        published_clip(tmp_path / f"{n}.mp4", n, 100, retention=0.7)
    winner = published_clip(tmp_path / "w.mp4", 99, 5000, retention=0.7)
    assert learn.learn_job(now=JUDGED_AT)["diagnosed"]["winner"] == 1
    learn.learn_job(now=JUDGED_AT)
    with session_scope() as db:
        jobs = db.execute(select(ClipJob)).scalars().all()
        assert len(jobs) == 1 and jobs[0].auto_queue is True
        assert db.get(Video, winner).meta["growth_diagnosis"]["code"] == "winner"


def test_a_run_of_weak_hold_steers_testing_towards_its_factors(tmp_path):
    for n in range(9):
        published_clip(tmp_path / f"{n}.mp4", n, 100, retention=0.7, published_at=START)
    for n in range(10, 15):
        published_clip(tmp_path / f"{n}.mp4", n, 100, retention=0.2, published_at=START + timedelta(days=1))
    assert "length" in learn.learn_job(now=JUDGED_AT)["focus"]


# ---------- choosing ----------
def policy(rate=0.0, tried=None, focus=()):
    return autopilot.Policy(Model([]).draw(np.random.default_rng(3)), tried or {}, rate, frozenset(focus), random.Random(4))


def test_exploring_turn_picks_the_least_tried_arm():
    assert autopilot.pick_arm(policy(1.0, {"slot": {"a": 5, "b": 1}}), "slot", ["a", "b"]) == "b"


def test_focus_raises_the_share_of_tests():
    # The evidence favours a, so b only gets picked on a testing turn.
    known = Model([Observation({"slot": "a"}, 9.0, 0.0), Observation({"slot": "b"}, 1.0, 0.0)] * 5)
    tried = {"slot": {"a": 5, "b": 1}}

    def picks(focus):
        current = autopilot.Policy(known.draw(np.random.default_rng(3)), tried, 0.1, frozenset(focus), random.Random(4))
        return sum(autopilot.pick_arm(current, "slot", ["a", "b"]) == "b" for _ in range(500))

    assert picks(("slot",)) > picks(()) + 50


def test_first_month_tests_harder_than_afterwards():
    assert autopilot.explore_rate(START, START + timedelta(days=29)) == settings.growth_explore_rate
    assert autopilot.explore_rate(START, START + timedelta(days=31)) == settings.growth_settled_explore_rate


def test_plan_uses_distinct_future_windows_inside_the_day():
    now = START + timedelta(hours=13)
    plan = autopilot.build_plan(policy(), now)
    assert len({p["slot"] for p in plan["posts"]}) == 3
    assert all(p["slot"] in ("12-16", "16-20", "20-24") for p in plan["posts"])
    assert all(now <= datetime.fromisoformat(p["due"]) <= datetime.fromisoformat(p["ends"]) - timedelta(minutes=30)
               for p in plan["posts"])


def test_windows_follow_the_channel_timezone(monkeypatch):
    monkeypatch.setattr(settings, "growth_timezone", "Asia/Kolkata")
    assert factors.slot_of(START) == "04-08"  # 05:30 local


def test_plan_copes_with_more_posts_than_windows(monkeypatch):
    monkeypatch.setattr(settings, "growth_uploads_per_day", 8)
    assert len(autopilot.build_plan(policy(), START)["posts"]) == 8


def test_starting_late_does_not_cram_the_day_into_the_last_window():
    posts = autopilot.build_plan(policy(), START + timedelta(hours=22))["posts"]
    assert [p["slot"] for p in posts] == ["20-24"]


def test_starting_in_the_final_margin_still_gives_a_valid_time():
    now = START + timedelta(hours=23, minutes=50)
    assert datetime.fromisoformat(autopilot.build_plan(policy(), now)["posts"][0]["due"]) == now


def test_scraped_videos_never_go_out_through_the_loop(tmp_path):
    with session_scope() as db:
        db.add(Video(source="snapchat", source_id="s", source_url="u", transformed_path="x", status=VideoStatus.TRANSFORMED))
        db.flush()
        assert autopilot.ready_clips(db) == []


def test_next_clip_avoids_the_source_just_posted(tmp_path):
    make_clip(tmp_path / "a", 1, parent="same")
    make_clip(tmp_path / "b", 2, parent="other")
    with session_scope() as db:
        assert autopilot.pick_clip(autopilot.ready_clips(db), policy(), "same").meta["clip_parent"] == "other"


# ---------- titles ----------
def test_quote_title_uses_the_speakers_words_and_stays_within_limits():
    title, style = titles.styled_title("quote", "Boring", "Nobody told me this")
    assert style == "quote" and title == '"Nobody told me this" #Shorts'


def test_title_falls_back_to_plain_and_records_that(monkeypatch):
    monkeypatch.setattr(titles.ai, "ask_json", lambda prompt: None)
    assert titles.styled_title("question", "My title", "hook") == ("My title #Shorts", "plain")


@pytest.mark.parametrize("reply", [{"title": "x" * 200}, {"title": 5}, {}])
def test_bad_ai_titles_are_rejected(monkeypatch, reply):
    monkeypatch.setattr(titles.ai, "ask_json", lambda prompt: reply)
    assert titles.styled_title("curiosity", "My title", "hook")[1] == "plain"


# ---------- recipes ----------
def test_recipe_applies_only_during_the_render(monkeypatch):
    monkeypatch.setattr(settings, "clip_sfx", True)
    before = settings.clip_speed
    with recipes.applied({"pace": "tight", "sfx": "off", "broll": "off"}):
        assert settings.clip_speed == factors.PACES["tight"][0] and settings.clip_sfx is False
    assert settings.clip_speed == before and settings.clip_sfx is True


def test_recipe_is_restored_when_a_render_fails():
    before = settings.clip_speed
    with pytest.raises(ValueError):
        with recipes.applied({"pace": "tight", "sfx": "on", "broll": "on"}):
            raise ValueError
    assert settings.clip_speed == before


def test_recipe_cannot_switch_on_what_the_owner_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "clip_broll", False)
    with recipes.applied({"pace": "normal", "sfx": "on", "broll": "on"}):
        assert settings.clip_broll is False


def test_no_recipe_when_the_loop_is_off(monkeypatch):
    monkeypatch.setattr(settings, "growth_enabled", False)
    assert recipes.choose() == {}


# ---------- autopilot ----------
class FakePublisher(Publisher):
    def __init__(self, error=None):
        self.error, self.uploads = error, []

    def upload(self, **kwargs):
        if self.error:
            raise self.error if isinstance(self.error, Exception) else RuntimeError(self.error)
        self.uploads.append(kwargs)
        return PublishResult(video_id=f"yt{len(self.uploads)}", status="uploaded", raw={})


@pytest.fixture
def publisher(monkeypatch):
    fake = FakePublisher()
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    return fake


def test_autopilot_posts_when_due_and_records_the_test(tmp_path, publisher):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"x")
    video_id = make_clip(path)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value={"day": START.date().isoformat(), "posts": [
            {"slot": "00-04", "due": START.isoformat(), "ends": (START + timedelta(hours=4)).isoformat(),
             "state": "waiting", "attempts": 0}]}))
    outcome = autopilot.autopilot_job(now=START + timedelta(hours=1), rng=random.Random(1))
    assert outcome["ok"] is True and len(publisher.uploads) == 1
    with session_scope() as db:
        assert db.get(Video, video_id).meta["growth_title_style"] in ("plain", "quote")
        assert db.get(GrowthState, "plan").value["posts"][0]["state"] == "posted"
    assert autopilot.autopilot_job(now=START + timedelta(hours=2), rng=random.Random(1)).get("ok") is None


def waiting_plan(*, hours_ago=1, window_hours=4):
    due = START - timedelta(hours=hours_ago)
    return {"day": START.date().isoformat(), "posts": [
        {"slot": "00-04", "due": due.isoformat(), "ends": (due + timedelta(hours=window_hours)).isoformat(),
         "state": "waiting", "attempts": 0}]}


def test_a_post_long_past_its_window_is_dropped(tmp_path, publisher):
    make_clip(tmp_path / "a.mp4")
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan(hours_ago=20, window_hours=4)))
    autopilot.autopilot_job(now=START, rng=random.Random(1))
    assert publisher.uploads == []
    with session_scope() as db:
        assert db.get(GrowthState, "plan").value["posts"][0]["state"] == "missed"


def test_missing_token_keeps_the_clip_and_keeps_the_window_open(tmp_path, monkeypatch):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"x")
    video_id = make_clip(path)
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: FakePublisher("YouTube token missing at x"))
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    for _ in range(5):
        autopilot.autopilot_job(now=START, rng=random.Random(1))
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.TRANSFORMED
        post = db.get(GrowthState, "plan").value["posts"][0]
        assert post["state"] == "waiting" and post["attempts"] == 0
        assert db.execute(select(GrowthEvent).where(GrowthEvent.kind == "publish_failed")).first() is None


def test_empty_stock_keeps_the_post_waiting(publisher):
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    for _ in range(5):
        assert "no finished clips" in autopilot.autopilot_job(now=START, rng=random.Random(1))["skipped"]
    with session_scope() as db:
        assert db.get(GrowthState, "plan").value["posts"][0]["state"] == "waiting"


def test_uploads_keep_a_minimum_gap(tmp_path, publisher):
    published_clip(tmp_path / "old.mp4", 1, 5, published_at=START - timedelta(minutes=10))
    path = tmp_path / "a.mp4"
    path.write_bytes(b"x")
    make_clip(path, 2)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    assert "gap" in autopilot.autopilot_job(now=START, rng=random.Random(1))["waiting"]


def test_fake_uploads_are_refused_outside_mock_mode(monkeypatch):
    monkeypatch.setattr(settings, "use_mock", False)
    monkeypatch.setattr(settings, "youtube_client_secrets", "")
    assert "fake" in autopilot.autopilot_job()["error"]


def test_old_publish_job_steps_aside():
    assert "growth" in scheduler.publish_job()["skipped"]


def test_scheduler_registers_loop_and_drops_scraping():
    sched = scheduler._make_scheduler()
    ids = {j.id for j in sched.get_jobs()}
    assert {"growth_autopilot", "growth_collect", "growth_learn", "growth_supply"} <= ids
    assert "scrape" not in ids and "analytics_pull" not in ids


def test_loop_jobs_are_absent_when_off(monkeypatch):
    monkeypatch.setattr(settings, "growth_enabled", False)
    ids = {j.id for j in scheduler._make_scheduler().get_jobs()}
    assert not any(i.startswith("growth_") for i in ids) and "scrape" in ids


# ---------- collect ----------
class FakeInsights:
    def __init__(self, stats=None, retention=None, analytics_error=None):
        self._stats, self._retention, self._error = stats or {}, retention or {}, analytics_error
        self.retention_calls = 0

    def stats(self, ids):
        return {k: v for k, v in self._stats.items() if k in ids}

    def retention(self, ids, start, end):
        self.retention_calls += 1
        if self._error:
            raise RuntimeError(self._error)
        return dict(self._retention)

    def feed_share(self, video_id, start, end):
        return 0.8


def stat(views):
    return {"views": views, "likes": 1, "comments": 0, "privacy": "public", "upload_status": "processed"}


def test_collect_records_snapshots_and_flags_vanished_videos(tmp_path, monkeypatch):
    seen = published_clip(tmp_path / "a", 1, 5, published_at=START)
    gone = published_clip(tmp_path / "b", 2, 5, published_at=START)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": stat(77)}))
    assert collect.collect_job(now=START + timedelta(hours=10))["snapshots"] == 1
    with session_scope() as db:
        assert db.get(Video, seen).views == 77
        assert db.get(Video, gone).meta["yt_missing"] is True
        assert db.execute(select(GrowthEvent).where(GrowthEvent.kind == "missing")).scalar_one()


def test_analytics_failure_does_not_lose_view_counts(tmp_path, monkeypatch):
    published_clip(tmp_path / "a", 1, 5, published_at=START)
    fake = FakeInsights({"yt1": stat(9)}, analytics_error="403 insufficient scope")
    monkeypatch.setattr(collect, "get_insights", lambda: fake)
    out = collect.collect_job(now=START + timedelta(days=3))
    assert out["snapshots"] == 1 and "403" in out["analytics_error"]
    assert "error" not in out


def test_analytics_waits_for_youtubes_delay_and_then_runs_daily(tmp_path, monkeypatch):
    video_id = published_clip(tmp_path / "a", 1, 5, published_at=START)
    fake = FakeInsights({"yt1": stat(9)}, {"yt1": {"averageViewPercentage": 70.0, "subscribersGained": 2}})
    monkeypatch.setattr(collect, "get_insights", lambda: fake)
    collect.collect_job(now=START + timedelta(days=1))
    assert fake.retention_calls == 0
    collect.collect_job(now=START + timedelta(days=3))
    collect.collect_job(now=START + timedelta(days=3, hours=3))
    assert fake.retention_calls == 1
    with session_scope() as db:
        assert db.get(Video, video_id).meta["yt_analytics"]["shorts_feed_share"] == 0.8


def test_collect_ignores_mock_uploads(tmp_path, monkeypatch):
    video_id = published_clip(tmp_path / "a", 1, 5, published_at=START)
    with session_scope() as db:
        db.get(Video, video_id).youtube_video_id = "MOCK_123"
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    assert "skipped" in collect.collect_job(now=START + timedelta(hours=5))


# ---------- the real Google clients, with the network faked ----------
def google_client(api, version, responses):
    """Built from the discovery document, so a wrong method or parameter name fails here."""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    bodies = [({"status": "200"}, json.dumps(r)) for r in responses]
    http = HttpMockSequence(bodies)
    return build(api, version, http=http, developerKey="test", cache_discovery=False, static_discovery=True), http


class StubPublisher:
    def __init__(self, data=None, analytics=None):
        self.data, self.analytics = data, analytics

    def _client(self):
        return self.data

    def analytics_client(self):
        return self.analytics


def test_insights_reads_real_response_shapes():
    from app.growth.youtube import Insights

    data, _ = google_client("youtube", "v3", [{"items": [{
        "id": "abc", "statistics": {"viewCount": "12", "likeCount": "3"},
        "status": {"privacyStatus": "public", "uploadStatus": "processed"}}]}])
    assert Insights(StubPublisher(data=data)).stats(["abc"])["abc"] == {
        "views": 12, "likes": 3, "comments": 0, "privacy": "public", "upload_status": "processed",
        "rejection_reason": "", "failure_reason": ""}

    report_body = {"columnHeaders": [{"name": "video"}, {"name": "views"}, {"name": "averageViewPercentage"}],
                   "rows": [["abc", 12, 71.5]]}
    traffic_body = {"columnHeaders": [{"name": "insightTrafficSourceType"}, {"name": "views"}],
                    "rows": [["SHORTS", 9], ["SUBSCRIBER", 3]]}
    analytics, _ = google_client("youtubeAnalytics", "v2", [report_body, traffic_body])
    insights = Insights(StubPublisher(analytics=analytics))
    day = START.date()
    assert insights.retention(["abc"], day, day) == {"abc": {"views": 12, "averageViewPercentage": 71.5}}
    assert insights.feed_share("abc", day, day) == 0.75


# ---------- supply ----------
def test_supply_takes_inbox_files_first_and_never_twice(tmp_path, monkeypatch):
    clip = tmp_path / "mine.mp4"
    clip.write_bytes(b"x")
    import os
    os.utime(clip, (0, 0))
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "growth_feeds", '["https://example.com/feed"]')
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/1", "https://example.com/2"])
    assert supply.supply_job()["queued"] == str(clip.resolve())
    with session_scope() as db:
        db.execute(select(ClipJob)).scalar_one().status = "done"
    assert supply.supply_job()["queued"] == "https://example.com/1"
    with session_scope() as db:
        job = db.execute(select(ClipJob).where(ClipJob.source == "https://example.com/1")).scalar_one()
        assert job.auto_queue is True
        job.status = "failed"
    assert supply.supply_job()["queued"] == "https://example.com/2"


def test_supply_ignores_a_file_still_being_copied(tmp_path, monkeypatch):
    (tmp_path / "copying.mp4").write_bytes(b"x")
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    assert "starved" in supply.supply_job()


def test_supply_stops_when_stocked(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_min_inventory", 1)
    make_clip(tmp_path / "a")
    assert "queued" not in supply.supply_job()


def test_a_bad_feed_does_not_stop_the_next_one(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "growth_feeds", '["bad", "good"]')

    def links(feed):
        if feed == "bad":
            raise RuntimeError("404")
        return ["https://example.com/ok"]

    monkeypatch.setattr(supply, "_feed_links", links)
    assert supply.next_source() == ("https://example.com/ok", "good")


# ---------- report ----------
def write_token(path, scopes, refresh="r"):
    path.write_text(json.dumps({"refresh_token": refresh, "scopes": scopes}))
    return str(path)


def checks_by_name():
    return {c["name"]: c for c in report.doctor()["checks"]}


def test_doctor_blocks_on_fake_uploads_and_a_missing_token(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "youtube_token_path", str(tmp_path / "none.json"))
    result = report.doctor()
    assert result["ready"] is False
    names = {c["name"] for c in result["checks"] if not c["ok"]}
    assert {"USE_MOCK", "token"} <= names


def test_doctor_catches_private_uploads_and_old_tokens(monkeypatch, tmp_path):
    from app.publishers.youtube_publisher import READ_SCOPE, UPLOAD_SCOPE
    monkeypatch.setattr(settings, "youtube_token_path", write_token(tmp_path / "t.json", [UPLOAD_SCOPE, READ_SCOPE]))
    monkeypatch.setattr(settings, "youtube_default_privacy", "private")
    checks = checks_by_name()
    assert checks["YOUTUBE_DEFAULT_PRIVACY"]["ok"] is False
    assert checks["analytics permission"]["ok"] is False and checks["analytics permission"]["required"] is False
    assert checks["upload permission"]["ok"] is True


def test_report_and_endpoints_work_on_an_empty_channel():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    assert client.get("/growth/report").json()["phase"] == "not started"
    assert "ready" in client.get("/growth/doctor").json()


# ---------- the whole loop ----------
def test_a_simulated_month_learns_the_best_window(tmp_path, monkeypatch, publisher):
    """A world where one posting window truly gets 4x the views. The loop is never told."""
    best_slot = "16-20"
    world = np.random.default_rng(7)
    monkeypatch.setattr(quota, "remaining", lambda db: 99)
    monkeypatch.setattr(settings, "growth_min_inventory", 0)
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    truth: dict[str, float] = {}

    class World(FakeInsights):
        def stats(self, ids):
            return {i: stat(int(truth[i])) for i in ids if i in truth}

    monkeypatch.setattr(collect, "get_insights", lambda: World())
    rng = random.Random(11)
    made = 0

    def tick(now):
        nonlocal made
        with session_scope() as db:
            stock = len(autopilot.ready_clips(db))
        for _ in range(4 - stock):
            made += 1
            make_clip(path, made, parent=f"s{made}")
        outcome = autopilot.autopilot_job(now=now, rng=rng)
        if outcome.get("ok"):
            with session_scope() as db:
                video = db.get(Video, outcome["video_id"])
                video.published_at = now  # publish_task stamps the real clock
                multiplier = 4.0 if factors.slot_of(now) == best_slot else 1.0
                truth[video.youtube_video_id] = 200 * multiplier * float(world.lognormal(0, 0.4))

    for day in range(40):
        tick(START + timedelta(days=day))  # the day's first tick writes the plan
        with session_scope() as db:
            dues = [datetime.fromisoformat(p["due"]) for p in db.get(GrowthState, "plan").value["posts"]
                    if p["state"] == "waiting"]
        # The real job ticks every 10 minutes, and most of those ticks do nothing.
        for due in dues:
            tick(due + timedelta(minutes=10) - timedelta(seconds=due.minute % 10 * 60 + due.second))
        collect.collect_job(now=START + timedelta(days=day, hours=23))
        learn.learn_job(now=START + timedelta(days=day, hours=23))

    with session_scope() as db:
        posted = db.execute(select(Video).where(Video.status == VideoStatus.PUBLISHED)
                            .order_by(Video.published_at)).scalars().all()
        slots = [(factors.slot_of(v.published_at), factors.aware(v.published_at)) for v in posted]
        summary = db.get(GrowthState, "summary").value
    assert len(slots) > 80
    assert {s for s, _ in slots} == set(factors.SLOTS)  # the first month really did test every window
    assert summary["slot"][0]["arm"] == best_slot and summary["slot"][0]["p_best"] > 0.9
    share = lambda rows: sum(s == best_slot for s, _ in rows) / len(rows)
    early = [r for r in slots if r[1] < START + timedelta(days=10)]
    late = [r for r in slots if r[1] >= START + timedelta(days=30)]
    assert share(late) > 0.25  # at most one post per window per day, so the ceiling is a third
    assert share(late) > share(early)


# ---------- publish safety ----------
def ready_file_clip(tmp_path, n=0, **kwargs):
    path = tmp_path / f"clip{n}.mp4"
    path.write_bytes(b"x")
    return make_clip(path, n, **kwargs)


def http_error(status, reason=None, cls=None):
    import httplib2
    from googleapiclient.errors import HttpError

    body = json.dumps({"error": {"code": status, "message": "m", "errors": [{"reason": reason}]}}) if reason else "Bad Gateway"
    return (cls or HttpError)(httplib2.Response({"status": status}), body.encode())


@pytest.fixture
def failing_sql():
    """Raise 'database is locked' on statements containing a given text, like a real lock would."""
    import sqlite3

    from sqlalchemy import event

    from app.db import engine

    hooks = []

    def arm(*fragments, times=1):
        left = {"n": times}

        def hook(conn, cursor, statement, parameters, context, executemany):
            if left["n"] and all(f in statement for f in fragments):
                left["n"] -= 1
                raise sqlite3.OperationalError("database is locked")

        event.listen(engine, "before_cursor_execute", hook)
        hooks.append(hook)

    def clear():
        while hooks:
            event.remove(engine, "before_cursor_execute", hooks.pop())

    arm.clear = clear
    yield arm
    clear()


# -- text YouTube would refuse
def test_titles_lose_angle_brackets_and_keep_the_fallback():
    from app.metadata import finished_title, youtube_title

    assert finished_title("I <3 this >>") == "I 3 this #Shorts"
    assert finished_title("<<>>") == "Watch this #Shorts"
    assert youtube_title("<<>>") == "Watch this #Shorts"
    assert youtube_title(None) == "Watch this #Shorts"
    assert youtube_title("a > b") == "a b"
    assert len(youtube_title("x" * 300)) == 100


def test_descriptions_are_capped_in_bytes_on_a_character_boundary():
    from app.metadata import youtube_description

    capped = youtube_description("\u0939" * 3000)
    assert len(capped.encode()) <= 5000 and capped == "\u0939" * 1666
    assert youtube_description("a <b> c\nd") == "a b c\nd"
    assert youtube_description(None) == ""
    # A character straddling the limit is dropped whole, and bad surrogates cannot crash the cut.
    assert youtube_description("ab\u0939", limit=3) == "ab"
    assert youtube_description("a\ud800b") == "ab"


def test_compose_output_is_accepted_by_youtube_and_keeps_the_credit():
    from app.metadata import compose

    out = compose(title="<>", description="\u0939" * 2000 + " >>> more", keywords=["k"], author="sam", platform="youtube",
                  source_url="https://example.com/v", language="hi")
    assert "<" not in out.title + out.description and ">" not in out.title + out.description
    assert len(out.description.encode()) <= 5000
    assert "Credit: sam" in out.description and "https://example.com/v" in out.description
    assert not out.title.startswith("#")


def fake_youtube(sent):
    class Request:
        def next_chunk(self):
            return None, {"id": "yt"}

    class Videos:
        def insert(self, part, body, media_body):
            sent.update(body)
            return Request()

    class Client:
        def videos(self):
            return Videos()

    return Client()


def test_upload_boundary_sends_only_what_youtube_accepts(tmp_path, monkeypatch):
    from app.publishers import YouTubePublisher

    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    sent = {}
    client = YouTubePublisher()
    monkeypatch.setattr(client, "_client", lambda: fake_youtube(sent))
    client.upload(file_path=str(clip), title="<" + "t" * 200, description="\u0939" * 3000 + ">", tags=[], category=22, privacy="public")
    assert sent["snippet"]["title"] == "t" * 100
    assert len(sent["snippet"]["description"].encode()) <= 5000 and ">" not in sent["snippet"]["description"]
    sent.clear()
    client.upload(file_path=str(clip), title="<>", description="", tags=[], category=22, privacy="public")
    assert sent["snippet"]["title"] == "Watch this #Shorts"


# -- the claim
def test_claim_records_what_is_being_sent(tmp_path, publisher):
    from tasks.pipeline import publish_task

    video_id = ready_file_clip(tmp_path)
    assert publish_task(video_id, {"title": "A <b> c", "description": "d >"})["ok"] is True
    assert publisher.uploads[0]["title"] == "A b c" and publisher.uploads[0]["description"] == "d"
    with session_scope() as db:
        meta = db.get(Video, video_id).meta
        assert meta["publish_title"] == "A b c"
        assert datetime.fromisoformat(meta["publish_claimed_at"]).tzinfo is not None


@pytest.mark.parametrize("status", [VideoStatus.PUBLISHED, VideoStatus.REJECTED, VideoStatus.PUBLISHING, VideoStatus.FAILED])
def test_a_clip_that_is_not_transformed_is_never_uploaded(tmp_path, publisher, status):
    from tasks.pipeline import publish_task

    video_id = ready_file_clip(tmp_path, status=status)
    with session_scope() as db:
        db.get(Video, video_id).youtube_video_id = "first"
    out = publish_task(video_id)
    assert out["ok"] is False and out["retryable"] is True and publisher.uploads == []
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == status and video.youtube_video_id == "first" and "publish_claimed_at" not in video.meta


def test_published_clip_with_its_file_cleaned_up_stays_published(tmp_path, publisher):
    from tasks.pipeline import publish_task

    video_id = make_clip(tmp_path / "gone.mp4", status=VideoStatus.PUBLISHED)
    assert publish_task(video_id)["ok"] is False
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PUBLISHED


def test_claim_is_decided_by_the_update_not_the_earlier_read(tmp_path, publisher, monkeypatch):
    """A reject landing between publish_task reading the row and claiming it must win."""
    from sqlalchemy import update

    from tasks import pipeline

    video_id = ready_file_clip(tmp_path)
    real_remaining = quota.remaining

    def rejected_meanwhile(db):
        db.execute(update(Video).where(Video.id == video_id).values(status=VideoStatus.REJECTED)
                   .execution_options(synchronize_session=False))
        return real_remaining(db)

    monkeypatch.setattr(pipeline.quota, "remaining", rejected_meanwhile)
    out = pipeline.publish_task(video_id)
    assert out["ok"] is False and out["retryable"] is True and publisher.uploads == []
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.REJECTED


def test_manual_publish_during_title_writing_does_not_upload_twice(tmp_path, publisher, monkeypatch):
    from tasks.pipeline import publish_task

    video_id = ready_file_clip(tmp_path)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))

    def owner_clicks_publish(style, title, hook):
        assert publish_task(video_id)["ok"] is True
        return "Autopilot title #Shorts", "plain"

    monkeypatch.setattr(titles, "styled_title", owner_clicks_publish)
    autopilot.autopilot_job(now=START, rng=random.Random(1))
    assert len(publisher.uploads) == 1
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.PUBLISHED and video.youtube_video_id == "yt1"
        assert quota.snapshot(db)["uploads"] == 1
        assert db.get(GrowthState, "plan").value["posts"][0]["state"] == "waiting"


def test_manual_publish_through_the_api_still_works(tmp_path, publisher, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from tasks import pipeline

    class NoBroker:
        def delay(self, *args):
            raise RuntimeError("no broker")

    monkeypatch.setattr(pipeline, "publish_task_async", NoBroker())
    video_id = ready_file_clip(tmp_path)
    client = TestClient(app)
    assert client.post(f"/videos/{video_id}/publish", json={"title": "Mine"}).json()["result"]["ok"] is True
    assert publisher.uploads[0]["title"] == "Mine"
    assert client.post(f"/videos/{video_id}/publish", json={}).status_code == 400


def test_an_upload_in_flight_holds_back_the_next_post(tmp_path, publisher):
    ready_file_clip(tmp_path, 1)
    stuck_row(tmp_path, 2, claimed_at=START - timedelta(minutes=5))
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    assert "gap" in autopilot.autopilot_job(now=START, rng=random.Random(1))["waiting"]
    assert publisher.uploads == []


# -- after the upload
def test_quota_failure_after_upload_keeps_the_video_published(tmp_path, publisher, failing_sql):
    from tasks.pipeline import publish_task

    video_id = ready_file_clip(tmp_path)
    with session_scope() as db:
        quota.remaining(db)
    failing_sql("UPDATE quota_usage")
    out = publish_task(video_id)
    assert out["ok"] is True and out["youtube_id"] == "yt1"
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.PUBLISHED and video.youtube_video_id == "yt1" and video.published_at
        assert video.error is None


def test_duplicate_creators_cannot_fail_a_live_video_or_lose_its_quota(tmp_path, publisher):
    from app.models import Creator
    from tasks.pipeline import publish_task

    video_id = ready_file_clip(tmp_path, author="twin")
    with session_scope() as db:
        db.add_all([Creator(handle="twin", platform="youtube"), Creator(handle="twin", platform="youtube")])
    assert publish_task(video_id)["ok"] is True
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PUBLISHED
        assert quota.snapshot(db)["uploads"] == 1


def test_recording_the_upload_retries_through_a_lock(tmp_path, publisher, failing_sql, monkeypatch):
    from tasks import pipeline

    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    video_id = ready_file_clip(tmp_path)
    failing_sql("UPDATE videos", "youtube_video_id", times=2)
    assert pipeline.publish_task(video_id)["ok"] is True
    assert len(publisher.uploads) == 1
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.PUBLISHED and video.youtube_video_id == "yt1"
        assert quota.snapshot(db)["uploads"] == 1


def test_an_upload_that_cannot_be_recorded_logs_the_id_and_is_never_reuploaded(tmp_path, publisher, failing_sql, monkeypatch, caplog):
    from tasks import pipeline

    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    video_id = ready_file_clip(tmp_path)
    failing_sql("UPDATE videos", "youtube_video_id", times=99)
    with caplog.at_level("ERROR", logger="tasks.pipeline"):
        out = pipeline.publish_task(video_id)
    assert out["ok"] is True and out["youtube_id"] == "yt1" and out["recorded"] is False
    assert "yt1" in caplog.text
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.PUBLISHING and video.error is None  # recovery finds it by title
        assert quota.snapshot(db)["uploads"] == 0
        assert autopilot.ready_clips(db) == []


# -- classification
@pytest.mark.parametrize("status,reason,retryable,account", [
    (500, None, True, False), (502, None, True, False), (503, "backendError", True, False),
    (429, None, True, False), (429, "rateLimitExceeded", True, False),
    (401, "authError", True, True), (401, "youtubeSignupRequired", True, True), (401, None, True, True),
    (403, "accessNotConfigured", True, True), (403, "forbidden", True, True), (403, "quotaExceeded", True, True),
    (403, "accountSuspended", True, True), (403, None, True, True),
    (400, "uploadLimitExceeded", True, True),
    (400, "invalidTitle", False, False), (400, "invalidDescription", False, False),
    (400, "mediaBodyRequired", False, False), (400, "invalidVideoMetadata", False, False),
    (400, "someReasonNobodyListed", True, False), (400, None, True, False), (404, "videoNotFound", True, False),
])
def test_http_errors_are_classified_by_status_and_reason(status, reason, retryable, account):
    from googleapiclient.errors import ResumableUploadError

    from tasks.pipeline import classify_publish_error

    for cls in (None, ResumableUploadError):
        assert classify_publish_error(http_error(status, reason, cls)) == {"retryable": retryable, "account_level": account}


def test_everything_that_is_not_an_http_error_is_retryable():
    import httplib2
    from google.auth.exceptions import RefreshError

    from tasks.pipeline import classify_publish_error

    for exc in (httplib2.ServerNotFoundError("Unable to find the server at x"), BrokenPipeError("Broken pipe"),
                OSError("Network is unreachable"), RefreshError("invalid_grant: Token has been revoked"),
                TimeoutError("timed out"), RuntimeError("anything else")):
        assert classify_publish_error(exc)["retryable"] is True


def test_a_reason_in_an_unreadable_body_does_not_mean_failed():
    import httplib2
    from googleapiclient.errors import HttpError

    from tasks.pipeline import classify_publish_error

    for body in (b"<html>", b"[]", b'{"error": "x"}', b'{"error": {"errors": ["x"]}}', b"\xff"):
        assert classify_publish_error(HttpError(httplib2.Response({"status": 400}), body))["retryable"] is True


def test_string_classifier_still_answers_for_old_callers():
    from tasks.pipeline import _is_retryable_publish_error

    assert _is_retryable_publish_error("YouTube token missing at x") is True
    assert _is_retryable_publish_error("YouTube token refresh failed: invalid_grant") is True
    assert _is_retryable_publish_error("quotaExceeded") is True
    assert _is_retryable_publish_error("invalidTitle") is False


@pytest.mark.parametrize("error", [
    http_error(403, "accessNotConfigured"), http_error(401, "youtubeSignupRequired"), http_error(502),
    http_error(429), OSError("Network is unreachable"),
])
def test_account_and_network_errors_keep_the_clip(tmp_path, monkeypatch, error):
    from tasks.pipeline import publish_task

    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: FakePublisher(error))
    video_id = ready_file_clip(tmp_path)
    out = publish_task(video_id)
    assert out["ok"] is False and out["retryable"] is True
    assert out["account_level"] is (getattr(getattr(error, "resp", None), "status", 0) in (401, 403))
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.TRANSFORMED and video.error


def test_only_a_listed_video_reason_ends_in_failed(tmp_path, monkeypatch):
    from tasks.pipeline import publish_task

    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: FakePublisher(http_error(400, "invalidVideoMetadata")))
    video_id = ready_file_clip(tmp_path)
    out = publish_task(video_id)
    assert out["ok"] is False and out["retryable"] is False and out["account_level"] is False
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.FAILED


def test_an_account_outage_does_not_burn_the_stock(tmp_path, monkeypatch):
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: FakePublisher(http_error(403, "accessNotConfigured")))
    ids = [ready_file_clip(tmp_path, n) for n in range(3)]
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    for _ in range(3):
        autopilot.autopilot_job(now=START, rng=random.Random(1))
    with session_scope() as db:
        assert {db.get(Video, i).status for i in ids} == {VideoStatus.TRANSFORMED}


# -- rows stuck in PUBLISHING
def stuck_row(tmp_path, n=0, *, claimed_at=None, title="Stuck title #Shorts", **meta):
    video_id = ready_file_clip(tmp_path, n, status=VideoStatus.PUBLISHING, **meta)
    if claimed_at:
        with session_scope() as db:
            video = db.get(Video, video_id)
            video.meta = {**video.meta, "publish_claimed_at": claimed_at.isoformat(), "publish_title": title}
    return video_id


class FakeUploads:
    def __init__(self, uploads=(), error=None):
        self.uploads, self.error, self.calls = list(uploads), error, []

    def uploads_since(self, since=None):
        self.calls.append(since)
        if self.error:
            raise self.error
        return self.uploads


@pytest.fixture
def real_mode(monkeypatch):
    def install(insights):
        monkeypatch.setattr(settings, "use_mock", False)
        monkeypatch.setattr("app.growth.youtube.get_insights", lambda: insights)
        return insights
    return install


def recover(now=START):
    from tasks.pipeline import recover_stuck_publishes

    return recover_stuck_publishes(now)


def events(kind):
    with session_scope() as db:
        return db.execute(select(GrowthEvent).where(GrowthEvent.kind == kind)).scalars().all()


def test_stuck_upload_found_on_the_channel_becomes_published(tmp_path, real_mode):
    video_id = stuck_row(tmp_path, claimed_at=START - timedelta(hours=3))
    insights = real_mode(FakeUploads([
        {"id": "other", "title": "Something else", "published_at": START - timedelta(hours=2)},
        {"id": "found", "title": "Stuck title #Shorts", "published_at": START - timedelta(hours=2, minutes=50)},
    ]))
    assert recover()["published"] == 1
    assert insights.calls and insights.calls[0] <= START - timedelta(hours=3)
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.PUBLISHED and video.youtube_video_id == "found"
        assert factors.aware(video.published_at) == START - timedelta(hours=2, minutes=50)
    assert len(events("publish_recovered")) == 1


def test_stuck_upload_not_on_the_channel_goes_back_on_the_shelf(tmp_path, real_mode):
    video_id = stuck_row(tmp_path, claimed_at=START - timedelta(hours=3))
    real_mode(FakeUploads([{"id": "x", "title": "Something else", "published_at": START}]))
    assert recover()["released"] == 1
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.TRANSFORMED and video.youtube_video_id is None
        assert "publish_claimed_at" not in video.meta
    assert len(events("publish_recovered")) == 1


def test_recovery_reads_the_real_uploads_playlist_shape(tmp_path, monkeypatch):
    video_id = stuck_row(tmp_path, claimed_at=START - timedelta(hours=3))
    client, _ = uploads_client(CHANNEL_REPLY, (200, {"items": [
        playlist_item("found", "Stuck title #Shorts", START - timedelta(hours=2))]}))
    monkeypatch.setattr(settings, "use_mock", False)
    monkeypatch.setattr("app.growth.youtube.get_publisher", lambda: StubPublisher(data=client))
    assert recover()["published"] == 1
    with session_scope() as db:
        assert db.get(Video, video_id).youtube_video_id == "found"


def test_failed_lookup_leaves_the_row_and_says_so_once(tmp_path, real_mode):
    video_id = stuck_row(tmp_path, claimed_at=START - timedelta(hours=3))
    real_mode(FakeUploads(error=OSError("Network is unreachable")))
    for _ in range(3):
        assert recover()["left"] == 1
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PUBLISHING
    assert len(events("publish_stuck")) == 1

    real_mode(FakeUploads([]))  # the next try, once it can look, settles it
    assert recover()["released"] == 1
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.TRANSFORMED


def test_no_way_to_look_leaves_the_row(tmp_path, real_mode):
    video_id = stuck_row(tmp_path, claimed_at=START - timedelta(hours=3))
    real_mode(None)
    assert recover()["left"] == 1
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PUBLISHING
    assert len(events("publish_stuck")) == 1


def test_a_recent_claim_is_left_alone(tmp_path, real_mode):
    video_id = stuck_row(tmp_path, claimed_at=START - timedelta(minutes=30))
    insights = real_mode(FakeUploads([]))
    assert recover() == {"published": 0, "released": 0, "left": 0}
    assert insights.calls == []
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PUBLISHING


def test_a_row_with_no_claim_stamp_is_looked_up_by_its_draft_title(tmp_path, real_mode):
    video_id = stuck_row(tmp_path)
    with session_scope() as db:
        db.add(MetadataDraft(video_id=video_id, title="Draft <title>", description="d", tags=[]))
    insights = real_mode(FakeUploads([{"id": "found", "title": "Draft title", "published_at": START - timedelta(days=9)}]))
    assert recover()["published"] == 1
    assert insights.calls == [None]  # no claim time, so it must search the whole list
    with session_scope() as db:
        assert db.get(Video, video_id).youtube_video_id == "found"


def test_row_with_no_stamp_and_nothing_to_search_for_is_left(tmp_path, real_mode):
    video_id = stuck_row(tmp_path)
    with session_scope() as db:
        db.get(Video, video_id).title = ""
    real_mode(FakeUploads([{"id": "x", "title": "#Shorts", "published_at": START}]))
    assert recover()["left"] == 1
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PUBLISHING


def test_a_same_title_that_cannot_be_this_upload_is_not_a_match(tmp_path, real_mode):
    """Older than the claim, or already another row's video: claiming it would lose the real one."""
    published = published_clip(tmp_path / "old.mp4", 7, 5)
    with session_scope() as db:
        db.get(Video, published).youtube_video_id = "taken"
    video_id = stuck_row(tmp_path, claimed_at=START - timedelta(hours=3))
    real_mode(FakeUploads([
        {"id": "taken", "title": "Stuck title #Shorts", "published_at": START - timedelta(hours=1)},
        {"id": "older", "title": "Stuck title #Shorts", "published_at": START - timedelta(days=4)},
    ]))
    assert recover()["released"] == 1
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.TRANSFORMED


def test_two_stuck_rows_with_one_title_do_not_share_a_video(tmp_path, real_mode):
    first = stuck_row(tmp_path, 1, claimed_at=START - timedelta(hours=3))
    second = stuck_row(tmp_path, 2, claimed_at=START - timedelta(hours=3))
    real_mode(FakeUploads([{"id": "only", "title": "Stuck title #Shorts", "published_at": START - timedelta(hours=2)}]))
    assert recover() == {"published": 1, "released": 1, "left": 0}
    with session_scope() as db:
        assert sorted(str(db.get(Video, i).youtube_video_id) for i in (first, second)) == ["None", "only"]


def test_recovered_upload_counts_towards_quota_only_on_its_own_day(tmp_path, real_mode):
    today = datetime.now(timezone.utc)
    stuck_row(tmp_path, 1, claimed_at=today - timedelta(hours=3), title="Today")
    stuck_row(tmp_path, 2, claimed_at=today - timedelta(days=3), title="Earlier")
    real_mode(FakeUploads([{"id": "a", "title": "Today", "published_at": today},
                           {"id": "b", "title": "Earlier", "published_at": today - timedelta(days=3)}]))
    assert recover(today)["published"] == 2
    with session_scope() as db:
        assert quota.snapshot(db)["uploads"] == 1


def test_mock_mode_and_mock_ids_go_straight_back_to_transformed(tmp_path, real_mode, monkeypatch):
    mock_row = stuck_row(tmp_path, 1, claimed_at=START - timedelta(hours=3))
    assert recover()["released"] == 1  # conftest forces mock mode, so nothing real could have gone out
    with session_scope() as db:
        assert db.get(Video, mock_row).status == VideoStatus.TRANSFORMED

    fake_id = stuck_row(tmp_path, 2, claimed_at=START - timedelta(hours=3))
    with session_scope() as db:
        db.get(Video, fake_id).youtube_video_id = "MOCK_1"
    insights = real_mode(FakeUploads(error=OSError("unused")))
    assert recover()["released"] == 1
    assert insights.calls == []
    with session_scope() as db:
        video = db.get(Video, fake_id)
        assert video.status == VideoStatus.TRANSFORMED and video.youtube_video_id is None


def test_autopilot_recovers_before_it_posts(tmp_path, publisher):
    stuck = stuck_row(tmp_path, 1, claimed_at=START - timedelta(hours=3))
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    outcome = autopilot.autopilot_job(now=START, rng=random.Random(1))
    assert outcome["ok"] is True and outcome["video_id"] == stuck  # released, then posted by the same tick


def test_a_recovery_crash_does_not_stop_the_tick(tmp_path, publisher, monkeypatch):
    def boom(now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("tasks.pipeline.recover_stuck_publishes", boom)
    ready_file_clip(tmp_path)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    assert autopilot.autopilot_job(now=START, rng=random.Random(1))["ok"] is True


class IdleScheduler:
    running = False

    def start(self):
        pass


def test_start_scheduler_sweeps_stuck_rows_before_jobs_start(monkeypatch):
    order = []
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "schedule_catchup", lambda sched: None)
    sched = IdleScheduler()
    monkeypatch.setattr(sched, "start", lambda: order.append("start"), raising=False)
    monkeypatch.setattr(scheduler, "_make_scheduler", lambda: sched)
    monkeypatch.setattr("tasks.pipeline.recover_stuck_publishes", lambda now=None: order.append("recover") or {})
    scheduler.start_scheduler()
    assert order == ["recover", "start"]


def test_start_scheduler_survives_a_recovery_crash(monkeypatch):
    def boom(now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "schedule_catchup", lambda sched: None)
    monkeypatch.setattr(scheduler, "_make_scheduler", IdleScheduler)
    monkeypatch.setattr("tasks.pipeline.recover_stuck_publishes", boom)
    assert isinstance(scheduler.start_scheduler(), IdleScheduler)


def uploads_client(*replies):
    """A real discovery-built client answering from canned (status, body) pairs."""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    http = HttpMockSequence([({"status": str(status)}, json.dumps(body)) for status, body in replies])
    return build("youtube", "v3", http=http, developerKey="test", cache_discovery=False, static_discovery=True), http


CHANNEL_REPLY = (200, {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUabc"}}}]})


def playlist_item(video_id, title, when):
    return {"snippet": {"title": title, "publishedAt": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}}}


def test_uploads_are_read_from_the_channels_uploads_playlist():
    from app.growth.youtube import Insights

    client, http = uploads_client(CHANNEL_REPLY, (200, {"items": [playlist_item("a", "A", START)], "nextPageToken": "n2"}),
                                  (200, {"items": [playlist_item("b", "B", START - timedelta(days=9))], "nextPageToken": "n3"}))
    uploads = Insights(StubPublisher(data=client)).uploads_since(START - timedelta(days=1))
    assert [u["id"] for u in uploads] == ["a", "b"] and uploads[0]["title"] == "A"
    assert uploads[0]["published_at"] == START
    assert "playlistId=UUabc" in http.request_sequence[1][0]
    assert "pageToken=n2" in http.request_sequence[2][0]
    assert len(http.request_sequence) == 3  # the second page reached past the claim, so it stops


def test_a_channel_with_no_uploads_answers_rather_than_fails():
    from app.growth.youtube import Insights

    client, _ = uploads_client(CHANNEL_REPLY, (404, {"error": {"errors": [{"reason": "playlistNotFound"}]}}))
    assert Insights(StubPublisher(data=client)).uploads_since(None) == []


@pytest.mark.parametrize("replies", [
    [(200, {"items": []})],  # no channel on this account
    [CHANNEL_REPLY, (500, {"error": {"errors": [{"reason": "backendError"}]}})],
    [CHANNEL_REPLY, (200, {"items": [], "nextPageToken": "n2"}), (404, {"error": {"errors": [{"reason": "playlistNotFound"}]}})],
])
def test_uploads_lookup_raises_rather_than_claiming_not_found(replies):
    from app.growth.youtube import Insights

    client, _ = uploads_client(*replies)
    with pytest.raises(Exception):
        Insights(StubPublisher(data=client)).uploads_since(None)


def test_uploads_lookup_too_long_to_read_is_not_a_not_found(monkeypatch):
    from app.growth import youtube

    monkeypatch.setattr(youtube, "_UPLOAD_PAGES", 2)
    page = (200, {"items": [playlist_item("a", "A", START)], "nextPageToken": "more"})
    client, _ = uploads_client(CHANNEL_REPLY, page, page)
    with pytest.raises(RuntimeError):
        youtube.Insights(StubPublisher(data=client)).uploads_since(None)


# -- the token
TOKEN = {"token": "old", "refresh_token": "r", "client_id": "c", "client_secret": "s",
         "token_uri": "https://oauth2.googleapis.com/token", "expiry": "2020-01-01T00:00:00Z"}


@pytest.fixture
def token_publisher(tmp_path, monkeypatch):
    from google_auth_oauthlib.flow import InstalledAppFlow

    from app.publishers import YouTubePublisher

    def no_browser(*args, **kwargs):
        raise AssertionError("the runtime publisher must never start a browser login")

    monkeypatch.setattr(InstalledAppFlow, "from_client_secrets_file", no_browser)
    monkeypatch.setattr(InstalledAppFlow, "run_local_server", no_browser)
    client = YouTubePublisher()
    client.token_path = str(tmp_path / "token.json")
    return client


@pytest.mark.parametrize("contents", [
    None,
    '{"token": "a", "refresh',  # cut off by a crash mid-write
    "",
    json.dumps({**TOKEN, "refresh_token": None}),  # expired, and nothing to renew it with
    json.dumps({k: v for k, v in TOKEN.items() if k != "refresh_token"}),
])
def test_bad_token_never_opens_a_browser_and_stays_retryable(token_publisher, contents):
    from tasks.pipeline import _is_retryable_publish_error, classify_publish_error

    if contents is not None:
        open(token_publisher.token_path, "w").write(contents)
    with pytest.raises(RuntimeError, match=r"token missing or invalid.*youtube_oauth\.py --force") as caught:
        token_publisher._credentials()
    assert classify_publish_error(caught.value)["retryable"] is True
    assert _is_retryable_publish_error(str(caught.value)) is True


def test_a_failed_refresh_stays_retryable_and_leaves_the_token_alone(token_publisher, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    def refused(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", refused)
    contents = json.dumps(TOKEN)
    open(token_publisher.token_path, "w").write(contents)
    with pytest.raises(RuntimeError, match="token refresh failed"):
        token_publisher._credentials()
    assert open(token_publisher.token_path).read() == contents


def renews(monkeypatch):
    from google.oauth2.credentials import Credentials

    def refresh(self, request):
        self.token = "new"
        self.expiry = datetime(2099, 1, 1)

    monkeypatch.setattr(Credentials, "refresh", refresh)


def test_refreshed_token_is_saved_privately_and_atomically(token_publisher, monkeypatch):
    import os
    import stat as stat_module

    renews(monkeypatch)
    open(token_publisher.token_path, "w").write(json.dumps(TOKEN))
    os.chmod(token_publisher.token_path, 0o644)
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((a, b)), real_replace(a, b))[1])
    assert token_publisher._credentials().token == "new"
    assert [b for _, b in replaced] == [token_publisher.token_path]
    assert json.load(open(token_publisher.token_path))["token"] == "new"
    assert stat_module.S_IMODE(os.stat(token_publisher.token_path).st_mode) == 0o600
    assert set(os.listdir(os.path.dirname(token_publisher.token_path))) == {"token.json"}


def test_a_crash_while_saving_leaves_the_old_token_whole(token_publisher, monkeypatch):
    import os

    renews(monkeypatch)
    contents = json.dumps(TOKEN)
    open(token_publisher.token_path, "w").write(contents)

    def full_disk(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "replace", full_disk)
    with pytest.raises(OSError):
        token_publisher._credentials()
    assert open(token_publisher.token_path).read() == contents
    assert set(os.listdir(os.path.dirname(token_publisher.token_path))) == {"token.json"}


def test_a_token_with_no_access_token_is_renewed_rather_than_rejected(token_publisher, monkeypatch):
    renews(monkeypatch)
    token = {k: v for k, v in TOKEN.items() if k not in ("token", "expiry")}
    open(token_publisher.token_path, "w").write(json.dumps(token))
    assert token_publisher._credentials().token == "new"


# -- the quota row for the day
def test_first_quota_calls_of_a_day_racing_leave_one_row(tmp_path):
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    QuotaUsage.__table__.create(engine)
    errors = []

    def first_call(barrier):
        barrier.wait()
        try:
            with Session(engine) as db:
                quota.remaining(db)
                db.commit()
        except Exception as exc:
            errors.append(exc)

    for _ in range(25):
        with Session(engine) as db:
            db.execute(delete(QuotaUsage))
            db.commit()
        barrier = threading.Barrier(6)
        threads = [threading.Thread(target=first_call, args=(barrier,)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with Session(engine) as db:
            assert len(db.execute(select(QuotaUsage)).scalars().all()) == 1
    engine.dispose()
    assert errors == []


def test_duplicate_quota_rows_cannot_stop_a_day_of_posting(tmp_path, publisher):
    from tasks.pipeline import publish_task

    with session_scope() as db:
        today = quota._today()
        db.add_all([QuotaUsage(day=today, uploads=1, cap=50), QuotaUsage(day=today, uploads=4, cap=50)])
    assert publish_task(ready_file_clip(tmp_path))["ok"] is True
    with session_scope() as db:
        assert quota.snapshot(db)["uploads"] == 5  # the fullest row, so the cap is never understated
        assert quota.remaining(db) == 45


# -- crashes when the loop is off
def legacy_mode(monkeypatch, *, auto_publish=True):
    monkeypatch.setattr(settings, "growth_enabled", False)
    monkeypatch.setattr(settings, "auto_publish", auto_publish)
    monkeypatch.setattr(settings, "upload_jitter_minutes", 0)
    monkeypatch.setattr(settings, "uploads_per_day", 8)


@pytest.mark.parametrize("auto_publish", [True, False])
def test_publish_job_settles_a_stuck_row_when_the_loop_is_off(tmp_path, real_mode, monkeypatch, auto_publish):
    legacy_mode(monkeypatch, auto_publish=auto_publish)
    claimed = datetime.now(timezone.utc) - timedelta(hours=3)
    real_mode(FakeUploads([{"id": "live1", "title": "Stuck title #Shorts", "published_at": claimed}]))
    stuck = stuck_row(tmp_path, claimed_at=claimed)
    scheduler.publish_job()
    with session_scope() as db:
        video = db.get(Video, stuck)
        assert video.status == VideoStatus.PUBLISHED and video.youtube_video_id == "live1"


def test_publish_job_leaves_a_fresh_claim_for_a_later_run(tmp_path, real_mode, monkeypatch):
    legacy_mode(monkeypatch)
    insights = real_mode(FakeUploads())
    stuck = stuck_row(tmp_path, claimed_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert scheduler.publish_job() == {"skipped": "no transformed videos ready"}
    assert insights.calls == []
    with session_scope() as db:
        assert db.get(Video, stuck).status == VideoStatus.PUBLISHING


def test_a_recovery_crash_does_not_stop_publish_job(tmp_path, publisher, monkeypatch):
    def boom(now=None):
        raise RuntimeError("boom")

    legacy_mode(monkeypatch)
    monkeypatch.setattr("tasks.pipeline.recover_stuck_publishes", boom)
    ready_file_clip(tmp_path)
    assert scheduler.publish_job()["ok"] is True


# -- a recovered upload was the plan's post
def read_plan():
    with session_scope() as db:
        return db.get(GrowthState, "plan").value


def real_autopilot(monkeypatch, publisher):
    monkeypatch.setattr("app.growth.autopilot.get_publisher", lambda: publisher)


def test_recovered_upload_fills_its_post_instead_of_a_second_clip(tmp_path, publisher, real_mode, monkeypatch):
    real_autopilot(monkeypatch, publisher)
    claimed = START - timedelta(hours=2, minutes=5)
    real_mode(FakeUploads([{"id": "live1", "title": "Stuck title #Shorts", "published_at": START - timedelta(hours=2)}]))
    stuck = stuck_row(tmp_path, claimed_at=claimed)
    ready_file_clip(tmp_path, 1)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan(hours_ago=2.2)))
    autopilot.autopilot_job(now=START, rng=random.Random(1))
    assert publisher.uploads == []
    post = read_plan()["posts"][0]
    assert post["state"] == "posted" and post["video_id"] == stuck


def test_startup_recovery_fills_the_post_too(tmp_path, real_mode):
    claimed = START - timedelta(hours=3)
    real_mode(FakeUploads([{"id": "live1", "title": "Stuck title #Shorts", "published_at": claimed}]))
    stuck = stuck_row(tmp_path, claimed_at=claimed)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan(hours_ago=4, window_hours=6)))
    recover()
    assert read_plan()["posts"][0]["video_id"] == stuck


def test_recovered_upload_from_before_a_post_was_due_leaves_it_waiting(tmp_path, real_mode):
    claimed = START - timedelta(hours=3)
    real_mode(FakeUploads([{"id": "live1", "title": "Stuck title #Shorts", "published_at": claimed}]))
    stuck_row(tmp_path, claimed_at=claimed)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan(hours_ago=1)))
    recover()
    assert read_plan()["posts"][0]["state"] == "waiting"


def test_recovered_upload_fills_only_one_post(tmp_path, real_mode):
    claimed = START - timedelta(hours=3)
    real_mode(FakeUploads([{"id": "live1", "title": "Stuck title #Shorts", "published_at": claimed}]))
    stuck_row(tmp_path, claimed_at=claimed)
    plan = waiting_plan(hours_ago=4, window_hours=6)
    plan["posts"].append({**plan["posts"][0], "slot": "04-08"})
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=plan))
    recover()
    assert [p["state"] for p in read_plan()["posts"]] == ["posted", "waiting"]


def test_a_broken_plan_does_not_undo_a_recovery(tmp_path, real_mode):
    claimed = START - timedelta(hours=3)
    real_mode(FakeUploads([{"id": "live1", "title": "Stuck title #Shorts", "published_at": claimed}]))
    stuck = stuck_row(tmp_path, claimed_at=claimed)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value={"posts": [{"state": "waiting"}]}))
    assert recover()["published"] == 1
    with session_scope() as db:
        assert db.get(Video, stuck).status == VideoStatus.PUBLISHED


# -- an upload whose reply was lost
class UploadRequest:
    def __init__(self, resumable_uri, error):
        self.resumable_uri, self.error = resumable_uri, error

    def next_chunk(self):
        raise self.error


def upload_with(monkeypatch, tmp_path, request):
    from app.publishers import YouTubePublisher

    class Videos:
        def insert(self, part, body, media_body):
            return request

    class Client:
        def videos(self):
            return Videos()

    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    publisher = YouTubePublisher()
    monkeypatch.setattr(publisher, "_client", Client)
    return publisher.upload(file_path=str(clip), title="t", description="", tags=[], category=22, privacy="public")


@pytest.mark.parametrize("error", [TimeoutError("timed out"), ConnectionResetError("reset"), http_error(502), http_error(503, "backendError")])
def test_a_failure_while_sending_the_file_leaves_the_outcome_unknown(tmp_path, monkeypatch, error):
    from app.publishers.base import UploadOutcomeUnknown

    with pytest.raises(UploadOutcomeUnknown) as caught:
        upload_with(monkeypatch, tmp_path, UploadRequest("https://upload/session", error))
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("uri, error", [
    (None, TimeoutError("timed out")),  # the session never opened, so no file was sent
    (None, http_error(503, "backendError")),
    ("https://upload/session", http_error(400, "invalidVideoMetadata")),  # YouTube answered, and the answer was no
    ("https://upload/session", http_error(403, "quotaExceeded")),
])
def test_a_failure_with_a_clear_answer_is_passed_on_as_it_is(tmp_path, monkeypatch, uri, error):
    with pytest.raises(type(error)) as caught:
        upload_with(monkeypatch, tmp_path, UploadRequest(uri, error))
    assert caught.value is error


class ReplyLostPublisher(FakePublisher):
    """YouTube took the file and the answer never arrived."""

    def upload(self, **kwargs):
        from app.publishers.base import UploadOutcomeUnknown

        self.uploads.append(kwargs)
        raise UploadOutcomeUnknown("upload outcome unknown: timed out")


def age_claim(video_id, claimed):
    with session_scope() as db:
        video = db.get(Video, video_id)
        video.meta = {**video.meta, "publish_claimed_at": claimed.isoformat()}


def lost_reply_tick(tmp_path, monkeypatch):
    fake = ReplyLostPublisher()
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    monkeypatch.setattr("app.growth.autopilot.get_publisher", lambda: fake)
    ids = [ready_file_clip(tmp_path, n) for n in range(2)]
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    assert autopilot.autopilot_job(now=START, rng=random.Random(1))["ok"] is False
    with session_scope() as db:
        held = [i for i in ids if db.get(Video, i).status == VideoStatus.PUBLISHING]
    assert len(held) == 1
    age_claim(held[0], START)
    return fake, held[0]


def test_a_lost_reply_never_uploads_again_while_the_channel_is_unchecked(tmp_path, monkeypatch, real_mode):
    insights = real_mode(FakeUploads())
    fake, held = lost_reply_tick(tmp_path, monkeypatch)
    autopilot.autopilot_job(now=START + timedelta(minutes=10), rng=random.Random(2))
    assert len(fake.uploads) == 1 and insights.calls == []
    assert read_plan()["posts"][0]["state"] == "waiting"


def test_a_lost_reply_found_on_the_channel_is_the_post(tmp_path, monkeypatch, real_mode):
    real_mode(FakeUploads([{"id": "live1", "title": "Clip 0 #Shorts", "published_at": START}]))
    fake, held = lost_reply_tick(tmp_path, monkeypatch)
    with session_scope() as db:
        video = db.get(Video, held)
        video.meta = {**video.meta, "publish_title": "Clip 0 #Shorts"}
    autopilot.autopilot_job(now=START + timedelta(hours=2, minutes=1), rng=random.Random(2))
    assert len(fake.uploads) == 1
    with session_scope() as db:
        video = db.get(Video, held)
        assert video.status == VideoStatus.PUBLISHED and video.youtube_video_id == "live1" and video.error is None
    assert read_plan()["posts"][0]["video_id"] == held


def test_a_lost_reply_not_on_the_channel_goes_back_on_the_shelf(tmp_path, monkeypatch, real_mode):
    real_mode(FakeUploads())
    _, held = lost_reply_tick(tmp_path, monkeypatch)
    from tasks.pipeline import recover_stuck_publishes

    assert recover_stuck_publishes(START + timedelta(hours=2, minutes=1))["released"] == 1
    with session_scope() as db:
        assert db.get(Video, held).status == VideoStatus.TRANSFORMED


# -- a clip that keeps failing
class PoisonPublisher(FakePublisher):
    def __init__(self, bad_path, error):
        super().__init__()
        self.bad_path, self.bad_error, self.attempts = str(bad_path), error, []

    def upload(self, **kwargs):
        self.attempts.append(kwargs["file_path"])
        if kwargs["file_path"] == self.bad_path:
            raise self.bad_error
        return super().upload(**kwargs)


def queued_clips(tmp_path, count=3):
    ids = [ready_file_clip(tmp_path, n, parent=f"p{n}") for n in range(count)]
    with session_scope() as db:
        for n, video_id in enumerate(ids):
            db.get(Video, video_id).transformed_at = START + timedelta(minutes=n)
    return ids


def test_one_bad_clip_cannot_block_the_publish_job(tmp_path, monkeypatch):
    legacy_mode(monkeypatch)
    ids = queued_clips(tmp_path)
    fake = PoisonPublisher(tmp_path / "clip0.mp4", PermissionError("denied"))
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    for _ in range(3):
        scheduler.publish_job()
    assert [a.rsplit("/", 1)[-1] for a in fake.attempts] == ["clip0.mp4", "clip1.mp4", "clip2.mp4"]
    with session_scope() as db:
        assert db.get(Video, ids[0]).status == VideoStatus.TRANSFORMED  # kept, and tried again when its turn comes
    scheduler.publish_job()
    assert fake.attempts[-1].endswith("clip0.mp4")


def test_one_bad_clip_cannot_block_the_loop(tmp_path, monkeypatch):
    ids = queued_clips(tmp_path)
    fake = PoisonPublisher(tmp_path / "clip0.mp4", http_error(400, "badRequest"))
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=waiting_plan()))
    outcomes = [autopilot.autopilot_job(now=START + timedelta(minutes=m), rng=random.Random(m)) for m in (0, 10)]
    assert outcomes[0]["ok"] is False and outcomes[1]["ok"] is True
    assert fake.attempts[0].endswith("clip0.mp4") and not fake.attempts[1].endswith("clip0.mp4")
    with session_scope() as db:
        assert db.get(Video, ids[0]).status == VideoStatus.TRANSFORMED


def test_the_avoided_source_does_not_push_the_bad_clip_back_to_the_front(tmp_path, monkeypatch):
    ids = queued_clips(tmp_path, 2)
    published_clip(tmp_path / "old.mp4", 9, 100, parent="p1", published_at=START - timedelta(days=1))
    with session_scope() as db:
        video = db.get(Video, ids[0])
        video.meta = {**video.meta, "publish_failures": 1}
        clips = autopilot.ready_clips(db)
        assert autopilot.pick_clip(clips, policy(), "p1").id == ids[1]


def test_when_every_clip_has_failed_they_all_get_turns(tmp_path, monkeypatch):
    legacy_mode(monkeypatch)
    queued_clips(tmp_path)
    fake = FakePublisher(TimeoutError("timed out"))
    tried = []

    def upload(**kwargs):
        tried.append(kwargs["file_path"].rsplit("/", 1)[-1])
        raise TimeoutError("timed out")

    monkeypatch.setattr(fake, "upload", upload)
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    for _ in range(4):
        scheduler.publish_job()
    assert tried == ["clip0.mp4", "clip1.mp4", "clip2.mp4", "clip0.mp4"]


def test_an_account_outage_does_not_mark_clips_as_bad(tmp_path, monkeypatch):
    from tasks.pipeline import publish_task

    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: FakePublisher(http_error(403, "accessNotConfigured")))
    video_id = ready_file_clip(tmp_path)
    publish_task(video_id)
    with session_scope() as db:
        assert "publish_failures" not in db.get(Video, video_id).meta


class ScriptedHttp:
    """Answers the real resumable upload request: the session opens, then the file PUT does as told."""

    def __init__(self, put):
        self.put, self.methods = put, []

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        import httplib2

        self.methods.append(method)
        if method == "POST":
            return httplib2.Response({"status": 200, "location": "https://upload/session"}), b""
        if isinstance(self.put, Exception):
            raise self.put
        return httplib2.Response({"status": self.put}), b"{}"


def real_upload_request(tmp_path, http):
    from googleapiclient.http import HttpRequest, MediaFileUpload

    clip = tmp_path / "real.mp4"
    clip.write_bytes(b"x" * 10)
    media = MediaFileUpload(str(clip), mimetype="video/mp4", resumable=True, chunksize=-1)
    return HttpRequest(http, lambda resp, content: json.loads(content), "https://upload/videos", method="POST",
                       body="{}", headers={}, resumable=media)


@pytest.mark.parametrize("put", [TimeoutError("timed out"), 502])
def test_the_real_client_reports_a_failed_put_as_unknown(tmp_path, put):
    from app.publishers.base import UploadOutcomeUnknown
    from app.publishers.youtube_publisher import _send

    http = ScriptedHttp(put)
    with pytest.raises(UploadOutcomeUnknown):
        _send(real_upload_request(tmp_path, http))
    assert http.methods == ["POST", "PUT"]


def test_the_real_client_reports_a_refused_put_as_refused(tmp_path):
    from googleapiclient.errors import HttpError

    from app.publishers.youtube_publisher import _send

    with pytest.raises(HttpError):
        _send(real_upload_request(tmp_path, ScriptedHttp(403)))


def test_a_lost_reply_counts_against_the_clip_while_it_waits(tmp_path, monkeypatch):
    from tasks.pipeline import publish_task

    monkeypatch.setattr("tasks.pipeline.get_publisher", ReplyLostPublisher)
    video_id = ready_file_clip(tmp_path)
    out = publish_task(video_id)
    assert out["ok"] is False and out["retry"] is True and out["account_level"] is False
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.status == VideoStatus.PUBLISHING and video.meta["publish_failures"] == 1 and video.error
        assert video.meta["publish_claimed_at"]


# ---------- what the loop must not post
def set_fields(video_id, **values):
    with session_scope() as db:
        video = db.get(Video, video_id)
        for name, value in values.items():
            setattr(video, name, value)
    return video_id


def add_plan(plan=None):
    with session_scope() as db:
        db.add(GrowthState(key="plan", value=plan or waiting_plan()))


def tick(minutes=0):
    return autopilot.autopilot_job(now=START + timedelta(minutes=minutes), rng=random.Random(minutes))


def stock_ids():
    with session_scope() as db:
        return {v.id for v in autopilot.ready_clips(db)}


# -- safety and focus, last thing before an upload
def test_a_blocked_clip_in_stock_is_rejected_and_the_next_one_posts(tmp_path, publisher):
    bad = set_fields(ready_file_clip(tmp_path, 0, parent="p0"), title="Best casino tricks")
    good = ready_file_clip(tmp_path, 1, parent="p1")
    add_plan()
    assert tick()["video_id"] == good
    assert len(publisher.uploads) == 1
    with session_scope() as db:
        row = db.get(Video, bad)
        assert row.status == VideoStatus.REJECTED and "casino" in row.meta["auto_rejected"]
    assert [e.video_id for e in events("auto_rejected")] == [bad]


def test_an_off_channel_clip_is_rejected_and_the_post_keeps_waiting(tmp_path, publisher):
    bad = set_fields(ready_file_clip(tmp_path), language="hi")
    add_plan()
    assert "no finished clips" in tick()["skipped"]
    assert publisher.uploads == []
    assert read_plan()["posts"][0]["state"] == "waiting"
    with session_scope() as db:
        assert db.get(Video, bad).meta["auto_rejected"] == "language: hi"


def saved_clip(tmp_path, *, auto_queue=True, title="A fine source", description="", moment_title="A fine moment",
               moment_description="Written by the AI", keywords=(), language="en"):
    from app.clipper.ingest import SourceVideo
    from app.clipper.job import _save_clip
    from app.clipper.moments import Moment

    src = SourceVideo(path=str(tmp_path / "s.mp4"), title=title, description=description, author="a",
                      url="https://example.com/v", source="youtube", source_id="src", keywords=list(keywords))
    moment = Moment(0, 30, title=moment_title, ai_title=True, description=moment_description)
    (tmp_path / "c.mp4").write_bytes(b"x")
    return _save_clip(src, moment, str(tmp_path / "c.mp4"), 30.0, language=language, auto_queue=auto_queue)


@pytest.mark.parametrize("text", [
    {"title": "Inside a casino"},
    {"description": "Sponsored by 1xbet"},  # the AI wrote the clip's description, so only the source says this
    {"moment_title": "My gambling story"},
    {"keywords": ["betting"]},
])
def test_auto_queue_leaves_a_flagged_clip_pending_with_the_reason(tmp_path, text):
    video_id, status = saved_clip(tmp_path, **text)
    assert status == VideoStatus.PENDING
    with session_scope() as db:
        assert "blocked term" in db.get(Video, video_id).meta["growth_flagged"]
    assert stock_ids() == set()


def test_auto_queue_still_stocks_a_clean_clip(tmp_path):
    video_id, status = saved_clip(tmp_path)
    assert status == VideoStatus.TRANSFORMED and stock_ids() == {video_id}
    with session_scope() as db:
        assert "growth_flagged" not in db.get(Video, video_id).meta


def test_a_clip_going_to_review_anyway_is_not_flagged(tmp_path):
    video_id, status = saved_clip(tmp_path, auto_queue=False, title="Inside a casino")
    assert status == VideoStatus.PENDING
    with session_scope() as db:
        assert "growth_flagged" not in db.get(Video, video_id).meta


# -- takedowns
def strike(video_id, reason, now=START):
    with session_scope() as db:
        safety.record_strike(db.get(Video, video_id), reason, now=now)


def live_clip(tmp_path, n, parent=None, author="a"):
    video_id = published_clip(tmp_path / f"live{n}.mp4", n, 5, parent=parent or f"live{n}")
    return set_fields(video_id, author=author, source_url=f"https://example.com/live{n}")


def paused_reason():
    with session_scope() as db:
        return safety.pause_reason(db)


def test_a_vanished_video_quarantines_only_its_own_sources_unposted_clips(tmp_path, monkeypatch):
    live_clip(tmp_path, 0, parent="src")
    sibling, other = ready_file_clip(tmp_path, 1, parent="src"), ready_file_clip(tmp_path, 2, parent="other")
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    collect.collect_job(now=START + timedelta(hours=1))
    assert stock_ids() == {other}
    with session_scope() as db:
        assert db.get(Video, sibling).meta["growth_quarantined"] == "missing"
        assert "growth_quarantined" not in db.get(Video, other).meta


def test_a_clip_finished_after_the_strike_is_quarantined_too(tmp_path):
    strike(live_clip(tmp_path, 0, parent="src"), "missing")
    late = ready_file_clip(tmp_path, 1, parent="src")
    assert late not in stock_ids()


def test_one_hard_strike_pauses_posting_until_the_owner_resumes(tmp_path, publisher):
    strike(set_fields(live_clip(tmp_path, 0), published_at=START - timedelta(days=1)), "copyright")
    ready_file_clip(tmp_path, 1, parent="other")
    add_plan()
    assert "copyright" in paused_reason()
    assert "paused" in tick(10_000)["skipped"]
    assert publisher.uploads == []
    assert len(events("paused")) == 1
    assert safety.resume() is True
    assert paused_reason() is None and tick()["ok"] is True
    assert safety.resume() is False


@pytest.mark.parametrize("reason", ["copyright", "claim", "termsOfUse"])
def test_hard_reasons(tmp_path, reason):
    strike(live_clip(tmp_path, 0), reason)
    assert paused_reason()


def test_three_soft_strikes_pause_but_two_do_not(tmp_path):
    strike(live_clip(tmp_path, 0), "missing")
    strike(live_clip(tmp_path, 1), "duplicate")  # rejected, but not a mark against the channel
    assert paused_reason() is None
    strike(live_clip(tmp_path, 2), "missing")
    assert "3" in paused_reason()
    assert len(events("paused")) == 1


def test_strikes_older_than_ninety_days_do_not_count(tmp_path):
    strike(live_clip(tmp_path, 0), "missing", now=START - timedelta(days=91))
    strike(live_clip(tmp_path, 1), "missing", now=START - timedelta(days=89))
    strike(live_clip(tmp_path, 2), "missing", now=START)
    assert paused_reason() is None


def test_several_videos_vanishing_at_once_pause_once(tmp_path, monkeypatch):
    for n in range(4):
        live_clip(tmp_path, n)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    collect.collect_job(now=START + timedelta(hours=1))
    assert paused_reason()
    assert len(events("paused")) == 1 and len(events("strike")) == 4


def test_seeing_the_same_video_again_is_not_a_new_strike(tmp_path):
    video_id = live_clip(tmp_path, 0)
    for _ in range(5):
        strike(video_id, "missing")
    assert paused_reason() is None and len(events("strike")) == 1


def test_a_cleared_pause_stays_cleared_while_collect_keeps_seeing_the_same_rejection(tmp_path):
    video_id = live_clip(tmp_path, 0)
    strike(video_id, "copyright")
    safety.resume()
    strike(video_id, "copyright")
    assert paused_reason() is None


def test_a_missing_video_later_found_rejected_for_copyright_is_hard(tmp_path):
    video_id = live_clip(tmp_path, 0)
    strike(video_id, "missing")
    assert paused_reason() is None
    strike(video_id, "copyright")
    assert "copyright" in paused_reason()
    assert len(events("strike")) == 2


def test_a_new_problem_after_a_resume_pauses_again(tmp_path):
    strike(live_clip(tmp_path, 0), "copyright")
    safety.resume()
    strike(live_clip(tmp_path, 1), "missing")
    assert paused_reason()
    assert len(events("paused")) == 2


def test_supply_skips_a_struck_source(tmp_path, monkeypatch):
    video_id = live_clip(tmp_path, 0)
    strike(video_id, "missing")
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "growth_feeds", '["feed"]')
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/live0", "https://example.com/next"])
    assert supply.next_source() == ("https://example.com/next", "feed")


def test_supply_skips_a_struck_authors_feed_links(tmp_path, monkeypatch):
    import yt_dlp

    feeds = {
        "their-channel": {"channel_id": "UCbad", "entries": [{"url": "https://example.com/theirs"}]},
        "mixed-playlist": {"entries": [{"url": "https://example.com/b1", "uploader_id": "@bad"},
                                       {"url": "https://example.com/b2", "uploader_id": "@good"}]},
    }

    class FakeYdl:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, feed, download):
            return feeds[feed]

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "growth_feeds", '["their-channel", "mixed-playlist"]')
    assert supply.next_source() == ("https://example.com/theirs", "their-channel")
    strike(set_fields(live_clip(tmp_path, 0, author="@bad"), author_name="UCbad"), "missing")
    assert supply.next_source() == ("https://example.com/b2", "mixed-playlist")


@pytest.mark.parametrize("reason", ["copyright", "claim", "termsOfUse", "legal", "inappropriate", "trademark",
                                    "uploaderAccountSuspended", "uploaderAccountClosed"])
def test_youtube_acting_on_content_or_the_account_pauses_at_once(tmp_path, reason):
    strike(live_clip(tmp_path, 0), reason)
    assert paused_reason() and reason in paused_reason()


@pytest.mark.parametrize("reason", ["duplicate", "length", "missing", "conversion", "codec"])
def test_a_technical_rejection_is_a_soft_strike(tmp_path, reason):
    strike(live_clip(tmp_path, 0), reason)
    assert paused_reason() is None


def test_a_winner_from_a_struck_source_is_not_mined(tmp_path):
    strike(live_clip(tmp_path, 0), "missing")
    from dataclasses import replace

    with session_scope() as db:
        assert learn._mine_winner(db, replace(result(100), source_url="https://example.com/live0")) is None
        assert learn._mine_winner(db, replace(result(100), source_url="https://example.com/fine"))


def test_doctor_and_report_show_the_pause(tmp_path):
    assert checks_by_name()["pause"]["ok"] is True and report.build_report()["paused"] is None
    strike(live_clip(tmp_path, 0), "copyright")
    check = checks_by_name()["pause"]
    assert check["ok"] is False and check["required"] is True and "resume" in check["detail"]
    assert report.doctor()["ready"] is False
    assert "copyright" in report.build_report()["paused"]["reason"]


def test_the_resume_command_clears_the_pause(tmp_path, monkeypatch, capsys):
    import runpy
    import sys
    from pathlib import Path

    strike(live_clip(tmp_path, 0), "copyright")
    script = Path(__file__).resolve().parent.parent / "scripts" / "growth.py"
    monkeypatch.setattr(sys, "argv", [str(script), "resume"])
    with pytest.raises(SystemExit) as finished:
        runpy.run_path(str(script), run_name="__main__")
    assert finished.value.code == 0 and paused_reason() is None
    assert "Resumed" in capsys.readouterr().out
    assert len(events("resumed")) == 1


# -- failures in the publish step
def test_an_exception_while_publishing_is_counted_and_reported(tmp_path, publisher, monkeypatch):
    ready_file_clip(tmp_path)
    add_plan()

    def boom(video_id):
        raise ValueError("no metadata")

    monkeypatch.setattr("tasks.pipeline.generate_metadata_for_video", boom)
    outcomes = [tick(m) for m in (0, 10, 20)]
    assert all(o["ok"] is False and "no metadata" in o["error"] for o in outcomes)
    post = read_plan()["posts"][0]
    assert post["state"] == "failed" and post["attempts"] == 3
    assert "no metadata" in events("publish_failed")[0].summary


class LockAfterUpload(FakePublisher):
    """The upload goes through, and then the plan cannot be saved."""

    def __init__(self, failing_sql, times):
        super().__init__()
        self.arm, self.times = failing_sql, times

    def upload(self, **kwargs):
        self.arm("UPDATE growth_state", times=self.times)
        return super().upload(**kwargs)


def test_saving_the_plan_retries_through_a_lock(tmp_path, monkeypatch, failing_sql):
    monkeypatch.setattr(autopilot.time, "sleep", lambda s: None)
    fake = LockAfterUpload(failing_sql, 1)
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    ready_file_clip(tmp_path)
    add_plan()
    assert tick()["ok"] is True
    assert read_plan()["posts"][0]["state"] == "posted" and len(events("posted")) == 1


def test_a_plan_save_that_never_lands_cannot_post_twice(tmp_path, monkeypatch, failing_sql):
    monkeypatch.setattr(autopilot.time, "sleep", lambda s: None)
    fake = LockAfterUpload(failing_sql, 99)
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    first = ready_file_clip(tmp_path, 0, parent="p0")
    ready_file_clip(tmp_path, 1, parent="p1")
    add_plan()
    assert tick()["ok"] is True  # the video is live, whatever happened to the plan
    failing_sql.clear()
    assert read_plan()["posts"][0]["state"] == "waiting"
    with session_scope() as db:  # far enough on for the gap to be clear
        db.get(Video, first).published_at = START
    assert "ok" not in tick(130)
    assert len(fake.uploads) == 1
    post = read_plan()["posts"][0]
    assert post["state"] == "posted" and post["video_id"] == first
    assert len(events("posted")) == 1


def test_an_upload_in_an_earlier_days_plan_is_not_posted_again_after_rollover(tmp_path, publisher):
    video_id = set_fields(ready_file_clip(tmp_path), status=VideoStatus.PUBLISHED, published_at=START,
                          youtube_video_id="yt9")
    post = waiting_plan(hours_ago=1)["posts"][0]
    with session_scope() as db:
        video = db.get(Video, video_id)
        video.meta = {**video.meta, "growth_post": autopilot._post_id(post)}
    add_plan({"day": "2026-11-01", "posts": [post]})
    tick(10)
    assert [p for p in read_plan()["posts"] if p["due"] == post["due"]] == []
    assert len(events("posted")) == 1 and events("missed") == []


# -- focus reaches the clip choice
def clip_row(seconds=30, author="a", **meta):
    from types import SimpleNamespace

    return SimpleNamespace(duration_sec=seconds, author=author, meta={"clip_kind": "speech", "clip_parent": author, **meta})


def test_focus_on_length_tests_clip_length_harder():
    known = Model([Observation({"length": "20-35"}, 9.0, 0.0), Observation({"length": "35-60"}, 1.0, 0.0)] * 5)
    favoured, untried = clip_row(30), clip_row(50)

    def picks(focus):
        current = autopilot.Policy(known.draw(np.random.default_rng(3)), {"length": {"20-35": 9}}, 0.1,
                                   frozenset(focus), random.Random(4))
        return sum(autopilot.pick_clip([favoured, untried], current, None) is untried for _ in range(1000))

    assert picks(("length",)) > picks(()) + 100


def test_a_focused_turn_tests_only_the_focused_clip_factors():
    new_series, new_length = clip_row(30, author="new"), clip_row(50, author="old")
    tried = {"series": {"old": 9}, "length": {"20-35": 9}}
    # The draw favours the same clip, so only a turn that tested length could choose the other.
    known = Model([Observation({"series": "new"}, 9.0, 0.0), Observation({"series": "old"}, 1.0, 0.0)] * 5)
    current = autopilot.Policy(known.draw(np.random.default_rng(3)), tried, 1.0, frozenset({"series"}), random.Random(4))
    assert all(autopilot.pick_clip([new_series, new_length], current, None) is new_series for _ in range(200))


def test_focus_elsewhere_leaves_the_clip_choice_alone():
    clips = [clip_row(30, author="x"), clip_row(50, author="y")]

    def sequence(focus):
        current = autopilot.Policy(Model([]).draw(np.random.default_rng(3)), {}, 0.1, frozenset(focus), random.Random(4))
        return [autopilot.pick_clip(clips, current, None) for _ in range(200)]

    assert sequence(("slot", "title_style")) == sequence(())


# -- failures that are not about the clip
def test_a_youtube_outage_keeps_the_window_open_until_it_passes(tmp_path, monkeypatch):
    fake = FakePublisher(http_error(503, "backendError"))
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    ready_file_clip(tmp_path)
    add_plan()
    for minutes in range(0, 60, 10):
        assert tick(minutes)["ok"] is False
    post = read_plan()["posts"][0]
    assert post["state"] == "waiting" and post["attempts"] == 0 and events("publish_failed") == []
    fake.error = None
    assert tick(60)["ok"] is True
    assert len(fake.uploads) == 1


def test_the_late_grace_ends_a_window_a_retryable_failure_held_open(tmp_path, monkeypatch):
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: FakePublisher("YouTube token missing at x"))
    ready_file_clip(tmp_path)
    add_plan()
    tick()
    tick(60 * 5 + 1)  # ends is 3h on, and the grace is 2h
    assert read_plan()["posts"][0]["state"] == "missed"
    assert "token missing" in events("missed")[0].summary


class CountingPublisher(FakePublisher):
    def __init__(self, error):
        super().__init__(error)
        self.calls = 0

    def upload(self, **kwargs):
        self.calls += 1
        return super().upload(**kwargs)


def test_an_account_failure_holds_posting_and_clears_itself(tmp_path, monkeypatch):
    fake = CountingPublisher(http_error(403, "accessNotConfigured"))
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    for n in range(3):
        ready_file_clip(tmp_path, n, parent=f"p{n}")
    add_plan()
    assert tick()["ok"] is False
    assert "hold" in tick(10)["skipped"] and "hold" in tick(50)["skipped"]
    assert fake.calls == 1  # no moving on through the stock
    post = read_plan()["posts"][0]
    assert post["attempts"] == 0 and post["state"] == "waiting"

    assert tick(61)["ok"] is False  # the hour is up, and it is still down
    assert fake.calls == 2 and len(events("account_hold")) == 1 and paused_reason() is None

    fake.error = None
    assert tick(122)["ok"] is True
    with session_scope() as db:
        assert db.get(GrowthState, "hold") is None
    assert events("paused") == [] and events("strike") == []


def test_the_report_shows_a_hold_only_while_it_lasts():
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        safety.set_hold(db, now, "quota is out")
    assert report.build_report()["hold"]["reason"] == "quota is out"
    with session_scope() as db:
        safety.set_hold(db, now - timedelta(hours=2), "quota is out")
    assert report.build_report()["hold"] is None


def test_a_new_account_failure_after_a_hold_cleared_is_reported_again(tmp_path, monkeypatch):
    fake = CountingPublisher(http_error(403, "accessNotConfigured"))
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: fake)
    for n in range(3):
        ready_file_clip(tmp_path, n, parent=f"p{n}")
    plan = waiting_plan()
    plan["posts"].append({**plan["posts"][0], "slot": "04-08"})
    add_plan(plan)
    tick()
    fake.error = None
    video_id = tick(61)["video_id"]
    set_fields(video_id, published_at=START + timedelta(minutes=61))
    fake.error = http_error(403, "accessNotConfigured")
    tick(61 + settings.growth_min_gap_minutes)
    assert len(events("account_hold")) == 2


# -- the day rolling over
def plan_for_yesterday(*posts):
    return {"day": (START - timedelta(days=1)).date().isoformat(), "posts": [
        {"slot": slot, "due": (START + due).isoformat(), "ends": (START + ends).isoformat(), "state": state, "attempts": 0}
        for slot, due, ends, state in posts]}


def test_a_post_left_waiting_at_midnight_is_carried_and_goes_out(tmp_path, publisher):
    ready_file_clip(tmp_path)
    leftover = (START - timedelta(minutes=50)).isoformat()
    add_plan(plan_for_yesterday(("20-24", timedelta(minutes=-50), timedelta(0), "waiting")))
    assert tick(5)["ok"] is True
    carried = next(p for p in read_plan()["posts"] if p["due"] == leftover)
    assert carried["state"] == "posted" and read_plan()["day"] == START.date().isoformat()
    assert events("missed") == []


def test_a_leftover_past_its_grace_is_recorded_as_missed_and_not_carried(tmp_path, publisher):
    gone = (START - timedelta(hours=7)).isoformat()
    add_plan(plan_for_yesterday(("16-20", timedelta(hours=-7), timedelta(hours=-4), "waiting"),
                                ("12-16", timedelta(hours=-9), timedelta(hours=-8), "posted")))
    tick(5)
    assert all(p["state"] == "waiting" and p["due"] != gone for p in read_plan()["posts"])
    assert [e.data["slot"] for e in events("missed")] == ["16-20"]
    assert publisher.uploads == []


def test_a_post_missed_inside_the_day_is_on_the_record_too(tmp_path, publisher):
    add_plan(waiting_plan(hours_ago=20, window_hours=4))
    tick()
    assert [e.data["slot"] for e in events("missed")] == ["00-04"]


# -- the test clock and rehearsals
def test_rendering_and_planning_do_not_start_the_clock(tmp_path, publisher):
    recipes.choose()
    current = autopilot.policy(START + timedelta(days=60))
    assert current.rate == settings.growth_explore_rate
    add_plan({"day": START.date().isoformat(), "posts": []})
    tick()
    with session_scope() as db:
        assert db.get(GrowthState, "started") is None
    assert report.build_report()["phase"] == "not started"


def test_the_first_real_post_starts_the_clock(tmp_path, publisher):
    video_id = ready_file_clip(tmp_path)
    add_plan()
    assert tick()["ok"] is True
    set_fields(video_id, published_at=START)
    tick(10)
    with session_scope() as db:
        assert autopilot.started_at(db) == START
        assert db.get(GrowthState, "started").value["video_id"] == video_id
    later = autopilot.policy(START + timedelta(days=settings.growth_explore_days + 1))
    assert later.rate == settings.growth_settled_explore_rate


def rehearsed_clip(tmp_path, n=0, *, parent="p", published_at=START - timedelta(minutes=5)):
    video_id = make_clip(tmp_path / "m.mp4", n, parent=parent, status=VideoStatus.PUBLISHED)
    return set_fields(video_id, youtube_video_id=f"MOCK_{n}", published_at=published_at)


def test_a_rehearsal_post_does_not_start_the_clock(tmp_path):
    video_id = ready_file_clip(tmp_path)  # no fake publisher here: the mock one answers
    add_plan()
    outcome = tick()
    assert outcome["ok"] is True and outcome["youtube_id"].startswith("MOCK_")
    tick(10)
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PUBLISHED
        assert autopilot.started_at(db) is None


def test_a_clock_left_by_a_rehearsal_gives_way_to_the_first_real_post(tmp_path, publisher):
    with session_scope() as db:
        db.add(GrowthState(key="started", value={"at": (START - timedelta(days=60)).isoformat()}))
    assert autopilot.policy(START).rate == settings.growth_explore_rate
    video_id = set_fields(ready_file_clip(tmp_path, growth_title_style="plain"), status=VideoStatus.PUBLISHED,
                          published_at=START, youtube_video_id="yt9")
    add_plan({"day": START.date().isoformat(), "posts": []})
    tick(1)
    with session_scope() as db:
        assert autopilot.started_at(db) == START
        assert db.get(GrowthState, "started").value["video_id"] == video_id


def test_rehearsal_posts_are_left_out_of_the_tested_counts(tmp_path):
    rehearsed_clip(tmp_path)
    assert autopilot.policy(START).tried == {}
    published_clip(tmp_path / "real.mp4", 1, 5)
    assert autopilot.policy(START).tried["series"] == {"a": 1}


def test_a_rehearsal_post_does_not_hold_the_real_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "use_mock", False)
    rehearsed_clip(tmp_path, published_at=START - timedelta(minutes=1))
    with session_scope() as db:
        assert autopilot._gap_is_clear(db, START) is True
    published_clip(tmp_path / "real.mp4", 1, 5, published_at=START - timedelta(minutes=1))
    with session_scope() as db:
        assert autopilot._gap_is_clear(db, START) is False


def test_a_rehearsal_post_does_not_count_as_the_last_source(tmp_path, publisher, monkeypatch):
    monkeypatch.setattr(settings, "use_mock", False)
    real_autopilot(monkeypatch, publisher)
    rehearsed_clip(tmp_path, parent="only")
    video_id = ready_file_clip(tmp_path, 1, parent="only")
    seen = []
    real = autopilot.pick_clip

    def spy(clips, current, last_parent):
        seen.append(last_parent)
        return real(clips, current, last_parent)

    monkeypatch.setattr(autopilot, "pick_clip", spy)
    add_plan()
    assert tick()["video_id"] == video_id and seen == [None]


def recut_source(tmp_path):
    from app.clipper.ingest import SourceVideo
    from app.clipper.moments import Moment

    src = SourceVideo(path=str(tmp_path / "s.mp4"), title="Source", url="https://example.com/v", source="youtube",
                      source_id="src")
    return src, Moment(0, 30, title="A fine moment", ai_title=True)


def test_a_rehearsed_clip_can_be_cut_again_and_posted_for_real(tmp_path):
    from app.clipper import job

    src, moment = recut_source(tmp_path)
    video_id, _ = job._save_clip(src, moment, str(tmp_path / "c.mp4"), 30.0, language="en", auto_queue=True)
    set_fields(video_id, status=VideoStatus.PUBLISHED, youtube_video_id="MOCK_1", published_at=START)
    assert job._existing_row(src, moment) == (None, None, None)
    assert job._earlier_clips(src) == []
    again, status = job._save_clip(src, moment, str(tmp_path / "c2.mp4"), 30.0, language="en", auto_queue=True)
    assert again == video_id and status == VideoStatus.TRANSFORMED
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert video.youtube_video_id is None and video.published_at is None
        assert video.transformed_path.endswith("c2.mp4")


def test_a_really_published_clip_is_still_left_alone(tmp_path):
    from app.clipper import job

    src, moment = recut_source(tmp_path)
    video_id, _ = job._save_clip(src, moment, str(tmp_path / "c.mp4"), 30.0, language="en", auto_queue=True)
    set_fields(video_id, status=VideoStatus.PUBLISHED, youtube_video_id="yt1", published_at=START)
    assert job._existing_row(src, moment)[0] == VideoStatus.PUBLISHED
    assert len(job._earlier_clips(src)) == 1
    _, status = job._save_clip(src, moment, str(tmp_path / "c2.mp4"), 30.0, language="en", auto_queue=True)
    assert status == VideoStatus.PUBLISHED
    with session_scope() as db:
        assert db.get(Video, video_id).youtube_video_id == "yt1"


def test_the_report_command_shows_a_pause_and_a_hold(tmp_path, monkeypatch, capsys):
    import runpy
    import sys
    from pathlib import Path

    strike(live_clip(tmp_path, 0), "copyright")
    with session_scope() as db:
        safety.set_hold(db, datetime.now(timezone.utc), "quota is out")
    script = Path(__file__).resolve().parent.parent / "scripts" / "growth.py"
    monkeypatch.setattr(sys, "argv", [str(script), "report"])
    with pytest.raises(SystemExit):
        runpy.run_path(str(script), run_name="__main__")
    out = capsys.readouterr().out
    assert "PAUSED" in out and "copyright" in out and "scripts/growth.py resume" in out and "quota is out" in out


# -- a clip that raises rather than fails
def twins(tmp_path):
    """Same arms, so only the failure count can tell them apart. The older one wins any tie."""
    bad, good = ready_file_clip(tmp_path, 0, parent="p"), ready_file_clip(tmp_path, 1, parent="p")
    set_fields(bad, transformed_at=START - timedelta(days=2))
    set_fields(good, transformed_at=START - timedelta(days=1))
    return bad, good


@pytest.mark.parametrize("step", ["generate_metadata_for_video", "publish_task"])
def test_a_clip_that_keeps_raising_gives_way_to_the_next(tmp_path, publisher, monkeypatch, step):
    import tasks.pipeline as pipeline

    bad, good = twins(tmp_path)
    real = getattr(pipeline, step)

    def raises(video_id, *args, **kwargs):
        if video_id == bad:
            raise ValueError("this clip breaks it")
        return real(video_id, *args, **kwargs)

    monkeypatch.setattr(pipeline, step, raises)
    add_plan()
    assert "this clip breaks it" in tick()["error"]
    assert tick(10)["video_id"] == good
    assert len(publisher.uploads) == 1
    with session_scope() as db:
        assert db.get(Video, bad).meta["publish_failures"] == 1
    assert read_plan()["posts"][0]["state"] == "posted" and read_plan()["posts"][0]["attempts"] == 2


# -- what collect judges
def test_a_video_removed_after_the_snapshot_month_is_still_a_strike(tmp_path, monkeypatch):
    video_id = set_fields(live_clip(tmp_path, 0, parent="src"), published_at=START - timedelta(days=31))
    sibling = ready_file_clip(tmp_path, 1, parent="src")
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    assert collect.collect_job(now=START)["snapshots"] == 0
    assert sibling not in stock_ids()
    with session_scope() as db:
        assert [s["video_id"] for s in db.get(GrowthState, "strikes").value["log"]] == [video_id]


def test_past_the_snapshot_month_a_video_is_only_checked_for_existence(tmp_path, monkeypatch):
    video_id = set_fields(live_clip(tmp_path, 0), published_at=START - timedelta(days=31))
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt0": stat(9999)}))
    assert collect.collect_job(now=START)["snapshots"] == 0
    with session_scope() as db:
        assert db.get(Video, video_id).meta["yt_privacy"] == "public"
        assert db.execute(select(VideoSnapshot).where(VideoSnapshot.video_id == video_id)).scalars().one().age_hours == 100
        assert db.get(GrowthState, "strikes") is None


def test_a_video_past_the_strike_window_is_not_asked_about(tmp_path, monkeypatch):
    set_fields(live_clip(tmp_path, 0), published_at=START - timedelta(days=91))
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    assert "skipped" in collect.collect_job(now=START)


def test_a_clip_published_while_youtube_answers_is_not_judged(tmp_path, monkeypatch):
    set_fields(live_clip(tmp_path, 0), published_at=START - timedelta(hours=1))
    sibling = ready_file_clip(tmp_path, 1, parent="new")
    newcomer = set_fields(ready_file_clip(tmp_path, 2, parent="new"), source_url="https://example.com/new")

    class Racing(FakeInsights):
        def stats(self, ids):
            set_fields(newcomer, status=VideoStatus.PUBLISHED, youtube_video_id="ytnew", published_at=START)
            return {"yt0": stat(9)}

    monkeypatch.setattr(collect, "get_insights", lambda: Racing())
    assert collect.collect_job(now=START + timedelta(minutes=1))["snapshots"] == 1
    assert events("strike") == [] and sibling in stock_ids()
    with session_scope() as db:
        assert "yt_missing" not in db.get(Video, newcomer).meta


# -- blocks can be seen and undone
def test_a_video_that_comes_back_takes_its_strike_and_block_with_it(tmp_path, monkeypatch):
    video_id = live_clip(tmp_path, 0, parent="src", author="@one")
    sibling = ready_file_clip(tmp_path, 1, parent="src")
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    collect.collect_job(now=START + timedelta(hours=1))
    with session_scope() as db:
        assert safety.blocked_authors(db) == {"@one"}
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt0": stat(9)}))
    collect.collect_job(now=START + timedelta(hours=4))
    assert sibling in stock_ids()
    with session_scope() as db:
        video = db.get(Video, video_id)
        assert "growth_strike" not in video.meta and "yt_missing" not in video.meta
        assert "growth_quarantined" not in db.get(Video, sibling).meta
        assert not safety.blocked_authors(db) and not safety.blocked_sources(db) and not safety.blocked_parents(db)
        assert db.get(GrowthState, "strikes").value["log"] == []
    assert len(events("strike_cleared")) == 1


def test_a_hard_strike_does_not_clear_when_the_video_shows_up(tmp_path, monkeypatch):
    live_clip(tmp_path, 0, parent="src", author="@one")
    sibling = ready_file_clip(tmp_path, 1, parent="src")
    strike(live_clip(tmp_path, 2, parent="other", author="@two"), "missing")
    with session_scope() as db:
        safety.record_strike(db.execute(select(Video).where(Video.youtube_video_id == "yt0")).scalar_one(), "copyright",
                             now=START)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt0": stat(9)}))
    collect.collect_job(now=START + timedelta(hours=1))
    assert sibling not in stock_ids()
    with session_scope() as db:
        assert "@one" in safety.blocked_authors(db)


def test_one_of_two_struck_videos_returning_keeps_the_other_block(tmp_path, monkeypatch):
    live_clip(tmp_path, 0, parent="src")
    live_clip(tmp_path, 2, parent="src")
    sibling = ready_file_clip(tmp_path, 1, parent="src")
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    collect.collect_job(now=START + timedelta(hours=1))
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt0": stat(9)}))
    collect.collect_job(now=START + timedelta(hours=4))
    assert sibling not in stock_ids()
    with session_scope() as db:
        assert db.get(Video, sibling).meta["growth_quarantined"] == "missing"


def test_a_returning_video_leaves_a_pause_to_the_owner(tmp_path, monkeypatch):
    for n in range(3):
        live_clip(tmp_path, n)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    collect.collect_job(now=START + timedelta(hours=1))
    assert paused_reason()
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({f"yt{n}": stat(9) for n in range(3)}))
    collect.collect_job(now=START + timedelta(hours=4))
    assert paused_reason()


def test_the_owner_can_lift_a_block_and_it_stays_lifted(tmp_path, monkeypatch):
    live_clip(tmp_path, 0, parent="src", author="@One")
    sibling = ready_file_clip(tmp_path, 1, parent="src")
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    collect.collect_job(now=START + timedelta(hours=1))
    assert safety.unblock("nobody") == 0
    assert sibling not in stock_ids()
    assert safety.unblock("@ONE") == 1
    collect.collect_job(now=START + timedelta(hours=4))  # still gone, as the owner deleted it
    assert sibling in stock_ids()
    with session_scope() as db:
        assert not safety.blocked_authors(db)
        assert len(db.get(GrowthState, "strikes").value["log"]) == 1
    assert len(events("unblocked")) == 1 and len(events("strike")) == 1


def test_lifting_an_author_lifts_every_block_naming_them(tmp_path):
    strike(live_clip(tmp_path, 0, author="@one"), "missing")
    strike(live_clip(tmp_path, 1, author="@one"), "missing")
    assert safety.unblock("@one") == 2


def test_a_lifted_block_comes_back_if_the_video_turns_out_to_be_a_takedown(tmp_path):
    video_id = live_clip(tmp_path, 0, author="@one")
    strike(video_id, "missing")
    safety.unblock("@one")
    strike(video_id, "copyright")
    with session_scope() as db:
        assert safety.blocked_authors(db) == {"@one"}


def test_doctor_and_report_show_a_block_and_how_to_lift_it(tmp_path):
    assert checks_by_name()["takedown blocks"]["ok"] is True and report.build_report()["blocks"] == []
    strike(live_clip(tmp_path, 0, author="@one"), "missing")
    check = checks_by_name()["takedown blocks"]
    assert check["ok"] is False and check["required"] is False
    assert "@one" in check["detail"] and "scripts/growth.py unblock" in check["detail"]
    assert report.build_report()["blocks"][0]["authors"] == ["@one"]


def test_a_starved_supply_names_the_block_behind_it(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    assert "unblock" not in supply.supply_job()["starved"]
    strike(live_clip(tmp_path, 0, author="@one"), "missing")
    starved = supply.supply_job()["starved"]
    assert "@one" in starved and "scripts/growth.py unblock" in starved


def run_growth_script(monkeypatch, *args):
    import runpy
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "growth.py"
    monkeypatch.setattr(sys, "argv", [str(script), *args])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(script), run_name="__main__")
    return stopped.value.code


def test_the_commands_show_and_lift_a_block(tmp_path, monkeypatch, capsys):
    strike(live_clip(tmp_path, 0, author="@one"), "missing")
    run_growth_script(monkeypatch, "report")
    assert "@one" in capsys.readouterr().out
    run_growth_script(monkeypatch, "unblock")
    out = capsys.readouterr().out
    assert "@one" in out and "unblock <name>" in out
    run_growth_script(monkeypatch, "unblock", "@one")
    assert "Lifted 1" in capsys.readouterr().out
    with session_scope() as db:
        assert not safety.blocked_authors(db)
    run_growth_script(monkeypatch, "unblock", "@one")
    assert "No block" in capsys.readouterr().out


def test_a_winner_by_a_blocked_author_is_not_mined(tmp_path):
    strike(live_clip(tmp_path, 0, author="@Bad"), "copyright")
    safety.resume()
    winner = set_fields(live_clip(tmp_path, 1, parent="other", author="@bad"), source_url="https://example.com/other")
    with session_scope() as db:
        db.execute(select(VideoSnapshot).where(VideoSnapshot.video_id == winner)).scalar_one().views = 5000
    for n in range(2, 12):
        live_clip(tmp_path, n, parent=f"base{n}", author="@fine")
    assert learn.learn_job(now=JUDGED_AT)["diagnosed"]["winner"] == 1
    with session_scope() as db:
        assert db.execute(select(ClipJob)).first() is None
        assert db.get(Video, winner).meta["growth_diagnosis"]["code"] == "winner"


def test_a_winner_by_a_clear_author_is_still_mined(tmp_path):
    strike(live_clip(tmp_path, 0, author="@bad"), "copyright")
    safety.resume()
    winner = set_fields(live_clip(tmp_path, 1, parent="other", author="@good"), source_url="https://example.com/other")
    with session_scope() as db:
        db.execute(select(VideoSnapshot).where(VideoSnapshot.video_id == winner)).scalar_one().views = 5000
    for n in range(2, 12):
        live_clip(tmp_path, n, parent=f"base{n}", author="@fine")
    learn.learn_job(now=JUDGED_AT)
    with session_scope() as db:
        assert db.execute(select(ClipJob)).scalar_one().source == "https://example.com/other"


# -- the two gates ask the same question
@pytest.fixture
def english_only(monkeypatch):
    monkeypatch.setattr(settings, "target_languages", '["en"]')
    monkeypatch.setattr(settings, "allow_unknown_language", False)


def test_a_stocked_clip_with_no_language_still_posts(tmp_path, publisher, english_only):
    video_id = set_fields(ready_file_clip(tmp_path, kind="visual"), language="")
    add_plan()
    assert tick()["video_id"] == video_id


def test_auto_queue_stocks_a_clip_with_no_language(tmp_path, english_only):
    video_id, status = saved_clip(tmp_path, language="")
    assert status == VideoStatus.TRANSFORMED and stock_ids() == {video_id}


def test_a_clip_with_no_language_survives_the_queue_filters(tmp_path, english_only):
    from tasks.pipeline import apply_queue_filters

    video_id, _ = saved_clip(tmp_path, language="", auto_queue=False)
    apply_queue_filters()
    with session_scope() as db:
        assert db.get(Video, video_id).status == VideoStatus.PENDING


def test_a_clip_that_fits_only_through_its_sources_words_posts(tmp_path, publisher, monkeypatch):
    monkeypatch.setattr(settings, "target_topics", '["fitness"]')
    video_id, status = saved_clip(tmp_path, title="Fitness podcast 12", moment_title="The one habit nobody keeps")
    assert status == VideoStatus.TRANSFORMED
    add_plan()
    assert tick()["video_id"] == video_id


def test_a_term_blocked_after_the_render_is_found_in_the_sources_words_too(tmp_path, publisher, monkeypatch):
    video_id, status = saved_clip(tmp_path, description="Brought to you by Acme")
    assert status == VideoStatus.TRANSFORMED
    monkeypatch.setattr("tasks.pipeline._BLOCKED_TERMS", frozenset({"acme"}))
    add_plan()
    assert "no finished clips" in tick()["skipped"]
    with session_scope() as db:
        assert db.get(Video, video_id).meta["auto_rejected"] == "blocked term: acme"


# -- a person's decision stands
def approve(video_id):
    from tasks.pipeline import transform_task

    set_fields(video_id, status=VideoStatus.APPROVED, curated_at=START)
    assert transform_task(video_id)["action"] == "already rendered"


def test_a_flagged_clip_the_owner_approved_posts(tmp_path, publisher):
    video_id, status = saved_clip(tmp_path, moment_title="My gambling story")
    assert status == VideoStatus.PENDING
    approve(video_id)
    add_plan()
    assert tick()["video_id"] == video_id
    assert events("auto_rejected") == []


def test_an_approved_clip_is_still_held_by_a_takedown(tmp_path, publisher):
    video_id, _ = saved_clip(tmp_path)
    approve(video_id)
    set_fields(video_id, meta={"clip_rendered": True, "clip_parent": "src", "growth_quarantined": "missing"})
    add_plan()
    assert "no finished clips" in tick()["skipped"]


def test_a_new_render_needs_a_new_approval(tmp_path):
    video_id, _ = saved_clip(tmp_path)
    set_fields(video_id, status=VideoStatus.FAILED, curated_at=START)
    assert saved_clip(tmp_path)[0] == video_id
    with session_scope() as db:
        assert db.get(Video, video_id).curated_at is None


# -- the clock is the loop's own
def test_a_clip_published_by_hand_before_the_loop_does_not_start_the_clock(tmp_path, publisher):
    published_clip(tmp_path / "hand.mp4", 9, 5, published_at=START - timedelta(days=60))
    video_id = ready_file_clip(tmp_path, parent="other")
    add_plan({"day": START.date().isoformat(), "posts": []})
    tick()
    with session_scope() as db:
        assert autopilot.started_at(db) is None
    assert autopilot.policy(START).rate == settings.growth_explore_rate
    assert report.build_report()["phase"] == "not started"
    with session_scope() as db:
        db.get(GrowthState, "plan").value = waiting_plan()
    assert tick()["video_id"] == video_id
    tick(10)
    with session_scope() as db:
        assert db.get(GrowthState, "started").value["video_id"] == video_id
    assert autopilot.policy(START).rate == settings.growth_explore_rate


# -- a dead token costs nothing
@pytest.fixture
def dead_token(tmp_path, monkeypatch):
    from app.publishers import YouTubePublisher

    dead = YouTubePublisher()
    dead.token_path = str(tmp_path / "no-token.json")
    monkeypatch.setattr(settings, "use_mock", False)
    real_autopilot(monkeypatch, dead)
    monkeypatch.setattr("tasks.pipeline.get_publisher", lambda: dead)
    return dead


@pytest.fixture
def ai_calls(monkeypatch):
    from app.clipper import ai

    calls = []
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "ask_json", lambda prompt: calls.append(prompt) or {"title": "A written title"})
    return calls


def test_a_dead_token_spends_nothing_and_harms_no_clip(tmp_path, dead_token, ai_calls, monkeypatch):
    metadata = []
    monkeypatch.setattr("tasks.pipeline.generate_metadata_for_video", metadata.append)
    clips = [ready_file_clip(tmp_path, n, parent=f"p{n}") for n in range(3)]
    add_plan(waiting_plan(hours_ago=0, window_hours=24))
    outcomes = [tick(minutes) for minutes in range(0, 24 * 60, 10)]
    assert "token" in outcomes[0]["error"]
    assert sum("error" in o for o in outcomes) == 24  # once an hour, not every tick
    assert ai_calls == [] and metadata == []
    assert len(events("account_hold")) == 1
    assert read_plan()["posts"][0]["attempts"] == 0
    with session_scope() as db:
        for video_id in clips:
            video = db.get(Video, video_id)
            assert video.status == VideoStatus.TRANSFORMED and "publish_failures" not in video.meta


def test_a_network_failure_while_checking_the_token_is_not_a_hold(tmp_path, publisher, monkeypatch, ai_calls):
    monkeypatch.setattr(settings, "use_mock", False)
    real_autopilot(monkeypatch, publisher)
    ready_file_clip(tmp_path)
    monkeypatch.setattr(publisher, "check_ready", lambda: (_ for _ in ()).throw(OSError("Network is unreachable")))
    add_plan()
    assert "unreachable" in tick()["error"]
    assert events("account_hold") == [] and ai_calls == []
    assert read_plan()["posts"][0] | {"last_error": ""} == waiting_plan()["posts"][0] | {"last_error": ""}


def test_a_good_token_goes_through_the_check(tmp_path, publisher, monkeypatch):
    monkeypatch.setattr(settings, "use_mock", False)
    real_autopilot(monkeypatch, publisher)
    video_id = ready_file_clip(tmp_path)
    add_plan()
    assert tick()["video_id"] == video_id


def test_an_unusable_token_is_an_account_problem(tmp_path):
    from app.publishers import YouTubePublisher
    from app.publishers.base import TokenProblem
    from tasks.pipeline import classify_publish_error

    dead = YouTubePublisher()
    dead.token_path = str(tmp_path / "no-token.json")
    with pytest.raises(TokenProblem) as caught:
        dead.check_ready()
    assert classify_publish_error(caught.value) == {"retryable": True, "account_level": True}


def test_a_revoked_token_is_a_token_problem_but_a_lost_network_is_not(token_publisher, monkeypatch):
    from google.auth.exceptions import RefreshError, TransportError
    from google.oauth2.credentials import Credentials

    from app.publishers.base import TokenProblem

    open(token_publisher.token_path, "w").write(json.dumps(TOKEN))
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(RefreshError("invalid_grant")))
    with pytest.raises(TokenProblem):
        token_publisher.check_ready()
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(TransportError("no route")))
    with pytest.raises(RuntimeError) as caught:
        token_publisher.check_ready()
    assert not isinstance(caught.value, TokenProblem)


# -- a rehearsal behaves like the real thing
def three_due_posts():
    return {"day": START.date().isoformat(), "posts": [
        {"slot": slot, "due": START.isoformat(), "ends": (START + timedelta(hours=4)).isoformat(),
         "state": "waiting", "attempts": 0} for slot in ("00-04", "04-08", "08-12")]}


def test_a_rehearsal_keeps_the_minimum_gap(tmp_path):
    for n in range(3):
        ready_file_clip(tmp_path, n, parent=f"p{n}")
    add_plan(three_due_posts())
    first = tick()
    assert first["youtube_id"].startswith("MOCK_")
    set_fields(first["video_id"], published_at=START)  # the upload stamps the real clock
    assert tick(10) == {"waiting": "minimum gap between uploads"}
    assert tick(settings.growth_min_gap_minutes + 1)["ok"] is True


def test_a_rehearsal_keeps_the_source_rotation(tmp_path, monkeypatch):
    rehearsed_clip(tmp_path, parent="only", published_at=START - timedelta(days=1))
    video_id = ready_file_clip(tmp_path, 1, parent="only")
    seen = []
    real = autopilot.pick_clip

    def spy(clips, current, last_parent):
        seen.append(last_parent)
        return real(clips, current, last_parent)

    monkeypatch.setattr(autopilot, "pick_clip", spy)
    add_plan()
    assert tick()["video_id"] == video_id and seen == ["only"]


# ---------- learning: what a result is measured against ----------
def dated(views, day, retention=None, length=None, video_id=None):
    arms = {"length": length} if length else {}
    return learn.Result(video_id or f"v{day}", "t", views, arms, day, retention, None, "", False)


def diagnosis_of(video_id):
    with session_scope() as db:
        return (db.get(Video, video_id).meta or {}).get("growth_diagnosis", {}).get("code")


def test_baseline_is_the_most_recent_videos_published_before_this_one():
    others = [dated(100, day) for day in range(50)]
    baseline = learn._baseline(dated(100, 40, video_id="me"), others)
    assert [o.day for o in baseline] == list(range(11, 41))


def test_the_channels_first_videos_borrow_later_ones_to_reach_the_minimum():
    others = [dated(100, day) for day in range(1, 20)]
    baseline = learn._baseline(dated(100, 0, video_id="first"), others)
    assert [o.day for o in baseline] == list(range(1, 9))


def test_a_step_up_in_reach_does_not_make_every_later_video_a_winner(tmp_path):
    low = [published_clip(tmp_path / f"l{n}", n, 10, published_at=START + timedelta(hours=8 * n), parent=f"l{n}")
           for n in range(40)]
    after = START + timedelta(days=14)
    high = [published_clip(tmp_path / f"h{n}", 100 + n, 100, published_at=after + timedelta(hours=8 * n), parent=f"h{n}")
            for n in range(40)]
    learn.learn_job(now=START + timedelta(days=15))
    assert diagnosis_of(low[-1]) != "winner"
    # The first few after the step really are above the recent normal, and the rest are the new normal.
    assert [diagnosis_of(v) for v in high[:3]] == ["winner"] * 3
    assert all(diagnosis_of(v) == "typical" for v in high[30:])


def test_a_drop_in_reach_stops_being_news_once_it_is_the_new_normal(tmp_path):
    after = START + timedelta(days=14)
    for n in range(40):
        published_clip(tmp_path / f"h{n}", n, 100, published_at=START + timedelta(hours=8 * n), parent=f"h{n}")
    low = [published_clip(tmp_path / f"l{n}", 100 + n, 15, published_at=after + timedelta(hours=8 * n), parent=f"l{n}")
           for n in range(40)]
    learn.learn_job(now=START + timedelta(days=15))
    assert all(diagnosis_of(v) == "typical" for v in low[30:])


def winner_with_baseline(tmp_path, offset=0):
    for n in range(9):
        published_clip(tmp_path / f"b{offset}_{n}", offset + n, 100, parent=f"b{offset}_{n}")
    return published_clip(tmp_path / f"w{offset}", offset + 99, 5000, parent=f"w{offset}")


def test_a_winner_is_not_mined_when_the_shelf_is_already_full(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_min_inventory", 2)
    winner = winner_with_baseline(tmp_path)
    make_clip(tmp_path / "s0.mp4", 500, parent="s0")
    make_clip(tmp_path / "s1.mp4", 501, parent="s1")
    assert learn.learn_job(now=JUDGED_AT)["diagnosed"]["winner"] == 1
    assert diagnosis_of(winner) == "winner"
    with session_scope() as db:
        assert db.execute(select(ClipJob)).first() is None


def mined_jobs():
    with session_scope() as db:
        return [(j.source, j.count) for j in db.execute(select(ClipJob).where(ClipJob.mined.is_(True))).scalars()]


def drain_stock():
    with session_scope() as db:
        for video in autopilot.ready_clips(db):
            video.status = VideoStatus.REJECTED


def full_shelf_winner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_min_inventory", 2)
    winner = winner_with_baseline(tmp_path)
    make_clip(tmp_path / "s0.mp4", 500, parent="s0")
    make_clip(tmp_path / "s1.mp4", 501, parent="s1")
    learn.learn_job(now=JUDGED_AT)
    assert mined_jobs() == []
    return winner


def test_a_winner_judged_on_a_full_shelf_is_mined_once_stock_falls(tmp_path, monkeypatch):
    winner = full_shelf_winner(tmp_path, monkeypatch)
    drain_stock()
    assert learn.learn_job(now=JUDGED_AT + timedelta(hours=6))["diagnosed"] == {}
    assert len(mined_jobs()) == 1
    with session_scope() as db:
        job_id = db.execute(select(ClipJob)).scalar_one().id
        assert db.get(Video, winner).meta["growth_diagnosis"]["mined_job"] == job_id
        assert db.execute(select(GrowthEvent).where(GrowthEvent.kind == "winner_mined")).scalar_one().data == {
            "mined_job": job_id}
        db.get(ClipJob, job_id).status = "done"
    learn.learn_job(now=JUDGED_AT + timedelta(hours=12))
    assert len(mined_jobs()) == 1  # each winner gets one follow-up, not one per run


def test_a_winner_mined_on_the_spot_is_not_owed_another_job(tmp_path):
    winner = winner_with_baseline(tmp_path)
    learn.learn_job(now=JUDGED_AT)
    with session_scope() as db:
        job = db.execute(select(ClipJob)).scalar_one()
        assert db.get(Video, winner).meta["growth_diagnosis"]["mined_job"] == job.id
        job.status = "done"
    learn.learn_job(now=JUDGED_AT + timedelta(hours=6))
    assert len(mined_jobs()) == 1


def test_supply_takes_a_waiting_winner_before_a_new_source(tmp_path, monkeypatch):
    full_shelf_winner(tmp_path, monkeypatch)
    drain_stock()
    monkeypatch.setattr(supply, "next_source", lambda: pytest.fail("took a new source over a waiting winner"))
    out = supply.supply_job()
    assert "mined" in out and "starved" not in out
    assert mined_jobs() == [("https://example.com/v", settings.growth_clips_per_source)]


def test_a_waiting_winner_ages_out_with_the_judging_window(tmp_path, monkeypatch):
    full_shelf_winner(tmp_path, monkeypatch)
    drain_stock()
    horizon = timedelta(hours=settings.growth_horizon_hours)
    learn.learn_job(now=START + horizon + timedelta(days=14, hours=1))
    assert mined_jobs() == []


def test_a_waiting_winner_stays_within_the_per_source_limit(tmp_path, monkeypatch):
    full_shelf_winner(tmp_path, monkeypatch)
    drain_stock()
    with session_scope() as db:
        for _ in range(2):
            db.add(ClipJob(source="https://example.com/v", count=3, auto_queue=True, mined=True, status="done"))
    learn.learn_job(now=JUDGED_AT + timedelta(hours=6))
    assert len(mined_jobs()) == 2


def test_a_waiting_winner_from_a_struck_source_is_left_alone(tmp_path, monkeypatch):
    winner = full_shelf_winner(tmp_path, monkeypatch)
    drain_stock()
    strike(winner, "copyright")
    learn.learn_job(now=JUDGED_AT + timedelta(hours=6))
    assert mined_jobs() == []


def test_one_run_does_not_queue_a_job_per_winning_source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_min_inventory", 9)
    for n in range(9):
        published_clip(tmp_path / f"b{n}", n, 100, parent=f"b{n}")
    for n in range(5):
        set_fields(published_clip(tmp_path / f"w{n}", 100 + n, 5000, parent=f"w{n}"),
                   source_url=f"https://example.com/w{n}")
    for n in range(8):
        make_clip(tmp_path / f"s{n}.mp4", 500 + n, parent=f"s{n}")
    assert learn.learn_job(now=JUDGED_AT)["diagnosed"]["winner"] == 5
    assert len(mined_jobs()) == 1  # one short of the minimum, and that job alone brings three


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_job_on_its_way_counts_against_the_shelf(tmp_path, monkeypatch, status):
    monkeypatch.setattr(settings, "growth_min_inventory", 3)
    winner_with_baseline(tmp_path)
    with session_scope() as db:
        db.add(ClipJob(source="https://example.com/other", count=3, auto_queue=True, status=status))
    learn.learn_job(now=JUDGED_AT)
    assert mined_jobs() == []


def test_a_job_with_no_count_counts_as_the_clipper_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_min_inventory", settings.max_clips_per_video)
    winner_with_baseline(tmp_path)
    with session_scope() as db:
        db.add(ClipJob(source="https://example.com/other", auto_queue=True))
    learn.learn_job(now=JUDGED_AT)
    assert mined_jobs() == []


def test_the_clipper_is_told_a_mined_job_is_mining(monkeypatch):
    from app.clipper import job

    seen = {}
    monkeypatch.setattr(job, "clip_source", lambda source, **k: seen.update(k) or {"ok": True})
    with session_scope() as db:
        db.add(ClipJob(source="https://example.com/m", count=3, auto_queue=True, mined=True))
    job.clip_queue_job()
    assert seen["mine"] is True and seen["job_id"]


def test_a_source_is_mined_at_most_twice(tmp_path):
    winner_with_baseline(tmp_path)
    for run in range(1, 4):
        published_clip(tmp_path / f"later{run}", 200 + run, 5000, published_at=START + timedelta(hours=run), parent=f"l{run}")
        learn.learn_job(now=JUDGED_AT)
        with session_scope() as db:
            for job in db.execute(select(ClipJob)).scalars():
                job.status = "done"
    with session_scope() as db:
        jobs = db.execute(select(ClipJob)).scalars().all()
        assert len(jobs) == 2 and all(j.source == "https://example.com/v" and j.mined for j in jobs)


def test_a_jobs_earlier_original_does_not_use_up_the_mining_budget(tmp_path):
    with session_scope() as db:
        db.add(ClipJob(source="https://example.com/v", count=3, auto_queue=True, status="done"))
    winner_with_baseline(tmp_path)
    learn.learn_job(now=JUDGED_AT)
    with session_scope() as db:
        assert db.scalar(select(func.count(ClipJob.id)).where(ClipJob.mined.is_(True))) == 1


# ---------- learning: uploads YouTube turned down ----------
def test_insights_reads_why_youtube_turned_an_upload_down():
    from app.growth.youtube import Insights

    data, _ = google_client("youtube", "v3", [{"items": [
        {"id": "a", "status": {"privacyStatus": "public", "uploadStatus": "rejected", "rejectionReason": "copyright"}},
        {"id": "b", "status": {"privacyStatus": "public", "uploadStatus": "failed", "failureReason": "codec"}},
    ]}])
    stats = Insights(StubPublisher(data=data)).stats(["a", "b"])
    assert stats["a"]["upload_status"] == "rejected" and stats["a"]["rejection_reason"] == "copyright"
    assert stats["b"]["upload_status"] == "failed" and stats["b"]["failure_reason"] == "codec"


@pytest.mark.parametrize("info, reason, hard", [
    ({"upload_status": "rejected", "rejection_reason": "copyright"}, "copyright", True),
    ({"upload_status": "rejected", "rejection_reason": "duplicate"}, "duplicate", False),
    ({"upload_status": "failed", "failure_reason": "codec"}, "codec", False),
    ({"upload_status": "deleted"}, "deleted", False),
])
def test_a_rejected_upload_is_a_strike_and_teaches_the_model_nothing(tmp_path, monkeypatch, info, reason, hard):
    video_id = published_clip(tmp_path / "a", 1, 500)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": {**stat(0), **info}}))
    assert collect.collect_job(now=START + timedelta(hours=10))["snapshots"] == 0
    with session_scope() as db:
        meta = db.get(Video, video_id).meta
        assert meta["yt_upload_status"] == info["upload_status"]
        assert meta["growth_strike"]["reason"] == reason and meta["growth_strike"]["hard"] is hard
        assert learn.matured_results(db) == []
    assert reason in events("strike")[0].summary


@pytest.mark.parametrize("status", ["processed", "uploaded"])
def test_a_live_or_processing_upload_is_learned_from_and_not_a_strike(tmp_path, monkeypatch, status):
    video_id = published_clip(tmp_path / "a", 1, 500)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": {**stat(7), "upload_status": status}}))
    assert collect.collect_job(now=START + timedelta(hours=10))["snapshots"] == 1
    with session_scope() as db:
        assert db.get(Video, video_id).meta["yt_upload_status"] == status
        assert "growth_strike" not in db.get(Video, video_id).meta
        assert len(learn.matured_results(db)) == 1
    assert events("strike") == []


def test_a_row_collected_before_the_status_was_kept_still_counts(tmp_path):
    published_clip(tmp_path / "a", 1, 500)
    with session_scope() as db:
        assert len(learn.matured_results(db)) == 1


# ---------- learning: retention is judged against clips of the same length ----------
def banded(views=1000):
    holds = {"under20": 0.95, "20-35": 0.72, "35-60": 0.55}
    return [dated(views, index / 3, retention=holds[band], length=band, video_id=f"{band}{index}")
            for index, band in enumerate(list(holds) * 20)]


def test_short_clips_are_not_favoured_for_naturally_holding_more_of_themselves():
    rows = learn.build_model(banded()).summary()["length"]
    assert max(r["multiplier"] for r in rows) < 1.1 and max(r["p_best"] for r in rows) < 0.8


def test_a_clip_that_holds_better_than_its_own_length_band_still_gains():
    typical = banded()
    better = [dated(1000, 20.0 + n / 3, retention=0.7, length="35-60", video_id=f"better{n}") for n in range(20)]
    for row in better:
        row.arms = {**row.arms, "pace": "tight"}
    for row in typical:
        row.arms = {**row.arms, "pace": "relaxed"}
    rows = {r["arm"]: r for r in learn.build_model(typical + better).summary()["pace"]}
    assert rows["tight"]["multiplier"] > rows["relaxed"]["multiplier"]


@pytest.mark.parametrize("hold, code", [(0.55, "typical"), (0.3, "weak_hold")])
def test_diagnosis_compares_hold_with_the_same_length_band(hold, code):
    others = ([dated(100, n, retention=0.55, length="35-60", video_id=f"long{n}") for n in range(6)]
              + [dated(100, n, retention=0.85, length="20-35", video_id=f"mid{n}") for n in range(6)])
    assert learn.diagnose(dated(100, 9, retention=hold, length="35-60", video_id="me"), others).code == code


def test_a_length_band_with_too_few_videos_falls_back_to_the_overall_median():
    others = ([dated(100, n, retention=0.2, length="35-60", video_id=f"long{n}") for n in range(2)]
              + [dated(100, n, retention=0.8, length="20-35", video_id=f"mid{n}") for n in range(8)])
    assert learn.diagnose(dated(100, 9, retention=0.5, length="35-60", video_id="me"), others).code == "weak_hold"
    rewards = {o.arms["length"]: o.reward for o in learn.build_model(others).observations}
    # Each is measured from 0.8, the median of all ten, because the long band has only two videos.
    assert rewards["20-35"] - rewards["35-60"] == pytest.approx(2.0 * 0.6)


# ---------- learning: views at the horizon ----------
def test_views_at_starts_from_zero_at_upload():
    assert learn.views_at([(100, 1000)], 72) == pytest.approx(720)
    assert learn.views_at([(108, 900)], 72) == pytest.approx(600)


def test_views_at_gives_up_when_first_seen_long_after_the_horizon():
    assert learn.views_at([(696, 5000)], 72) is None
    assert learn.views_at([(109, 900)], 72) is None
    assert learn.views_at([(696, 5000), (720, 5100)], 72) is None


def test_a_clip_first_seen_weeks_late_is_not_judged_or_mined(tmp_path):
    for n in range(9):
        published_clip(tmp_path / f"{n}", n, 100, parent=f"b{n}")
    old = published_clip(tmp_path / "old", 50, 5000, parent="old")
    with session_scope() as db:
        db.execute(select(VideoSnapshot).where(VideoSnapshot.video_id == old)).scalar_one().age_hours = 696
    learn.learn_job(now=JUDGED_AT)
    assert diagnosis_of(old) is None
    with session_scope() as db:
        assert db.execute(select(ClipJob)).first() is None


# ---------- learning: what the numbers are called ----------
def blended_results():
    return [dated(1000, index / 3, retention=0.9 if index % 2 else 0.6, video_id=f"r{index}") for index in range(40)]


def with_pace(rows):
    for index, row in enumerate(rows):
        row.arms = {"pace": "tight" if index % 2 else "relaxed"}
    return rows


def test_the_views_multiplier_means_views_and_the_blend_is_stored_beside_it():
    rows = {r["arm"]: r for r in learn.summarise(with_pace(blended_results()))["pace"]}
    assert rows["tight"]["multiplier"] == pytest.approx(1.0, abs=0.05)
    assert rows["tight"]["low"] < 1 < rows["tight"]["high"] and rows["tight"]["p_best"] < 0.8
    assert rows["tight"]["score"]["multiplier"] > 1.2 and rows["tight"]["score"]["p_best"] > 0.95
    assert rows["relaxed"]["score"]["multiplier"] < 0.9


def test_the_composite_reward_is_still_what_drives_choices():
    model = learn.build_model(with_pace(blended_results()))
    draw = model.draw(np.random.default_rng(0))
    assert draw.arm("pace", "tight") > draw.arm("pace", "relaxed")


def test_learn_stores_both_measures_for_the_report(tmp_path):
    for n in range(12):
        video = published_clip(tmp_path / f"{n}", n, 100, retention=0.9 if n % 2 else 0.6, parent=f"p{n}")
        with session_scope() as db:
            row = db.get(Video, video)
            row.meta = {**row.meta, "growth_title_style": "plain" if n % 2 else "question"}
    learn.learn_job(now=JUDGED_AT)
    rows = report.build_report()["factors"]["title_style"]
    assert {"multiplier", "low", "high", "p_best", "score"} <= set(rows[0])
    assert {"multiplier", "low", "high", "p_best"} == set(rows[0]["score"])


def test_the_report_script_names_each_measure(tmp_path, monkeypatch, capsys):
    for n in range(12):
        video = published_clip(tmp_path / f"{n}", n, 100, parent=f"p{n}")
        with session_scope() as db:
            row = db.get(Video, video)
            row.meta = {**row.meta, "growth_title_style": "plain" if n % 2 else "question"}
    learn.learn_job(now=JUDGED_AT)
    run_growth_script(monkeypatch, "report")
    line = next(l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("question"))
    assert "views" in line and "score" in line


# ---------- learning: analytics windows ----------
class RecordingInsights(FakeInsights):
    def __init__(self, *args, failing_share=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.failing_share, self.windows, self.shares, self.asked = set(failing_share), [], [], []

    def retention(self, ids, start, end):
        self.asked.append(list(ids))
        self.windows.append((start, end))
        return super().retention(ids, start, end)

    def feed_share(self, video_id, start, end):
        self.shares.append((start, end))
        if video_id in self.failing_share:
            raise RuntimeError("HttpError 500")
        return 0.8


def test_analytics_windows_use_youtubes_pacific_days_and_start_a_day_early(tmp_path, monkeypatch):
    # 19:00 Pacific on the 4th is already the 5th in UTC.
    published = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    published_clip(tmp_path / "a", 1, 5, published_at=published)
    fake = RecordingInsights({"yt1": stat(9)}, {"yt1": {"averageViewPercentage": 70.0}})
    monkeypatch.setattr(collect, "get_insights", lambda: fake)
    collect.collect_job(now=published + timedelta(days=3))
    window = (datetime(2026, 11, 3).date(), datetime(2026, 11, 7).date())
    assert fake.windows == [window] and fake.shares == [window]


def test_analytics_days_work_without_zone_data(monkeypatch):
    def no_data(name):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(collect, "ZoneInfo", no_data)
    assert collect._report_day(datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 4).date()


def test_one_failed_feed_share_keeps_every_retention_number(tmp_path, monkeypatch):
    first = published_clip(tmp_path / "a", 1, 5)
    second = published_clip(tmp_path / "b", 2, 5)
    numbers = {"averageViewPercentage": 70.0}
    fake = RecordingInsights({"yt1": stat(9), "yt2": stat(9)}, {"yt1": numbers, "yt2": numbers}, failing_share=["yt1"])
    monkeypatch.setattr(collect, "get_insights", lambda: fake)
    out = collect.collect_job(now=START + timedelta(days=3))
    assert out["analytics"] == 2 and "analytics_error" not in out
    with session_scope() as db:
        assert db.get(Video, first).meta["yt_analytics"]["shorts_feed_share"] is None
        assert db.get(Video, first).meta["yt_analytics"]["averageViewPercentage"] == 70.0
        assert db.get(Video, second).meta["yt_analytics"]["shorts_feed_share"] == 0.8


def test_a_missing_video_is_left_out_of_analytics_requests(tmp_path, monkeypatch):
    published_clip(tmp_path / "a", 1, 5)
    published_clip(tmp_path / "b", 2, 5)
    fake = RecordingInsights({"yt1": stat(9)}, {"yt1": {"averageViewPercentage": 70.0}})
    monkeypatch.setattr(collect, "get_insights", lambda: fake)
    collect.collect_job(now=START + timedelta(days=3))
    assert fake.asked == [["yt1"]] and len(fake.shares) == 1


# ---------- learning: the model ----------
def test_fast_early_growth_is_not_credited_to_the_latest_arm():
    observations = [Observation({"slot": "old" if d < 10 else "new"}, 3 + 0.3 * d, d) for d in np.arange(0, 20, 1 / 3)]
    rows = {r["arm"]: r["multiplier"] for r in Model(observations).summary()["slot"]}
    assert rows["new"] / rows["old"] < 1.5


def test_noise_estimate_is_near_the_truth_with_thirty_videos():
    estimates = []
    for seed in range(20):
        rng = np.random.default_rng(seed)
        observations = [Observation({
            "slot": str(rng.choice(factors.SLOTS)), "title_style": str(rng.choice(factors.TITLE_STYLES)),
            "kind": str(rng.choice(["speech", "visual"])), "length": str(rng.choice(["under20", "20-35", "35-60"])),
            "series": f"s{index // 3}", "pace": str(rng.choice(list(factors.PACES))),
            "sfx": str(rng.choice(factors.SWITCHES)), "broll": str(rng.choice(factors.SWITCHES)),
        }, 5 + rng.normal(0, 1), index / 3) for index in range(30)]
        estimates.append(Model(observations).noise)
    assert 0.75 < float(np.mean(estimates)) < 1.25


# ---------- learning: findings ----------
def finding_row(arm="on", p_best=0.97, n=10):
    return {"arm": arm, "n": n, "multiplier": 1.4, "low": 1.1, "high": 1.8, "p_best": p_best}


def note_finding(p_best, matured, *, factor="sfx", n=10):
    with session_scope() as db:
        return learn._note_findings(db, {factor: [finding_row(p_best=p_best, n=n)]}, matured)


def run_findings(*p_bests, factor="sfx", n=10, start=100, step=learn._CONFIRM_VIDEOS):
    """Each run brings `step` new videos, so a confirmation needs one run after the first strong one."""
    return sum(note_finding(p, start + i * step, factor=factor, n=n) for i, p in enumerate(p_bests))


def test_a_finding_must_hold_across_new_videos():
    assert note_finding(0.97, 100) == 0
    assert note_finding(0.97, 100 + learn._CONFIRM_VIDEOS - 1) == 0 and events("finding") == []
    assert note_finding(0.97, 100 + learn._CONFIRM_VIDEOS) == 1
    assert "sfx = on" in events("finding")[0].summary


def test_runs_over_the_same_videos_are_one_look():
    assert sum(note_finding(0.99, 100) for _ in range(20)) == 0


def test_learn_runs_with_no_new_video_log_no_finding(tmp_path, monkeypatch):
    for n in range(9):
        published_clip(tmp_path / f"{n}", n, 100, parent=f"p{n}")
    monkeypatch.setattr(learn, "summarise", lambda results: {"sfx": [finding_row(p_best=0.96)]})
    found = [learn.learn_job(now=JUDGED_AT + timedelta(hours=6 * run))["findings"] for run in range(4)]
    assert found == [0, 0, 0, 0]


def test_a_lapse_restarts_the_count():
    assert run_findings(0.97, 0.9, 0.97) == 0
    assert note_finding(0.97, 100 + 3 * learn._CONFIRM_VIDEOS - 1) == 0


@pytest.mark.parametrize("p_best, n", [(0.94, 10), (0.97, 7)])
def test_a_finding_needs_strong_evidence_and_enough_videos(p_best, n):
    assert run_findings(p_best, p_best, p_best, p_best, n=n) == 0


def test_a_finding_is_only_logged_once_while_it_holds():
    assert run_findings(0.97, 0.97, 0.97, 0.97, 0.8, 0.97) == 1
    assert events("finding_retracted") == []


def test_a_finding_is_retracted_when_the_evidence_fades_and_can_return():
    run_findings(0.97, 0.97)
    run_findings(0.69, start=200)
    retracted = events("finding_retracted")
    assert len(retracted) == 1 and "sfx = on" in retracted[0].summary
    run_findings(0.69, start=300)
    assert len(events("finding_retracted")) == 1
    assert run_findings(0.97, 0.97, start=400) == 1


def test_day_type_is_never_a_finding():
    assert run_findings(0.99, 0.99, 0.99, factor="day_type") == 0


def test_a_saved_old_style_findings_state_does_not_break_the_job():
    with session_scope() as db:
        db.add(GrowthState(key="findings", value={"sfx=on": 1.4}))
    assert run_findings(0.97, 0.97) == 1


def test_a_streak_saved_by_an_older_version_does_not_count_as_confirmation():
    with session_scope() as db:
        db.add(GrowthState(key="findings", value={"live": {}, "streaks": {"sfx=on": 2}}))
    assert note_finding(0.97, 100) == 0


def _null_world_findings(seed, days=60, per_day=3):
    arms = {"slot": 6, "title_style": 4, "kind": 2, "length": 4, "pace": 3, "sfx": 2, "broll": 2}
    rng = np.random.default_rng(seed)
    observations, found = [], 0
    with session_scope() as db:
        db.execute(delete(GrowthState))
    for index in range(days * per_day):
        picked = {factor: f"a{rng.integers(size)}" for factor, size in arms.items()}
        observations.append(Observation(picked, 5 + rng.normal(0, 1), index / per_day))
        if index >= 8:
            with session_scope() as db:
                found += learn._note_findings(db, Model(observations).summary(), len(observations))
    return found


@pytest.mark.slow
def test_a_channel_where_nothing_matters_usually_logs_no_false_finding():
    assert sum(_null_world_findings(seed) > 0 for seed in range(12)) <= 3


# ---------- learning: subscribers ----------
def analytics_clip(path, n, views, gained):
    video_id = published_clip(path, n, 500, parent=f"p{n}")
    with session_scope() as db:
        video = db.get(Video, video_id)
        numbers = {"views": views, "averageViewPercentage": 70.0}
        if gained is not None:
            numbers["subscribersGained"] = gained
        video.meta = {**video.meta, "yt_analytics": numbers}
    return video_id


def test_subscribers_are_a_rate_so_an_older_video_has_no_head_start(tmp_path):
    old = analytics_clip(tmp_path / "old", 1, 15000, 60)
    young = analytics_clip(tmp_path / "young", 2, 2000, 8)
    with session_scope() as db:
        rows = {r.video_id: r for r in learn.matured_results(db)}
    assert rows[old].subscribers == pytest.approx(4.0) and rows[young].subscribers == pytest.approx(4.0)
    assert learn.reward(rows[old], 0.7) == pytest.approx(learn.reward(rows[young], 0.7))


@pytest.mark.parametrize("views, gained", [(0, 3), (None, 3), (2000, None)])
def test_subscribers_with_no_rate_stay_unknown(tmp_path, views, gained):
    analytics_clip(tmp_path / "a", 1, views, gained)
    with session_scope() as db:
        assert learn.matured_results(db)[0].subscribers is None


def test_missing_subscribers_count_as_typical():
    rows = [learn.Result(f"v{n}", "", 100, {}, n, None, subs, "", False) for n, subs in enumerate((2.0, 4.0, 6.0, None))]
    rewards = [o.reward for o in learn.build_model(rows).observations]
    assert rewards[3] == pytest.approx(rewards[1])


# ---------- learning: when a video is judged ----------
def test_an_early_breakout_is_judged_and_mined_once_there_is_a_baseline(tmp_path):
    hit = published_clip(tmp_path / "hit", 0, 100000, parent="hit")
    assert learn.learn_job(now=JUDGED_AT)["diagnosed"] == {"too_early": 1}
    assert diagnosis_of(hit) is None and events("diagnosis:too_early") == []
    for n in range(1, 10):
        published_clip(tmp_path / f"{n}", n, 100, published_at=START + timedelta(hours=8 * n), parent=f"p{n}")
        learn.learn_job(now=JUDGED_AT)
    assert diagnosis_of(hit) == "winner"
    with session_scope() as db:
        assert len(db.execute(select(ClipJob)).scalars().all()) == 1


def test_a_too_early_row_saved_by_an_older_version_gets_judged(tmp_path):
    hit = published_clip(tmp_path / "hit", 0, 100000, parent="hit")
    with session_scope() as db:
        video = db.get(Video, hit)
        video.meta = {**video.meta, "growth_diagnosis": {"code": "too_early", "reason": "", "focus": []}}
    for n in range(1, 10):
        published_clip(tmp_path / f"{n}", n, 100, published_at=START + timedelta(hours=8 * n), parent=f"p{n}")
    learn.learn_job(now=JUDGED_AT)
    assert diagnosis_of(hit) == "winner"


def test_videos_long_past_the_horizon_are_not_all_judged_at_once(tmp_path):
    horizon = timedelta(hours=settings.growth_horizon_hours)
    old = published_clip(tmp_path / "old", 0, 100000, parent="old")
    edge = published_clip(tmp_path / "edge", 1, 100000, published_at=START + timedelta(days=1), parent="edge")
    for n in range(2, 12):
        published_clip(tmp_path / f"{n}", n, 100, published_at=START + timedelta(days=2), parent=f"p{n}")
    out = learn.learn_job(now=START + horizon + timedelta(days=14, hours=1))
    assert diagnosis_of(old) is None and diagnosis_of(edge) == "winner"
    assert sum(out["diagnosed"].values()) == 11


# ---------- supply: feeds ----------
def tab(name, ids):
    # What yt-dlp 2026.10.26 returned for one tab of https://www.youtube.com/@TED under extract_flat:
    # a playlist with no url of its own, and flat url entries inside it.
    return {"_type": "playlist", "id": "UCAuUUnT6oDeKwE6v1NGQxug", "title": f"TED - {name}", "url": None,
            "webpage_url": f"https://www.youtube.com/@TED/{name.lower()}", "extractor": "youtube:tab",
            "entries": [{"_type": "url", "ie_key": "Youtube", "id": i, "title": f"Talk {i}",
                         "url": f"https://www.youtube.com/watch?v={i}", "channel_id": None, "uploader": None}
                        for i in ids]}


def channel_page(*tabs):
    return {"_type": "playlist", "id": "UCAuUUnT6oDeKwE6v1NGQxug", "title": "TED", "channel": "TED",
            "channel_id": "UCAuUUnT6oDeKwE6v1NGQxug", "uploader_id": "@TED", "entries": list(tabs)}


def fake_ydl(monkeypatch, read):
    """A stand-in for yt-dlp that records what it was asked for, and with which options."""
    import yt_dlp

    calls = []

    class FakeYdl:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            assert download is False
            calls.append((url, self.options))
            return read(url, self.options)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYdl)
    return calls


def uploads(count):
    """A flat feed of `count` videos, answering only as deep as playlistend asks."""
    links = [f"https://example.com/{n}" for n in range(count)]
    return lambda url, options: {"entries": [{"url": link} for link in links[:options["playlistend"]]]}


def use_feeds(monkeypatch, tmp_path, *feeds):
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "growth_feeds", json.dumps(list(feeds)))


def add_job(source, status="done", *, age_hours=0, **result):
    with session_scope() as db:
        db.add(ClipJob(source=source, status=status, result=result,
                       finished_at=datetime.now(timezone.utc) - timedelta(hours=age_hours)))


def test_a_multi_tab_channel_page_yields_the_videos_inside_its_tabs(monkeypatch):
    page = channel_page(tab("Videos", ["v1", "v2"]), tab("Live", ["l1"]), tab("Shorts", ["s1"]))
    fake_ydl(monkeypatch, lambda url, options: page)
    assert list(supply._feed_links("https://www.youtube.com/@TED/featured")) == [
        f"https://www.youtube.com/watch?v={i}" for i in ("v1", "v2", "l1", "s1")]


@pytest.mark.parametrize("bare", ["https://www.youtube.com/@TED", "https://www.youtube.com/@TED/",
                                  "https://youtube.com/channel/UCAuUUnT6oDeKwE6v1NGQxug",
                                  "https://m.youtube.com/c/TED", "http://www.youtube.com/user/TEDtalks"])
def test_a_bare_channel_link_is_read_through_its_videos_tab(monkeypatch, bare):
    calls = fake_ydl(monkeypatch, lambda url, options: tab("Videos", ["v1"]))
    assert list(supply._feed_links(bare)) == ["https://www.youtube.com/watch?v=v1"]
    assert calls[0][0] == bare.rstrip("/") + "/videos"


@pytest.mark.parametrize("given", ["https://www.youtube.com/@TED/shorts", "https://example.com/@TED",
                                   "https://www.youtube.com/playlist?list=UUAuUUnT6oDeKwE6v1NGQxug"])
def test_other_feed_links_are_read_as_given(monkeypatch, given):
    calls = fake_ydl(monkeypatch, lambda url, options: tab("Videos", ["v1"]))
    list(supply._feed_links(given))
    assert calls[0][0] == given


def test_a_channel_with_no_videos_tab_is_still_read(monkeypatch):
    def read(url, options):
        if url.endswith("/videos"):
            raise RuntimeError("This channel does not have a videos tab")
        return tab("Shorts", ["s1"])

    fake_ydl(monkeypatch, read)
    assert list(supply._feed_links("https://www.youtube.com/@ShortsOnly")) == ["https://www.youtube.com/watch?v=s1"]


def test_a_blocked_author_in_a_nested_tab_gives_no_links(monkeypatch, tmp_path):
    named = {**tab("Videos", ["v1"]), "channel_id": "UCbad"}
    fake_ydl(monkeypatch, lambda url, options: {"entries": [named, tab("Live", ["l1"])]})
    monkeypatch.setattr(safety, "blocked_authors", lambda db: {"ucbad"})
    assert list(supply._feed_links("https://example.com/mixed")) == ["https://www.youtube.com/watch?v=l1"]


def test_a_feed_with_no_links_warns_and_records_one_event(monkeypatch, tmp_path, caplog):
    use_feeds(monkeypatch, tmp_path, "https://example.com/empty")
    fake_ydl(monkeypatch, lambda url, options: {"entries": []})
    with caplog.at_level("WARNING", logger="app.growth.supply"):
        assert supply.next_source() is None
        assert supply.next_source() is None
    assert "https://example.com/empty" in caplog.text
    recorded = events("feed_empty")
    assert len(recorded) == 1 and "https://example.com/empty" in recorded[0].summary


def test_each_empty_feed_gets_its_own_event(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/a", "https://example.com/b")
    fake_ydl(monkeypatch, lambda url, options: {"entries": []})
    supply.next_source()
    assert len(events("feed_empty")) == 2


def test_a_blocked_feed_is_not_reported_as_empty(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/theirs")
    fake_ydl(monkeypatch, lambda url, options: {"channel_id": "UCbad", "entries": [{"url": "https://example.com/1"}]})
    monkeypatch.setattr(safety, "blocked_authors", lambda db: {"ucbad"})
    assert supply.next_source() is None
    assert events("feed_empty") == []


def test_feed_listing_uses_the_same_proxy_and_cookies_as_downloads(monkeypatch, tmp_path):
    from app.clipper import ingest

    monkeypatch.setattr(settings, "proxy_url", "http://proxy:1")
    monkeypatch.setattr(settings, "cookies_from_browser", "firefox,work")
    calls = fake_ydl(monkeypatch, uploads(1))
    list(supply._feed_links("https://example.com/feed"))
    options = calls[0][1]
    download = ingest._ydl_options(tmp_path)
    assert options["proxy"] == download["proxy"] == "http://proxy:1"
    assert options["cookiesfrombrowser"] == download["cookiesfrombrowser"] == ("firefox", "work")


def test_feed_listing_has_no_proxy_or_cookies_unless_set(monkeypatch):
    monkeypatch.setattr(settings, "proxy_url", "")
    monkeypatch.setattr(settings, "cookies_from_browser", "")
    calls = fake_ydl(monkeypatch, uploads(1))
    list(supply._feed_links("https://example.com/feed"))
    assert "proxy" not in calls[0][1] and "cookiesfrombrowser" not in calls[0][1]


def test_a_used_up_feed_reads_further_back_only_when_it_must(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    calls = fake_ydl(monkeypatch, uploads(400))
    assert supply.next_source()[0] == "https://example.com/0"
    assert [c[1]["playlistend"] for c in calls] == [15]
    for n in range(15):
        add_job(f"https://example.com/{n}")
    calls.clear()
    assert supply.next_source()[0] == "https://example.com/15"
    assert [c[1]["playlistend"] for c in calls] == [15, 60]
    for n in range(15, 60):
        add_job(f"https://example.com/{n}")
    calls.clear()
    assert supply.next_source()[0] == "https://example.com/60"
    assert [c[1]["playlistend"] for c in calls] == [15, 60, 240]


def test_reading_back_stops_at_the_end_of_the_feed_and_at_the_deepest_step(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    calls = fake_ydl(monkeypatch, uploads(20))
    for n in range(20):
        add_job(f"https://example.com/{n}")
    assert supply.next_source() is None
    assert [c[1]["playlistend"] for c in calls] == [15, 60]
    calls = fake_ydl(monkeypatch, uploads(5000))
    assert len(list(supply._feed_links("https://example.com/feed"))) == 1000
    assert [c[1]["playlistend"] for c in calls] == [15, 60, 240, 1000]


def test_supply_takes_turns_across_feeds(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/A", "https://example.com/B", "https://example.com/C")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: [f"{feed}/{n}" for n in range(5)])
    taken = []
    for _ in range(4):
        taken.append(supply.supply_job()["queued"])
        with session_scope() as db:
            db.execute(select(ClipJob).where(ClipJob.status == "queued")).scalar_one().status = "done"
    assert taken == ["https://example.com/A/0", "https://example.com/B/0", "https://example.com/C/0",
                     "https://example.com/A/1"]


def test_a_feed_with_nothing_new_gives_its_turn_to_the_next(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/A", "https://example.com/B")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/A/0"] if feed.endswith("A")
                        else ["https://example.com/B/0"])
    add_job("https://example.com/A/0", feed="https://example.com/B")
    add_job("https://example.com/X", feed="https://example.com/B")
    assert supply.next_source() == ("https://example.com/B/0", "https://example.com/B")


def test_inbox_files_still_come_ahead_of_every_feed(monkeypatch, tmp_path):
    clip = settled_file(tmp_path / "mine.mp4")
    use_feeds(monkeypatch, tmp_path, "https://example.com/A")
    add_job("https://example.com/other", feed="https://example.com/A")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/A/0"])
    assert supply.next_source() == (str(clip.resolve()), None)


# ---------- supply: what counts as used ----------
def settled_file(path, body=b"x"):
    import os

    path.write_bytes(body)
    os.utime(path, (1000, 1000))
    return path


@pytest.mark.parametrize("failures, hours_ago, retried", [
    (1, 5, False), (1, 7, True),
    (2, 11, False), (2, 13, True),
    (3, 17, False), (3, 19, True),
    (4, 1000, False),
])
def test_a_failure_that_may_clear_up_is_retried_with_a_growing_wait(monkeypatch, tmp_path, failures, hours_ago, retried):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/1", "https://example.com/2"])
    for extra in range(failures):
        add_job("https://example.com/1", "failed", age_hours=hours_ago + extra, ok=False, error="download failed")
    assert supply.next_source()[0] == ("https://example.com/1" if retried else "https://example.com/2")


def test_a_failed_job_with_no_finish_time_waits_from_when_it_was_queued(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/1"])
    with session_scope() as db:
        db.add(ClipJob(source="https://example.com/1", status="failed",
                       created_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    assert supply.next_source() is None
    with session_scope() as db:
        db.execute(select(ClipJob)).scalar_one().created_at = datetime.now(timezone.utc) - timedelta(hours=7)
    assert supply.next_source()[0] == "https://example.com/1"


@pytest.mark.parametrize("status, result", [("done", {"ok": True}), ("queued", {}), ("running", {}),
                                            ("failed", {"ok": False, "permanent": True})])
def test_a_finished_active_or_hopeless_source_is_never_taken_again(monkeypatch, tmp_path, status, result):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/1"])
    add_job("https://example.com/1", status, age_hours=10000, **result)
    assert supply.next_source() is None


def test_a_done_job_outranks_earlier_failures_of_the_same_source(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/1"])
    add_job("https://example.com/1", "failed", age_hours=100)
    add_job("https://example.com/1", "done", age_hours=90)
    assert supply.next_source() is None


def test_a_transient_failure_does_not_burn_an_inbox_file(monkeypatch, tmp_path):
    clip = settled_file(tmp_path / "mine.mp4")
    use_feeds(monkeypatch, tmp_path)
    job_id = supply.supply_job()["job"]
    with session_scope() as db:
        job = db.get(ClipJob, job_id)
        job.status, job.finished_at = "failed", datetime.now(timezone.utc) - timedelta(hours=1)
    assert supply.next_source() is None
    with session_scope() as db:
        db.get(ClipJob, job_id).finished_at = datetime.now(timezone.utc) - timedelta(hours=7)
    assert supply.next_source() == (str(clip.resolve()), None)


def test_a_replaced_inbox_file_is_a_new_source(monkeypatch, tmp_path):
    from app.clipper.job import _finish_job

    clip = settled_file(tmp_path / "mine.mp4")
    use_feeds(monkeypatch, tmp_path)
    job_id = supply.supply_job()["job"]
    _finish_job(job_id, {"ok": True, "clips": []})
    assert supply.next_source() is None
    settled_file(clip, b"the finished copy, not the partial one")
    assert supply.next_source() == (str(clip.resolve()), None)


def test_an_inbox_job_with_no_fingerprint_still_closes_its_path(monkeypatch, tmp_path):
    clip = settled_file(tmp_path / "mine.mp4")
    use_feeds(monkeypatch, tmp_path)
    add_job(str(clip.resolve()))
    assert supply.next_source() is None


def test_a_job_keeps_its_fingerprint_and_feed_when_it_finishes():
    from app.clipper.job import _finish_job, enqueue

    job_id = enqueue("https://example.com/1", feed="https://example.com/feed")
    _finish_job(job_id, {"ok": False, "error": "nope"})
    with session_scope() as db:
        result = db.get(ClipJob, job_id).result
    assert result == {"feed": "https://example.com/feed", "ok": False, "error": "nope"}


# ---------- supply: inbox ----------
def test_a_dangling_symlink_does_not_stop_supply(monkeypatch, tmp_path):
    good = settled_file(tmp_path / "good.mp4")
    (tmp_path / "gone.mp4").symlink_to(tmp_path / "moved-away.mp4")
    use_feeds(monkeypatch, tmp_path)
    assert supply.next_source() == (str(good.resolve()), None)


def test_a_file_vanishing_mid_scan_does_not_stop_the_feeds(monkeypatch, tmp_path):
    from pathlib import Path

    settled_file(tmp_path / "vanishing.mp4")
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/1"])
    real_stat = Path.stat

    def stat(self, **kwargs):
        if self.name == "vanishing.mp4" and not kwargs:
            raise FileNotFoundError(self)
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert supply.next_source() == ("https://example.com/1", "https://example.com/feed")


def test_directories_and_other_files_in_the_inbox_are_ignored(monkeypatch, tmp_path):
    (tmp_path / "folder.mp4").mkdir()
    settled_file(tmp_path / "notes.txt")
    use_feeds(monkeypatch, tmp_path)
    assert supply.open_inbox_files() == []


def content_source():
    return checks_by_name()["content source"]


def test_doctor_does_not_count_files_supply_would_ignore(monkeypatch, tmp_path):
    settled_file(tmp_path / "notes.txt")
    (tmp_path / ".gitkeep").write_bytes(b"")
    used = settled_file(tmp_path / "used.mp4")
    use_feeds(monkeypatch, tmp_path)
    add_job(str(used.resolve()))
    assert content_source()["ok"] is False
    assert "0 inbox files ready" in content_source()["detail"]


def test_doctor_counts_a_file_supply_would_cut(monkeypatch, tmp_path):
    settled_file(tmp_path / "fresh.mp4")
    settled_file(tmp_path / "another.mkv")
    use_feeds(monkeypatch, tmp_path)
    monkeypatch.setattr(report.ai, "available", lambda: True)
    assert content_source()["ok"] is True
    assert "2 inbox files ready" in content_source()["detail"]


def test_doctor_does_not_count_a_feed_that_just_resolved_to_nothing(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/empty")
    fake_ydl(monkeypatch, lambda url, options: {"entries": []})
    assert content_source()["ok"] is True
    supply.next_source()
    assert content_source()["ok"] is False
    assert "https://example.com/empty" in content_source()["detail"]


def test_doctor_stays_off_the_network(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: pytest.fail("the dashboard runs doctor on every refresh"))
    assert content_source()["ok"] is True


# ---------- titles: never a made-up quotation ----------
def test_quote_title_needs_a_real_line():
    assert titles.styled_title("quote", "Boring", "") == ("Boring #Shorts", "plain")
    assert titles.styled_title("quote", "Boring", "x" * 200) == ("Boring #Shorts", "plain")


@pytest.mark.parametrize("meta, handed_over", [({"clip_opening_line": "He really said this."}, "He really said this."),
                                                ({}, "")])
def test_autopilot_hands_titles_the_transcript_never_the_hook(monkeypatch, tmp_path, publisher, meta, handed_over):
    seen = []

    def styled(style, title, opening_line):
        seen.append(opening_line)
        return f"{title} #Shorts", "plain"

    monkeypatch.setattr(titles, "styled_title", styled)
    make_clip(tmp_path / "a", **meta)  # its clip_hook is "Nobody told me this"
    autopilot._publish_next(policy(), START, "post")
    assert seen == [handed_over]


def test_ai_titles_get_no_opening_line_to_lean_on_when_there_is_none(monkeypatch):
    prompts = []
    monkeypatch.setattr(titles.ai, "ask_json", lambda prompt: prompts.append(prompt) or {"title": "Why?"})
    assert titles.styled_title("question", "My title", "")[0] == "Why? #Shorts"
    assert "Opening line" not in prompts[0]
    titles.styled_title("question", "My title", "Real words.")
    assert "Opening line: Real words." in prompts[1]


# ---------- recipes ----------
def test_a_recipe_does_not_read_another_renders_override(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(settings, "clip_sfx", True)
    seen = {}

    def second_render():
        with recipes.applied({"pace": "normal", "sfx": "on", "broll": "off"}):
            seen["sfx"] = settings.clip_sfx

    with recipes.applied({"pace": "normal", "sfx": "off", "broll": "off"}):
        other = threading.Thread(target=second_render)
        other.start()
        time.sleep(0.2)  # long enough to reach the lock, while this render's override is live
    other.join(5)
    assert seen["sfx"] is True


# ---------- a transcript line in a public title ----------
def test_the_last_gate_screens_the_line_a_quote_title_would_use(tmp_path):
    video_id = make_clip(tmp_path / "c.mp4", 700, clip_source_text="A long talk",
                         clip_opening_line="I lost everything at the casino.")
    with session_scope() as db:
        assert "blocked term" in clip_gate_reason(db.get(Video, video_id))
        assert autopilot._screen(db, [db.get(Video, video_id)], START) == []
        assert db.get(Video, video_id).status == VideoStatus.REJECTED


def test_a_clean_opening_line_passes_the_gate(tmp_path):
    video_id = make_clip(tmp_path / "c.mp4", 701, clip_source_text="A long talk", clip_opening_line="Nobody told me.")
    with session_scope() as db:
        assert clip_gate_reason(db.get(Video, video_id)) is None


# ---------- a mined job that fails ----------
def fail_mined_job(at, **result):
    with session_scope() as db:
        job = db.execute(select(ClipJob).where(ClipJob.mined.is_(True)).order_by(ClipJob.created_at.desc())).scalars().first()
        job.status, job.finished_at, job.result = "failed", at, {"ok": False, **result}
        return job.id


def mined_job_of(video_id):
    with session_scope() as db:
        return db.get(Video, video_id).meta["growth_diagnosis"].get("mined_job")


def test_a_failed_mined_job_is_tried_again_after_the_wait(tmp_path):
    winner = winner_with_baseline(tmp_path)
    learn.learn_job(now=JUDGED_AT)
    first = fail_mined_job(JUDGED_AT)
    drain_stock()
    learn.learn_job(now=JUDGED_AT + timedelta(hours=5))
    assert len(mined_jobs()) == 1
    learn.learn_job(now=JUDGED_AT + timedelta(hours=7))
    assert len(mined_jobs()) == 2
    assert mined_job_of(winner) != first


def test_a_mined_job_that_keeps_failing_stops_like_any_other_source(tmp_path):
    winner_with_baseline(tmp_path)
    learn.learn_job(now=JUDGED_AT)
    drain_stock()
    at = JUDGED_AT
    for tries in range(1, 7):
        fail_mined_job(at)
        at += timedelta(hours=6 * tries + 1)
        learn.learn_job(now=at)
    assert len(mined_jobs()) == 4  # the first try, and supply's three retries


@pytest.mark.parametrize("status, result", [("failed", {"permanent": True}), ("done", {})])
def test_a_mined_job_that_finished_or_cannot_work_is_not_tried_again(tmp_path, status, result):
    winner_with_baseline(tmp_path)
    learn.learn_job(now=JUDGED_AT)
    job_id = fail_mined_job(JUDGED_AT, **result)
    with session_scope() as db:
        db.get(ClipJob, job_id).status = status
    drain_stock()
    learn.learn_job(now=JUDGED_AT + timedelta(days=2))
    assert len(mined_jobs()) == 1


def test_a_failure_that_may_clear_up_does_not_use_up_the_mining_budget(tmp_path):
    winner_with_baseline(tmp_path)
    learn.learn_job(now=JUDGED_AT)
    fail_mined_job(JUDGED_AT)
    drain_stock()
    with session_scope() as db:
        source = db.execute(select(ClipJob.source)).scalars().first()
        db.add(ClipJob(source=source, mined=True, status="done", finished_at=START, created_at=START))
        db.add(ClipJob(source=source, mined=True, status="failed", finished_at=START, created_at=START))
    learn.learn_job(now=JUDGED_AT + timedelta(hours=13))
    assert len(mined_jobs()) == 4


# ---------- a source that keeps stopping the app ----------
def test_a_source_stopped_too_many_times_is_closed_for_good(monkeypatch, tmp_path):
    from app.clipper.job import _MAX_ATTEMPTS, _next_job, enqueue

    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    monkeypatch.setattr(supply, "_feed_links", lambda feed: ["https://example.com/1"])
    job_id = enqueue("https://example.com/1", feed="https://example.com/feed")
    for _ in range(_MAX_ATTEMPTS):
        assert _next_job()[0] == job_id
    assert _next_job() is None
    with session_scope() as db:
        job = db.get(ClipJob, job_id)
        assert job.status == "failed" and "stopped" in job.error
        assert job.result["permanent"] is True and job.result["feed"] == "https://example.com/feed"
        job.finished_at = datetime.now(timezone.utc) - timedelta(days=30)
    assert supply.next_source() is None


# ---------- the same bytes under a new mtime ----------
def test_touching_a_finished_inbox_file_does_not_reopen_it(monkeypatch, tmp_path):
    import os

    from app.clipper.job import _finish_job

    clip = settled_file(tmp_path / "mine.mp4")
    use_feeds(monkeypatch, tmp_path)
    _finish_job(supply.supply_job()["job"], {"ok": True, "clips": []})
    assert supply.open_inbox_files() == []
    os.utime(clip, (2000, 2000))
    assert supply.open_inbox_files() == []


def test_a_touched_or_copied_file_finds_the_clips_it_already_made(monkeypatch, tmp_path):
    import os
    import shutil

    from app.clipper.ingest import fetch
    from app.clipper.job import _earlier_clips, _save_clip
    from app.clipper.moments import Moment

    clip = settled_file(tmp_path / "mine.mp4", b"the same bytes")
    src = fetch(str(clip))
    (tmp_path / "out.mp4").write_bytes(b"x")
    _save_clip(src, Moment(0, 30, title="Written", ai_title=True), str(tmp_path / "out.mp4"), 30.0, language="en", auto_queue=True)
    os.utime(clip, (2000, 2000))
    assert [cid for cid, _ in _earlier_clips(fetch(str(clip)))] == [f"{src.source_id}_0_30"]
    shutil.copy(clip, tmp_path / "copy.mp4")
    assert [cid for cid, _ in _earlier_clips(fetch(str(tmp_path / "copy.mp4")))] == [f"{src.source_id}_0_30"]


# ---------- clips nobody will ever see ----------
def test_doctor_says_inbox_files_alone_cannot_stock_without_ai(monkeypatch, tmp_path):
    settled_file(tmp_path / "fresh.mp4")
    use_feeds(monkeypatch, tmp_path)
    monkeypatch.setattr(report.ai, "available", lambda: False)
    assert content_source()["ok"] is False
    assert "AI CLI" in content_source()["detail"] and "review" in content_source()["detail"]
    ai_check = checks_by_name()["AI (OpenCode or Claude)"]
    assert "review" in ai_check["detail"] and ai_check["required"] and not ai_check["ok"]


def test_doctor_still_counts_inbox_files_while_stock_or_feeds_exist_without_ai(monkeypatch, tmp_path):
    settled_file(tmp_path / "fresh.mp4")
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    monkeypatch.setattr(report.ai, "available", lambda: False)
    assert content_source()["ok"] is True


def test_doctor_reports_clips_held_for_review_and_why(tmp_path):
    make_clip(tmp_path / "a.mp4", 710, status=VideoStatus.PENDING, growth_flagged="no AI-written title")
    make_clip(tmp_path / "b.mp4", 711, status=VideoStatus.PENDING, growth_flagged="no AI-written title")
    make_clip(tmp_path / "c.mp4", 712, status=VideoStatus.PENDING)
    make_clip(tmp_path / "d.mp4", 713, status=VideoStatus.REJECTED, growth_flagged="decided already")
    held = checks_by_name()["held for review"]
    assert held["ok"] is False and held["required"] is False
    assert "2 clips" in held["detail"] and "no AI-written title" in held["detail"]
    assert "2 clips held for review" in content_source()["detail"]  # the check the dashboard's banner shows


def test_doctor_is_quiet_when_nothing_is_held():
    assert checks_by_name()["held for review"]["ok"] is True


def test_holding_a_clip_for_review_is_recorded_where_the_owner_looks(tmp_path):
    saved_clip(tmp_path, title="Inside a casino")
    saved_clip(tmp_path, title="Inside a casino", moment_title="Another")
    with session_scope() as db:
        held = db.execute(select(GrowthEvent).where(GrowthEvent.kind == "clip_held")).scalars().all()
    assert len(held) == 1 and "blocked term" in held[0].summary and "review" in held[0].summary


def test_a_clean_clip_records_no_hold(tmp_path):
    saved_clip(tmp_path)
    with session_scope() as db:
        assert db.execute(select(GrowthEvent).where(GrowthEvent.kind == "clip_held")).first() is None


# ---------- a channel link shared with a query string ----------
@pytest.mark.parametrize("given", ["https://www.youtube.com/@TED?si=abc", "https://www.youtube.com/@TED/?si=abc",
                                   "https://youtube.com/channel/UCabc?si=abc&t=1", "https://www.youtube.com/@TED#about"])
def test_a_shared_channel_link_is_still_read_through_its_videos_tab(monkeypatch, given):
    calls = fake_ydl(monkeypatch, lambda url, options: tab("Videos", ["v1"]))
    list(supply._feed_links(given))
    assert calls[0][0] == given.split("?")[0].split("#")[0].rstrip("/") + "/videos"


@pytest.mark.parametrize("given", ["https://www.youtube.com/@TED/shorts?si=abc",
                                   "https://www.youtube.com/watch?v=abc"])
def test_a_link_to_something_else_keeps_its_query(monkeypatch, given):
    calls = fake_ydl(monkeypatch, lambda url, options: tab("Videos", ["v1"]))
    list(supply._feed_links(given))
    assert calls[0][0] == given


# ---------- operations: what the owner is told ----------
def google_refusal(status, reason, message="refused"):
    import httplib2
    from googleapiclient.errors import HttpError

    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


class LiveInsights:
    def __init__(self, *, token=None, channel="My Channel", channel_error=None, analytics_error=None):
        self.token, self.channel, self.channel_error, self.analytics_error = token, channel, channel_error, analytics_error
        self.calls = []

    def verify_token(self):
        self.calls.append("token")
        if self.token:
            raise self.token

    def channel_name(self):
        self.calls.append("channel")
        if self.channel_error:
            raise self.channel_error
        return self.channel

    def probe_analytics(self):
        self.calls.append("analytics")
        if self.analytics_error:
            raise self.analytics_error


def live_checks(monkeypatch, insights):
    monkeypatch.setattr(report, "get_insights", lambda: insights)
    return {c["name"]: c for c in report.doctor(live=True)["checks"]}


# -- doctor can ask Google
def test_doctor_is_offline_unless_asked(monkeypatch):
    monkeypatch.setattr(report, "get_insights", lambda: pytest.fail("offline doctor must not touch YouTube"))
    monkeypatch.setattr(supply, "first_link", lambda feed: pytest.fail("offline doctor must not read feeds"))
    monkeypatch.setattr(settings, "growth_feeds", '["https://example.com/feed"]')
    assert "channel" not in checks_by_name()


def test_live_doctor_names_the_channel_it_reached(monkeypatch):
    insights = LiveInsights()
    checks = live_checks(monkeypatch, insights)
    assert insights.calls == ["token", "channel", "analytics"]
    assert checks["token refresh"]["ok"] and checks["analytics read"]["ok"]
    assert checks["channel"]["ok"] and "My Channel" in checks["channel"]["detail"]


def test_live_doctor_fails_a_dead_token_and_explains_testing_mode(monkeypatch, token_publisher):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    from app.growth.youtube import Insights

    open(token_publisher.token_path, "w").write(json.dumps(TOKEN))
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(RefreshError("invalid_grant: expired")))
    check = live_checks(monkeypatch, Insights(token_publisher))["token refresh"]
    assert check["ok"] is False and check["required"] is True
    assert "invalid_grant" in check["detail"] and "Testing" in check["detail"] and "7 days" in check["detail"]
    assert "youtube_oauth.py --force" in check["detail"]


def test_live_doctor_refreshes_a_token_that_still_looks_valid(monkeypatch, token_publisher):
    """A Testing token past day 7 keeps its last access token for up to an hour, which would hide that it is dead."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    from app.growth.youtube import Insights

    fresh = {**TOKEN, "expiry": "2099-01-01T00:00:00Z"}
    open(token_publisher.token_path, "w").write(json.dumps(fresh))
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(RefreshError("invalid_grant")))
    token_publisher.check_ready()  # an upload would still go ahead on what is left of the hour
    assert live_checks(monkeypatch, Insights(token_publisher))["token refresh"]["ok"] is False


def test_live_doctor_stops_at_a_dead_token(monkeypatch):
    from app.publishers.base import TokenProblem

    insights = LiveInsights(token=TokenProblem("token refresh failed"))
    live_checks(monkeypatch, insights)
    assert insights.calls == ["token"]


@pytest.mark.parametrize("error, expected", [
    (google_refusal(403, "accessNotConfigured", "YouTube Data API v3 has not been used in project 1"), "APIs & Services"),
    (google_refusal(403, "insufficientPermissions", "Request had insufficient authentication scopes."), "tick every box"),
    (google_refusal(403, "quotaExceeded", "The request cannot be completed because you have exceeded your quota."), "midnight Pacific"),
    (google_refusal(401, "authError", "Invalid Credentials"), "youtube_oauth.py --force"),
    (google_refusal(500, "backendError", "Backend Error"), "try again"),
    (OSError("no route to host"), "connection"),
])
def test_live_doctor_turns_a_failed_call_into_a_cause_and_a_next_step(monkeypatch, error, expected):
    check = live_checks(monkeypatch, LiveInsights(channel_error=error))["channel"]
    assert check["ok"] is False and check["required"] is True and expected in check["detail"]


def test_live_doctor_calls_a_lost_network_on_refresh_a_network_problem(monkeypatch, token_publisher):
    from google.auth.exceptions import TransportError
    from google.oauth2.credentials import Credentials

    from app.growth.youtube import Insights

    open(token_publisher.token_path, "w").write(json.dumps(TOKEN))
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(TransportError("no route")))
    check = live_checks(monkeypatch, Insights(token_publisher))["token refresh"]
    assert check["ok"] is False and "could not reach Google" in check["detail"] and "Testing" not in check["detail"]


def test_a_video_id_in_the_url_does_not_read_as_a_quota_problem():
    from app.publishers.youtube_publisher import explain_api_error

    error = google_refusal(400, "badRequest", "Bad request")
    error.uri = "https://example.com/?id=quota"
    assert "quota" not in explain_api_error(error)


def test_an_error_that_is_not_about_google_is_passed_on_as_it_is():
    from app.publishers.youtube_publisher import explain_api_error

    assert explain_api_error(ValueError("database is locked")) == "database is locked"


def test_the_live_reads_use_real_api_parameters(monkeypatch):
    """Checked against the discovery documents bundled with the client, so nothing goes over the network."""
    from googleapiclient.discovery import build

    from app.growth.youtube import Insights

    youtube = build("youtube", "v3", developerKey="x", static_discovery=True)
    analytics = build("youtubeAnalytics", "v2", developerKey="x", static_discovery=True)
    sent = []

    def record(request):
        sent.append(request.uri)
        return {"items": [{"snippet": {"title": "My Channel"}}]}

    monkeypatch.setattr("googleapiclient.http.HttpRequest.execute", record)

    class Publisher:
        def _client(self):
            return youtube

        def analytics_client(self):
            return analytics

    insights = Insights(Publisher())
    assert insights.channel_name() == "My Channel"
    insights.probe_analytics()
    assert "mine=true" in sent[0] and "metrics=views" in sent[1] and "ids=channel%3D%3DMINE" in sent[1]


def test_live_doctor_fails_on_a_refused_analytics_read(monkeypatch):
    error = google_refusal(403, "accessNotConfigured", "YouTube Analytics API has not been used in project 1")
    checks = live_checks(monkeypatch, LiveInsights(analytics_error=error))
    check = checks["analytics read"]
    assert check["ok"] is False and check["required"] is True
    assert "YouTube Analytics API" in check["detail"] and "APIs & Services" in check["detail"]
    assert checks["channel"]["ok"] is True


def test_live_doctor_is_not_ready_on_a_failed_call(monkeypatch):
    monkeypatch.setattr(report, "get_insights", lambda: LiveInsights(channel="", channel_error=None))
    result = report.doctor(live=True)
    channel = next(c for c in result["checks"] if c["name"] == "channel")
    assert channel["ok"] is False and result["ready"] is False and "no YouTube channel" in channel["detail"]


def test_live_doctor_says_when_there_is_no_real_connection(monkeypatch):
    check = live_checks(monkeypatch, None)["live YouTube checks"]
    assert check["ok"] is False and check["required"] is False and "USE_MOCK" in check["detail"]


def test_live_doctor_resolves_every_feed_to_a_link(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/good", "https://example.com/empty", "https://example.com/broken")

    def read(url, options):
        if "broken" in url:
            raise RuntimeError("HTTP Error 404")
        return {"entries": [{"url": "https://example.com/1"}] if "good" in url else []}

    fake_ydl(monkeypatch, read)
    checks = live_checks(monkeypatch, None)
    assert checks["feed https://example.com/good"]["ok"] is True
    empty, broken = checks["feed https://example.com/empty"], checks["feed https://example.com/broken"]
    assert empty["ok"] is False and empty["required"] is True and "lists no videos" in empty["detail"]
    assert broken["ok"] is False and broken["required"] is True and "404" in broken["detail"]


def test_live_doctor_explains_a_feed_with_nothing_usable_because_of_a_block(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/theirs")
    fake_ydl(monkeypatch, lambda url, options: {"channel_id": "UCbad", "entries": [{"url": "https://example.com/1"}]})
    monkeypatch.setattr(safety, "blocked_authors", lambda db: {"ucbad"})
    check = live_checks(monkeypatch, None)["feed https://example.com/theirs"]
    assert check["ok"] is False and check["required"] is False and "takedown block" in check["detail"]


@pytest.mark.parametrize("args, live", [(("doctor",), True), (("doctor", "--offline"), False)])
def test_the_doctor_command_is_live_unless_told_otherwise(monkeypatch, capsys, args, live):
    seen = []

    def fake_doctor(*, live=False):
        seen.append(live)
        return {"ready": True, "checks": []}

    monkeypatch.setattr(report, "doctor", fake_doctor)
    assert run_growth_script(monkeypatch, *args) == 0
    assert seen == [live]


def test_the_doctor_command_prints_live_results_for_real(monkeypatch, capsys):
    monkeypatch.setattr(report, "get_insights", lambda: LiveInsights(channel_error=OSError("no route")))
    assert run_growth_script(monkeypatch, "doctor") == 1
    out = capsys.readouterr().out
    assert "FAIL  channel: could not reach Google" in out and "ok    token refresh" in out
    assert run_growth_script(monkeypatch, "doctor", "--offline") == 1
    assert "token refresh" not in capsys.readouterr().out


@pytest.mark.parametrize("query, live", [("", False), ("?live=true", True)])
def test_the_doctor_endpoint_is_offline_unless_asked(monkeypatch, query, live):
    from fastapi.testclient import TestClient

    from app.main import app

    seen = []
    monkeypatch.setattr(report, "doctor", lambda *, live=False: seen.append(live) or {"ready": True, "checks": []})
    assert TestClient(app).get(f"/growth/doctor{query}").json()["ready"] is True
    assert seen == [live]


# -- #47: a switch that stops posting is a reason it cannot run
def test_doctor_requires_publishing_to_be_on(monkeypatch):
    assert checks_by_name()["ENABLE_PUBLISH"]["ok"] is True
    monkeypatch.setattr(settings, "enable_publish", False)
    check = checks_by_name()["ENABLE_PUBLISH"]
    assert check["ok"] is False and check["required"] is True


# -- collecting: failures reach the report
class BrokenStats(FakeInsights):
    def stats(self, ids):
        raise google_refusal(403, "insufficientPermissions", "Request had insufficient authentication scopes.")


def test_a_failed_stats_read_is_reported_once_a_day(tmp_path, monkeypatch):
    published_clip(tmp_path / "a", 1, 5, published_at=START)
    monkeypatch.setattr(collect, "get_insights", lambda: BrokenStats())
    now = START + timedelta(hours=10)
    assert "video stats" in collect.collect_job(now=now)["error"]
    assert "video stats" in collect.collect_job(now=now + timedelta(hours=3))["error"]
    found = events("collect_failed")
    assert len(found) == 1 and "tick every box" in found[0].summary


def test_a_failed_analytics_read_is_reported_once_a_day(tmp_path, monkeypatch):
    published_clip(tmp_path / "a", 1, 5, published_at=START)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": stat(9)}, analytics_error="403 forbidden"))
    now = START + timedelta(days=3)
    collect.collect_job(now=now)
    collect.collect_job(now=now + timedelta(hours=3))
    found = events("analytics_failed")
    assert len(found) == 1 and "403 forbidden" in found[0].summary


def test_the_report_shows_the_last_good_collect(tmp_path, monkeypatch):
    assert report.build_report()["collect"]["last_success"] is None
    published_clip(tmp_path / "a", 1, 5, published_at=START)
    good = START + timedelta(hours=10)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": stat(9)}))
    collect.collect_job(now=good)
    assert report.build_report()["collect"]["last_success"] == good.isoformat()
    monkeypatch.setattr(collect, "get_insights", lambda: BrokenStats())
    collect.collect_job(now=good + timedelta(hours=3))
    assert report.build_report()["collect"]["last_success"] == good.isoformat()


def test_collect_with_nothing_to_read_is_not_a_good_collect(monkeypatch):
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights())
    assert "skipped" in collect.collect_job(now=START)
    assert report.build_report()["collect"]["last_success"] is None


def test_analytics_failing_for_three_days_becomes_a_failure_and_a_warning(tmp_path, monkeypatch):
    first = datetime.now(timezone.utc) - timedelta(days=4)
    published_clip(tmp_path / "a", 1, 5, published_at=first - timedelta(days=3))
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": stat(9)}, analytics_error="403 forbidden"))
    assert "error" not in collect.collect_job(now=first)
    assert "error" not in collect.collect_job(now=first + timedelta(days=2, hours=23))
    out = collect.collect_job(now=first + timedelta(days=3))
    assert "3 days" in out["error"] and out["snapshots"] == 1
    assert any("3 days" in e.summary for e in events("analytics_failed"))
    assert any("Analytics" in warning and "views alone" in warning for warning in report.build_report()["warnings"])


def test_analytics_warning_waits_for_three_days():
    since = START.isoformat()
    status = {"analytics_failing_since": since, "analytics_error": "no"}
    assert collect.analytics_warning(status, START + timedelta(days=2, hours=23)) is None
    assert "3 days" in collect.analytics_warning(status, START + timedelta(days=3))
    assert collect.analytics_warning({"analytics_failing_since": None, "analytics_error": None}, START) is None


def test_analytics_recovering_clears_the_failure(tmp_path, monkeypatch):
    published_clip(tmp_path / "a", 1, 5, published_at=START)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": stat(9)}, analytics_error="403 forbidden"))
    first = START + timedelta(days=3)
    collect.collect_job(now=first)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": stat(9)}))
    assert "error" not in collect.collect_job(now=first + timedelta(days=1))
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": stat(9)}, analytics_error="403 forbidden"))
    assert "error" not in collect.collect_job(now=first + timedelta(days=4))  # the clock started again
    assert report.build_report()["collect"]["analytics_failing_since"] == (first + timedelta(days=4)).isoformat()


def test_the_report_command_shows_collect_health(tmp_path, monkeypatch, capsys):
    run_growth_script(monkeypatch, "report")
    assert "Last successful collect: never" in capsys.readouterr().out
    published_clip(tmp_path / "a", 1, 5, published_at=START)
    monkeypatch.setattr(collect, "get_insights", lambda: FakeInsights({"yt1": stat(9)}))
    collect.collect_job(now=START + timedelta(hours=10))
    run_growth_script(monkeypatch, "report")
    assert "Last successful collect: 2026-11-02 10:00" in capsys.readouterr().out


# -- #25: an empty shelf and a missed window say so
def test_a_window_missed_for_lack_of_clips_says_so(publisher):
    add_plan(waiting_plan(hours_ago=1))
    tick()
    tick(60 * 6)
    assert "no finished clips in stock" in events("missed")[0].summary


def test_a_window_nothing_tried_to_post_still_gets_a_reason(publisher):
    add_plan(waiting_plan(hours_ago=20, window_hours=4))
    tick()
    assert "did not get to it" in events("missed")[0].summary


def test_a_window_missed_on_a_hold_says_so(tmp_path, publisher):
    ready_file_clip(tmp_path)
    add_plan(waiting_plan(hours_ago=1))
    with session_scope() as db:
        safety.set_hold(db, START + timedelta(hours=9), "quota is out")
    tick()
    tick(60 * 6)
    assert "quota is out" in events("missed")[0].summary


def test_a_window_missed_at_the_daily_cap_says_so(tmp_path, publisher, monkeypatch):
    ready_file_clip(tmp_path)
    add_plan(waiting_plan(hours_ago=1))
    monkeypatch.setattr(quota, "remaining", lambda db: 0)
    tick()
    tick(60 * 6)
    assert "daily upload cap" in events("missed")[0].summary


def test_a_window_missed_on_the_minimum_gap_says_so(tmp_path, publisher, monkeypatch):
    ready_file_clip(tmp_path)
    add_plan(waiting_plan(hours_ago=1))
    monkeypatch.setattr(autopilot, "_gap_is_clear", lambda db, now: False)
    assert tick() == {"waiting": "minimum gap between uploads"}
    tick(60 * 6)
    assert "minimum gap" in events("missed")[0].summary


def test_a_starved_supply_is_recorded_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_inbox_dir", str(tmp_path))
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    assert "starved" in supply.supply_job()
    assert "starved" in supply.supply_job()
    found = events("starved")
    assert len(found) == 1 and "0 clips left" in found[0].summary
    with session_scope() as db:
        found_row = db.execute(select(GrowthEvent).where(GrowthEvent.kind == "starved")).scalar_one()
        found_row.created_at = datetime.now(timezone.utc) - timedelta(days=1, minutes=1)
    supply.supply_job()
    assert len(events("starved")) == 2


def test_a_stocked_supply_is_not_starved(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_min_inventory", 1)
    ready_file_clip(tmp_path)
    assert "starved" not in supply.supply_job()
    assert events("starved") == []


def test_the_report_warns_of_an_empty_shelf_and_how_to_fill_it(tmp_path):
    warnings = report.build_report()["warnings"]
    assert len(warnings) == 1 and "nothing can post" in warnings[0] and "GROWTH_FEEDS" in warnings[0]
    ready_file_clip(tmp_path)
    assert report.build_report()["warnings"] == []


def test_the_empty_shelf_warning_knows_clips_are_on_the_way():
    with session_scope() as db:
        db.add(ClipJob(source="x", status="running"))
    warnings = report.build_report()["warnings"]
    assert "1 clip jobs" in warnings[0] and "GROWTH_FEEDS" not in warnings[0]


def test_the_report_command_warns_of_an_empty_shelf(monkeypatch, capsys):
    run_growth_script(monkeypatch, "report")
    assert "WARNING: No finished clips in stock" in capsys.readouterr().out


# -- #51: problems stay on the page
def problem_at(kind, days_ago, summary="went wrong"):
    with session_scope() as db:
        db.add(GrowthEvent(kind=kind, summary=summary, created_at=datetime.now(timezone.utc) - timedelta(days=days_ago)))


@pytest.mark.parametrize("kind", ["missing", "strike", "paused", "account_hold", "publish_failed", "publish_stuck",
                                  "collect_failed", "analytics_failed", "finding_retracted", "starved", "missed",
                                  "transcribe_failed", "feed_empty", "clip_held"])
def test_a_problem_outlasts_a_month_of_routine_events_but_not_the_month(kind):
    problem_at(kind, 29, "recent")
    problem_at(kind, 31, "too old")
    for _ in range(60):
        problem_at("posted", 0)
    built = report.build_report()
    assert [(p["kind"], p["summary"]) for p in built["problems"]] == [(kind, "recent")]
    assert len(built["events"]) == 40 and {e["kind"] for e in built["events"]} == {"posted"}


def test_routine_events_are_not_problems():
    for kind in ("plan", "posted", "diagnosis:winner", "finding"):
        problem_at(kind, 1)
    assert report.build_report()["problems"] == []


def test_problems_come_newest_first():
    problem_at("missed", 3, "older")
    problem_at("strike", 1, "newer")
    assert [p["summary"] for p in report.build_report()["problems"]] == ["newer", "older"]


def test_the_report_command_keeps_a_rare_problem_visible_through_a_long_outage(monkeypatch, capsys):
    problem_at("strike", 20, "yt5 has a problem: copyright")
    for day in range(10):
        problem_at("missed", day, f"Missed window on day {day}")
    for _ in range(20):
        problem_at("posted", 0, "routine")
    run_growth_script(monkeypatch, "report")
    out = capsys.readouterr().out
    problems = out[out.index("Problems in the last 30 days"):out.index("Recent")]
    assert "yt5 has a problem: copyright" in problems and "Missed window on day 0" in problems
    assert "Missed window on day 9" not in problems and "and 5 more missed" in problems
    assert "routine" not in problems


def test_the_report_command_says_nothing_of_problems_when_there_are_none(monkeypatch, capsys):
    run_growth_script(monkeypatch, "report")
    assert "Problems" not in capsys.readouterr().out


# ---------- the OAuth script ----------
class FakeCreds:
    def __init__(self, scopes, refresh_token="r"):
        self.granted_scopes, self.scopes, self.refresh_token = scopes, scopes, refresh_token

    def to_json(self):
        return json.dumps({"token": "t", "refresh_token": self.refresh_token, "client_secret": "s", "scopes": self.scopes})


def oauth_scopes(*missing):
    from app.publishers.youtube_publisher import SCOPES

    return [s for s in SCOPES if s not in missing]


@pytest.fixture
def run_oauth(tmp_path, monkeypatch):
    """Runs the real script with Google's login and API replaced. Returns (exit code, printed text)."""
    import runpy
    import sys
    from pathlib import Path

    from google_auth_oauthlib.flow import InstalledAppFlow

    script = Path(__file__).resolve().parent.parent / "scripts" / "youtube_oauth.py"
    (tmp_path / "client.json").write_text("{}")
    monkeypatch.setenv("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    token = tmp_path / "token.json"

    def run(*args, scopes=None, build=None, refresh_token="r"):
        class Flow:
            def run_local_server(self, **kwargs):
                return FakeCreds(oauth_scopes() if scopes is None else scopes, refresh_token)

        monkeypatch.setattr(InstalledAppFlow, "from_client_secrets_file", lambda *a, **k: Flow())
        monkeypatch.setattr("googleapiclient.discovery.build", build or (lambda *a, **k: FakeYouTube()))
        monkeypatch.setattr(sys, "argv", [str(script), "--secrets", str(tmp_path / "client.json"), "--token", str(token), *args])
        with pytest.raises(SystemExit) as stopped:
            runpy.run_path(str(script), run_name="__main__")
        return stopped.value.code

    run.token = token
    return run


class FakeYouTube:
    def __init__(self, error=None, items=None):
        self.error = error
        self.items = [{"snippet": {"title": "My Channel"}}] if items is None else items

    def channels(self):
        return self

    def list(self, **kwargs):
        return self

    def execute(self):
        if self.error:
            raise self.error
        return {"items": self.items}


ENV_BLOCK = ("USE_MOCK=false", "YOUTUBE_CLIENT_SECRETS=", "YOUTUBE_TOKEN_PATH=")


def test_oauth_warns_when_read_permission_was_unticked(run_oauth, capsys):
    from app.publishers.youtube_publisher import READ_SCOPE

    run_oauth(scopes=oauth_scopes(READ_SCOPE), build=lambda *a, **k: FakeYouTube(
        google_refusal(403, "insufficientPermissions", "Request had insufficient authentication scopes.")))
    out = capsys.readouterr().out
    assert "WARNING: read permission" in out


def test_oauth_prints_the_env_block_before_calling_the_api(run_oauth, capsys):
    printed_first = []

    def build(*args, **kwargs):
        printed_first.append(capsys.readouterr().out)
        return FakeYouTube()

    assert run_oauth(build=build) == 0
    assert all(line in printed_first[0] for line in ENV_BLOCK)
    assert "Connected channel: My Channel" in capsys.readouterr().out


@pytest.mark.parametrize("error, expected", [
    (google_refusal(403, "insufficientPermissions", "Request had insufficient authentication scopes."), "tick every box"),
    (google_refusal(403, "accessNotConfigured", "YouTube Data API v3 has not been used in project 1"), "APIs & Services"),
])
def test_oauth_explains_a_refused_channel_read_instead_of_a_traceback(run_oauth, capsys, error, expected):
    assert run_oauth(build=lambda *a, **k: FakeYouTube(error)) == 1
    out = capsys.readouterr().out
    assert expected in out and all(line in out for line in ENV_BLOCK)
    assert run_oauth.token.exists()


def test_oauth_reprints_the_env_block_when_a_token_exists(run_oauth, capsys):
    run_oauth.token.write_text(json.dumps({"refresh_token": "r", "scopes": oauth_scopes()}))
    assert run_oauth(build=lambda *a, **k: pytest.fail("a re-run must not call Google")) == 0
    out = capsys.readouterr().out
    assert "already exists" in out and all(line in out for line in ENV_BLOCK) and "WARNING" not in out


def test_oauth_rerun_says_what_the_existing_token_lacks(run_oauth, capsys):
    from app.publishers.youtube_publisher import READ_SCOPE

    run_oauth.token.write_text(json.dumps({"refresh_token": "r", "scopes": oauth_scopes(READ_SCOPE)}))
    run_oauth()
    assert "WARNING: read permission" in capsys.readouterr().out


def test_oauth_rerun_survives_an_unreadable_token(run_oauth, capsys):
    run_oauth.token.write_text('{"cut off')
    assert run_oauth() == 0
    out = capsys.readouterr().out
    assert "cannot be read" in out and "--force" in out and all(line in out for line in ENV_BLOCK)


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_oauth_tightens_a_loose_existing_token(run_oauth):
    import stat as stat_module

    run_oauth.token.write_text("{}")
    os.chmod(run_oauth.token, 0o644)
    assert run_oauth("--force") == 0
    assert stat_module.S_IMODE(os.stat(run_oauth.token).st_mode) == 0o600
    assert json.loads(run_oauth.token.read_text())["refresh_token"] == "r"
    assert [p.name for p in run_oauth.token.parent.iterdir() if p.suffix == ".tmp"] == []


# -- operations review: what the report says when something has stopped
@pytest.mark.parametrize("kind", ["transcribe_failed", "feed_empty", "clip_held"])
def test_a_stopped_job_or_a_dead_feed_outlasts_the_routine_events(kind):
    problem_at(kind, 6, "needs the owner")
    for _ in range(60):
        problem_at("posted", 0)
    built = report.build_report()
    assert [p["kind"] for p in built["problems"]] == [kind]
    assert kind not in {e["kind"] for e in built["events"]}


@pytest.mark.parametrize("kind", ["strike_cleared", "resumed", "unblocked"])
def test_what_ended_a_problem_stays_beside_it(kind):
    problem_at("strike", 8, "yt5 has a problem. Lift with: scripts/growth.py unblock <name>")
    problem_at(kind, 7, "over now")
    for _ in range(60):
        problem_at("posted", 0)
    assert [p["kind"] for p in report.build_report()["problems"]] == [kind, "strike"]


def test_a_cleared_strike_can_be_seen_from_the_report_command(monkeypatch, capsys):
    problem_at("strike", 8, "yt5 has a problem")
    problem_at("strike_cleared", 7, "yt5 is back on YouTube, so its strike and block are gone.")
    for _ in range(20):
        problem_at("posted", 0, "routine")
    run_growth_script(monkeypatch, "report")
    out = capsys.readouterr().out
    assert "yt5 is back on YouTube" in out[out.index("Problems in the last 30 days"):out.index("Recent")]


def test_the_empty_shelf_warning_names_clips_held_for_review(tmp_path):
    for n in range(3):
        ready_file_clip(tmp_path, n, status=VideoStatus.PENDING, growth_flagged="no AI-written title")
    warning = report.build_report()["warnings"][0]
    assert "nothing can post" in warning and "3 clips" in warning and "approves" in warning
    assert "no AI-written title" in warning and "GROWTH_FEEDS" not in warning


def test_the_held_warning_shows_in_the_report_command(monkeypatch, capsys, tmp_path):
    ready_file_clip(tmp_path, status=VideoStatus.PENDING, growth_flagged="no AI-written title")
    run_growth_script(monkeypatch, "report")
    assert "1 clips will not post until someone approves" in capsys.readouterr().out


def test_the_empty_shelf_warning_keeps_clip_jobs_and_held_clips_apart(tmp_path):
    ready_file_clip(tmp_path, status=VideoStatus.PENDING, growth_flagged="no AI-written title")
    with session_scope() as db:
        db.add(ClipJob(source="x", status="running"))
    warning = report.build_report()["warnings"][0]
    assert "1 clip jobs" in warning and "1 clips will not post" in warning


def test_an_empty_shelf_is_no_warning_while_the_loop_is_off(monkeypatch):
    monkeypatch.setattr(settings, "growth_enabled", False)
    assert report.build_report()["warnings"] == []


def live_feed(monkeypatch, read):
    fake_ydl(monkeypatch, read)
    return live_checks(monkeypatch, None)


def test_a_feed_that_lists_videos_again_stops_counting_as_empty(monkeypatch, tmp_path):
    feed = "https://example.com/feed"
    use_feeds(monkeypatch, tmp_path, feed)
    fake_ydl(monkeypatch, lambda url, options: {"entries": []})
    assert supply.next_source() is None
    assert content_source()["ok"] is False
    checks = live_feed(monkeypatch, lambda url, options: {"entries": [{"url": "https://example.com/1"}]})
    assert checks[f"feed {feed}"]["ok"] is True
    assert checks["content source"]["ok"] is True and "no videos found" not in checks["content source"]["detail"]
    assert events("feed_empty") == []


def test_a_feed_that_goes_empty_again_is_recorded_again(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    fake_ydl(monkeypatch, lambda url, options: {"entries": []})
    supply.next_source()
    fake_ydl(monkeypatch, lambda url, options: {"entries": [{"url": "https://example.com/1"}]})
    supply.next_source()
    fake_ydl(monkeypatch, lambda url, options: {"entries": []})
    supply.next_source()
    assert len(events("feed_empty")) == 1 and content_source()["ok"] is False


def test_one_feed_recovering_leaves_another_feeds_empty_event(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/dead", "https://example.com/good")
    fake_ydl(monkeypatch, lambda url, options: {"entries": [] if "dead" in url else [{"url": "https://example.com/1"}]})
    supply.first_link("https://example.com/dead")
    supply.first_link("https://example.com/good")
    assert ["https://example.com/dead" in e.summary for e in events("feed_empty")] == [True]


def blocked_feed_reader(url, options):
    return {"channel_id": "UCbad" if "theirs" in url else "UCgood", "entries": [{"url": f"{url}/1"}]}


def test_a_blocked_feed_warns_without_stopping_a_loop_that_has_another_feed(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/theirs", "https://example.com/mine")
    monkeypatch.setattr(safety, "blocked_authors", lambda db: {"ucbad"})
    fake_ydl(monkeypatch, blocked_feed_reader)
    monkeypatch.setattr(report, "get_insights", lambda: None)
    checks = {c["name"]: c for c in report.doctor(live=True)["checks"]}
    blocked = checks["feed https://example.com/theirs"]
    assert blocked["ok"] is False and blocked["required"] is False and "unblock" in blocked["detail"]
    assert checks["feed https://example.com/mine"]["ok"] is True
    source = checks["content source"]
    assert source["ok"] is True and "1 feeds" in source["detail"] and "takedown block" in source["detail"]


def test_a_feed_whose_every_video_is_barred_warns_too(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/mixed")
    monkeypatch.setattr(safety, "blocked_authors", lambda db: {"ucbad"})
    fake_ydl(monkeypatch, lambda url, options: {"entries": [{"url": "https://example.com/1", "channel_id": "UCbad"}]})
    check = live_checks(monkeypatch, None)["feed https://example.com/mixed"]
    assert check["ok"] is False and check["required"] is False


def test_a_block_as_the_only_source_is_still_not_ready(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/theirs")
    monkeypatch.setattr(safety, "blocked_authors", lambda db: {"ucbad"})
    fake_ydl(monkeypatch, blocked_feed_reader)
    checks = live_checks(monkeypatch, None)
    assert checks["feed https://example.com/theirs"]["required"] is False
    assert checks["content source"]["ok"] is False and checks["content source"]["required"] is True


def test_a_block_replaces_an_old_empty_event_so_the_feed_is_not_a_failure(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/theirs")
    fake_ydl(monkeypatch, lambda url, options: {"entries": []})
    supply.next_source()
    monkeypatch.setattr(safety, "blocked_authors", lambda db: {"ucbad"})
    assert live_feed(monkeypatch, blocked_feed_reader)["feed https://example.com/theirs"]["required"] is False


def starved_warning():
    return report.build_report()["warnings"][0]


def test_a_feed_that_cannot_be_read_is_named_as_the_reason_supply_starved(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    fake_ydl(monkeypatch, lambda url, options: (_ for _ in ()).throw(RuntimeError("HTTP Error 429")))
    starved = supply.supply_job()["starved"]
    assert "https://example.com/feed" in starved and "429" in starved and "no new source" not in starved
    assert "429" in events("starved")[0].summary
    warning = starved_warning()
    assert "could not read https://example.com/feed" in warning and "429" in warning
    assert "connection" in warning and "yt-dlp" in warning and "GROWTH_FEEDS" not in warning


def test_failing_clip_jobs_are_named_as_the_reason_supply_starved(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    fake_ydl(monkeypatch, lambda url, options: {"entries": [{"url": "https://example.com/1"}]})
    with session_scope() as db:
        db.add(ClipJob(source="https://example.com/1", status="failed", error="transcription failed: Deepgram said 401",
                       finished_at=datetime.now(timezone.utc)))
    starved = supply.supply_job()["starved"]
    assert "Deepgram said 401" in starved and "no new source" not in starved
    warning = starved_warning()
    assert "Deepgram said 401" in warning and "Clip tab" in warning and "GROWTH_FEEDS" not in warning


def test_only_failures_since_the_last_good_job_count(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path)
    add_job("https://example.com/old", "failed", age_hours=9)
    add_job("https://example.com/good", "done", age_hours=5)
    starved = supply.supply_job()["starved"]
    assert "no new source" in starved and "failed" not in starved
    assert "GROWTH_FEEDS" in starved_warning()


def test_a_used_up_supply_still_says_to_add_sources(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    fake_ydl(monkeypatch, lambda url, options: {"entries": [{"url": "https://example.com/1"}]})
    add_job("https://example.com/1", "done")
    assert "no new source" in supply.supply_job()["starved"]
    assert "GROWTH_FEEDS" in starved_warning()


def test_a_new_cause_of_starving_is_recorded_the_same_day(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    fake_ydl(monkeypatch, lambda url, options: {"entries": [{"url": "https://example.com/1"}]})
    add_job("https://example.com/1", "done")
    supply.supply_job()
    fake_ydl(monkeypatch, lambda url, options: (_ for _ in ()).throw(RuntimeError("HTTP Error 429")))
    supply.supply_job()
    supply.supply_job()
    assert [("429" in e.summary) for e in events("starved")] == [False, True]


def test_the_reason_supply_starved_goes_once_it_finds_a_source(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    fake_ydl(monkeypatch, lambda url, options: (_ for _ in ()).throw(RuntimeError("HTTP Error 429")))
    supply.supply_job()
    fake_ydl(monkeypatch, lambda url, options: {"entries": [{"url": "https://example.com/1"}]})
    assert supply.supply_job()["queued"] == "https://example.com/1"
    with session_scope() as db:
        db.execute(delete(ClipJob))
    assert "429" not in starved_warning() and "GROWTH_FEEDS" in starved_warning()


def test_a_long_error_does_not_flood_the_report(monkeypatch, tmp_path):
    use_feeds(monkeypatch, tmp_path, "https://example.com/feed")
    fake_ydl(monkeypatch, lambda url, options: (_ for _ in ()).throw(RuntimeError("line one\n" + "x" * 5000)))
    starved = supply.supply_job()["starved"]
    assert len(starved) < 400 and "\n" not in starved


@pytest.mark.parametrize("error", [
    OSError(28, "No space left on device"),
    PermissionError(13, "Permission denied"),
    FileNotFoundError(2, "No such file or directory"),
])
def test_a_local_disk_failure_is_not_blamed_on_the_network(error):
    from app.publishers.youtube_publisher import explain_api_error

    text = explain_api_error(error)
    assert "could not reach Google" not in text and "this machine" in text and error.strerror in text


@pytest.mark.parametrize("error", [
    OSError(113, "No route to host"), ConnectionResetError(104, "reset"), TimeoutError("timed out"), OSError("network down"),
])
def test_a_socket_failure_is_still_a_network_problem(error):
    from app.publishers.youtube_publisher import explain_api_error

    assert "could not reach Google" in explain_api_error(error)


def test_live_doctor_does_not_blame_google_when_saving_the_token_fails(monkeypatch, token_publisher):
    from google.oauth2.credentials import Credentials

    from app.growth.youtube import Insights
    from app.publishers import youtube_publisher

    open(token_publisher.token_path, "w").write(json.dumps(TOKEN))
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: None)

    def full_disk(path, contents):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(youtube_publisher, "write_token_file", full_disk)
    check = live_checks(monkeypatch, Insights(token_publisher))["token refresh"]
    assert check["ok"] is False and "could not reach Google" not in check["detail"] and "No space" in check["detail"]


# ---------- owner approval and the review page ----------
def test_in_approval_mode_supply_cuts_into_review_not_onto_the_shelf(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "growth_require_approval", True)
    monkeypatch.setattr(supply, "next_source", lambda unread=None: ("https://example.com/v", "https://example.com/feed"))
    supply.supply_job()
    with session_scope() as db:
        job = db.execute(select(ClipJob)).scalars().all()[-1]
        assert job.auto_queue is False


def test_clips_awaiting_review_count_as_stock(tmp_path, monkeypatch):
    from app.growth import approval

    monkeypatch.setattr(settings, "growth_require_approval", True)
    monkeypatch.setattr(settings, "growth_min_inventory", 1)
    path = tmp_path / "c.mp4"
    path.write_bytes(b"x")
    make_clip(path, 1, status=VideoStatus.PENDING)
    with session_scope() as db:
        assert len(approval.awaiting_review(db)) == 1
    assert "queued" not in supply.supply_job()


def test_approving_puts_a_clip_on_the_shelf_with_the_owners_title(tmp_path):
    from app.growth import approval

    path = tmp_path / "c.mp4"
    path.write_bytes(b"x")
    video_id = make_clip(path, 1, status=VideoStatus.PENDING)
    approval.approve(video_id, "  My  own title ")
    with session_scope() as db:
        video = db.get(Video, video_id)
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == video_id)).scalar_one()
        assert video.status == VideoStatus.TRANSFORMED and video.curated_at is not None
        assert draft.edited and draft.title.startswith("My own title")
        assert autopilot.ready_clips(db)[0].id == video_id


def test_a_clip_can_be_decided_only_once(tmp_path):
    from app.growth import approval

    path = tmp_path / "c.mp4"
    path.write_bytes(b"x")
    video_id = make_clip(path, 1, status=VideoStatus.PENDING)
    approval.reject(video_id)
    with pytest.raises(approval.ReviewError):
        approval.approve(video_id)


def test_review_page_is_shut_without_a_token_and_locked_without_the_key(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.review import review_app

    client = TestClient(review_app)
    monkeypatch.setattr(settings, "review_token", "")
    assert client.get("/api/queue").status_code == 503
    monkeypatch.setattr(settings, "review_token", "s3cret-token")
    assert client.get("/api/queue").status_code == 401
    assert client.get("/?key=wrong", follow_redirects=False).status_code == 401
    assert client.post("/api/clips/x/approve", json={}).status_code == 401


def test_review_page_signs_in_once_and_approves(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.review import review_app

    monkeypatch.setattr(settings, "review_token", "s3cret-token")
    path = tmp_path / "c.mp4"
    path.write_bytes(b"0123456789" * 100)
    video_id = make_clip(path, 1, status=VideoStatus.PENDING)
    client = TestClient(review_app)
    assert client.get("/?key=s3cret-token", follow_redirects=False).status_code == 303
    queue = client.get("/api/queue").json()
    assert [c["id"] for c in queue["clips"]] == [video_id]
    part = client.get(f"/media/{video_id}", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == b"0123456789"  # phones seek through video with ranges
    assert client.post(f"/api/clips/{video_id}/approve", json={"title": ""}).json()["status"] == "transformed"
    assert client.get("/api/queue").json()["clips"] == []


def test_review_page_shows_real_posts_with_their_results(tmp_path):
    from app.growth import approval

    published_clip(tmp_path / "a.mp4", 1, 500, retention=0.62)
    mock = published_clip(tmp_path / "b.mp4", 2, 5)
    with session_scope() as db:
        db.get(Video, mock).youtube_video_id = "MOCK_x"
        real = db.execute(select(Video).where(Video.youtube_video_id == "yt1")).scalar_one()
        real.meta = {**real.meta, "growth_diagnosis": {"code": "winner", "reason": "Well above normal."}}
    posted = approval.queue()["posted"]
    assert [p["url"] for p in posted] == ["https://youtube.com/shorts/yt1"]
    assert posted[0]["watched"] == 62 and posted[0]["verdict"] == "winner"


# ---------- phone alerts ----------
def test_alerts_stay_off_until_a_channel_is_set(monkeypatch):
    from app.growth import notify

    monkeypatch.setattr(settings, "ntfy_topic", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    assert "skipped" in notify.notify_job()


def test_alert_digest_names_new_clips_posts_and_problems(tmp_path, monkeypatch):
    from app.growth import notify

    since = START - timedelta(minutes=1)
    path = tmp_path / "c.mp4"
    path.write_bytes(b"x")
    make_clip(path, 1, status=VideoStatus.PENDING)
    posted = published_clip(tmp_path / "p.mp4", 2, 10)
    with session_scope() as db:
        for video in db.execute(select(Video)).scalars():
            video.scraped_at = START
        db.add(GrowthEvent(kind="posted", video_id=posted, created_at=START))
        db.add(GrowthEvent(kind="paused", summary="Posting paused after a copyright claim", created_at=START))
        db.add(GrowthEvent(kind="plan", summary="routine", created_at=START))
    with session_scope() as db:  # saved first, as the jobs that write events have
        lines = notify.digest(db, since)
    assert lines[0].startswith("1 new clip waiting")
    assert any(l.startswith("Posted:") and "youtube.com/shorts/yt2" in l for l in lines), lines
    assert any("copyright claim" in l for l in lines) and not any("routine" in l for l in lines)


def test_alerts_send_once_and_retry_when_delivery_fails(tmp_path, monkeypatch):
    from app.growth import notify

    monkeypatch.setattr(settings, "ntfy_topic", "test-topic")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append(text) or True)
    assert notify.notify_job(now=START)["sent"] == 0  # the first run only sets the clock
    path = tmp_path / "c.mp4"
    path.write_bytes(b"x")
    video_id = make_clip(path, 1, status=VideoStatus.PENDING)
    with session_scope() as db:
        db.get(Video, video_id).scraped_at = START + timedelta(minutes=5)
    monkeypatch.setattr(notify, "send", lambda text, **k: False)
    assert "error" in notify.notify_job(now=START + timedelta(minutes=10))
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append(text) or True)
    assert notify.notify_job(now=START + timedelta(minutes=20))["sent"] == 1
    assert notify.notify_job(now=START + timedelta(minutes=30))["sent"] == 0


def test_telegram_failures_never_log_the_bot_token(monkeypatch, caplog):
    from app.growth import notify
    import httpx

    monkeypatch.setattr(settings, "ntfy_topic", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "123:SECRET")
    monkeypatch.setattr(settings, "telegram_chat_id", "42")

    def boom(url, **kwargs):
        raise httpx.ConnectError(f"failed to reach {url}")

    monkeypatch.setattr(notify.httpx, "post", boom)
    with caplog.at_level("WARNING"):
        assert notify.send("hi") is False
    assert "SECRET" not in caplog.text
