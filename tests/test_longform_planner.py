"""Unit tests for app.longform.planner (energy curve, cold open, dedup, Part N, flat listing, end-to-end mocked)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import delete

from app.clipper import ingest, moments, transcribe
from app.clipper.media import MediaInfo
from app.clipper.signals import Signals
from app.config import settings
from app.db import init_db, session_scope
from app.longform import planner
from app.models import Channel, LongJob, Video, VideoStatus


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    init_db()
    with session_scope() as db:
        for tbl in (LongJob, Video, Channel):
            db.execute(delete(tbl))
    monkeypatch.setattr(settings, "clip_ai_timeout_sec", 5)


def _seg(sid: str, creator: str, start: float, end: float, score: float) -> planner.SegmentPlan:
    return planner.SegmentPlan(
        source_path=f"/tmp/{sid}.mp4",
        source_url=f"https://youtube.com/watch?v={sid}",
        source_id=sid,
        creator=creator,
        creator_name=creator,
        video_title=f"Vid {sid}",
        start=start,
        end=end,
        score=score,
        moment_title=f"Moment {sid}",
        language="en",
    )


def test_order_energy_curve_opens_with_second_and_closes_on_climax():
    segs = [
        _seg("s1", "@A", 0, 30, 50.0),
        _seg("s2", "@A", 35, 65, 95.0),
        _seg("s3", "@A", 70, 100, 82.0),
        _seg("s4", "@A", 105, 135, 60.0),
    ]
    ordered = planner.order_energy_curve(segs, format_kind="best_of")
    assert ordered[0].source_id == "s3"   # #2 opens
    assert ordered[-1].source_id == "s2"  # #1 closes

    vs_segs = [
        _seg("a1", "@Alpha", 0, 30, 99.0),
        _seg("a2", "@Alpha", 30, 60, 70.0),
        _seg("b1", "@Beta", 0, 30, 88.0),
        _seg("b2", "@Beta", 30, 60, 65.0),
    ]
    vs_ordered = planner.order_energy_curve(vs_segs, format_kind="vs")
    assert vs_ordered[0].creator == "@Beta"
    assert vs_ordered[-1].source_id == "a1"


def test_pick_cold_open_returns_short_teaser_before_climax_payoff():
    climax = _seg("s2", "@A", 10.0, 50.0, 95.0)
    co = planner.pick_cold_open([climax])
    assert 4.0 <= co.duration <= 6.0
    assert co.start >= climax.start and co.end <= climax.end


def test_used_spans_and_next_part_number_deduplicate_across_parts():
    with session_scope() as db:
        ch = Channel(slug="ch1", name="Ch1")
        db.add(ch)
        db.flush()
        db.add(Video(
            source="longform",
            source_id="lf1",
            source_url="https://youtube.com/watch?v=src1",
            title="Best of @Creator Part 1",
            status=VideoStatus.TRANSFORMED,
            channel_id=ch.id,
            meta={
                "long_form": True,
                "long_format": "best_of",
                "long_targets_key": "@creator",
                "part_number": 1,
                "long_segments": [{"source_id": "src1", "start": 10.0, "end": 45.0}],
            },
        ))
        # Failed rows and MOCK_ rehearsals must not increment Part N or consume source spans.
        db.add(Video(
            source="longform",
            source_id="lf_fail",
            source_url="https://youtu.be/fail",
            title="Failed",
            status=VideoStatus.FAILED,
            channel_id=ch.id,
            meta={
                "long_form": True,
                "long_format": "best_of",
                "long_targets_key": "@creator",
                "part_number": 9,
                "long_segments": [{"source_id": "src1", "start": 50.0, "end": 80.0}],
            },
        ))
        db.flush()
        spans = planner.used_spans_for_channel(db, ch.id)
        assert len(spans["src1"]) == 1
        assert spans["src1"][0].start == 10.0
        assert planner.next_part_number(db, ch.id, ["@Creator"]) == 2


def test_list_creator_videos_filters_short_live_and_blocked(monkeypatch):
    seen_kwargs = {}

    def fake_run(cmd, **kwargs):
        seen_kwargs.update(kwargs)
        payload = {
            "entries": [
                {"id": "short1", "duration": 90, "title": "Short"},
                {"id": "live1", "duration": 600, "is_live": True, "title": "Live"},
                {"id": "good1", "duration": 420, "title": "Full Vlog", "url": "good1"},
            ]
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(planner.subprocess, "run", fake_run)
    rows = planner.list_creator_videos("https://www.youtube.com/@SomeVlog")
    assert seen_kwargs["stdin"] is subprocess.DEVNULL
    assert len(rows) == 1
    assert rows[0]["id"] == "good1"
    assert rows[0]["creator"] == "@SomeVlog"
    assert rows[0]["url"] == "https://www.youtube.com/watch?v=good1"


def test_plan_compilation_best_of_vs_and_theme_without_shorts_tag(tmp_path, monkeypatch):
    src_a = tmp_path / "srcA.mp4"
    src_b = tmp_path / "srcB.mp4"
    src_a.write_bytes(b"A")
    src_b.write_bytes(b"B")

    monkeypatch.setattr(
        planner,
        "list_creator_videos",
        lambda url, limit=30: [
            {"id": "vA" if "Alpha" in url else "vB", "url": f"https://youtube.com/watch?v={'vA' if 'Alpha' in url else 'vB'}", "creator": planner.creator_handle(url), "duration": 600}
        ],
    )
    monkeypatch.setattr(
        planner.ingest,
        "fetch",
        lambda url: ingest.SourceVideo(
            path=str(src_a if "vA" in url else src_b),
            title="Source Title",
            url=url,
            source="youtube",
            source_id="vA" if "vA" in url else "vB",
            author="Alpha" if "vA" in url else "Beta",
        ),
    )
    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(600.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, i: Signals(600.0, np.zeros(600), np.zeros(600), np.zeros(600), np.ones(600), np.ones(600), []))
    words = [transcribe.Word(f"word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(400)]
    monkeypatch.setattr(planner.transcribe, "transcribe", lambda p, i: transcribe.Transcript(words=words, language="en", duration=600.0))
    monkeypatch.setattr(
        planner.moments,
        "pick_speech",
        lambda *a, **k: [
            moments.Moment(10.0, 75.0, title="Huge Stunt", score=92.0, kind="speech"),
            moments.Moment(90.0, 155.0, title="Plot Twist", score=80.0, kind="speech"),
            moments.Moment(170.0, 235.0, title="Crazy Finish", score=96.0, kind="speech"),
        ],
    )
    monkeypatch.setattr(planner.ai, "available", lambda: False)

    plan = planner.plan_compilation(
        format_kind="vs",
        channel_slug="default",
        targets=["@Alpha", "@Beta"],
        target_minutes=6,
        min_total_sec=100.0,
    )
    assert plan.format_kind == "vs"
    assert "#Shorts" not in plan.title and "#shorts" not in plan.title.lower()
    assert "#Shorts" not in plan.description and "shorts" not in plan.tags
    assert len(plan.segments) >= 2
    assert 4.0 <= plan.cold_open.duration <= 6.0
