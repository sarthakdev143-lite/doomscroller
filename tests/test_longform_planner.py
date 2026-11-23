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
        # Failed, Rejected, and MOCK_ rehearsals must not increment Part N or consume source spans.
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
        db.add(Video(
            source="longform",
            source_id="lf_rejected",
            source_url="https://youtu.be/rejected",
            title="Rejected",
            status=VideoStatus.REJECTED,
            channel_id=ch.id,
            meta={
                "long_form": True,
                "long_format": "best_of",
                "long_targets_key": "@creator",
                "part_number": 7,
                "long_segments": [{"source_id": "src1", "start": 90.0, "end": 130.0}],
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


def test_plan_compilation_prioritizes_cached_videos_and_transcripts_and_balances_vs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    long_dir = tmp_path / "content" / "long"
    long_dir.mkdir(parents=True)

    cached_a = long_dir / "Youtube_a2.mp4"
    cached_a.write_bytes(b"A" * 60_000)
    Path(f"{cached_a}.transcript.json").write_text('{"words": []}', encoding="utf-8")

    cached_b = long_dir / "Youtube_b2.mp4"
    cached_b.write_bytes(b"B" * 60_000)
    Path(f"{cached_b}.transcript.json").write_text('{"words": []}', encoding="utf-8")

    entries = [
        {"id": "a1", "url": "https://youtube.com/watch?v=a1", "creator": "@Alpha", "duration": 600},
        {"id": "b1", "url": "https://youtube.com/watch?v=b1", "creator": "@Beta", "duration": 600},
        {"id": "a2", "url": "https://youtube.com/watch?v=a2", "creator": "@Alpha", "duration": 600},
        {"id": "b2", "url": "https://youtube.com/watch?v=b2", "creator": "@Beta", "duration": 600},
    ]
    prioritized = planner._prioritize_cached_entries(entries, {}, format_kind="vs")
    assert [e["id"] for e in prioritized] == ["a2", "b2", "a1", "b1"]

    fetched: list[str] = []
    monkeypatch.setattr(
        planner,
        "_pick_source_candidates",
        lambda **k: (["@Alpha", "@Beta"], entries),
    )
    monkeypatch.setattr(planner.ingest, "fetch", lambda u: fetched.append(u) or (_ for _ in ()).throw(AssertionError(f"should not download {u}")))
    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(600.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, i: Signals(600.0, np.zeros(600), np.zeros(600), np.zeros(600), np.ones(600), np.ones(600), []))
    words = [transcribe.Word(f"word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(400)]
    monkeypatch.setattr(planner.transcribe, "transcribe", lambda p, i: transcribe.Transcript(words=words, language="en", duration=600.0))
    monkeypatch.setattr(
        planner.moments,
        "pick_speech",
        lambda *a, **k: [
            moments.Moment(10.0, 75.0, title="M1", score=90.0, kind="speech"),
            moments.Moment(90.0, 155.0, title="M2", score=88.0, kind="speech"),
            moments.Moment(170.0, 235.0, title="M3", score=86.0, kind="speech"),
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
    assert fetched == []
    assert len(plan.segments) >= 2


def test_planner_bans_vlog_intros_filters_unsafe_topics_and_enforces_chronology(tmp_path, monkeypatch):
    # 1. _moments_from_source bans 0..45s vlog intros and final 25s outros on >=240s videos and drops unsafe moments.
    vid = tmp_path / "vlog.mp4"
    vid.write_bytes(b"MP4")
    src = ingest.SourceVideo(path=str(vid), title="Vlog", url="https://youtu.be/v1", source="youtube", source_id="v1", author="@souravjvlogs")
    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(600.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, i: Signals(600.0, np.zeros(600), np.zeros(600), np.zeros(600), np.ones(600), np.ones(600), []))
    words = [transcribe.Word(f"word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(600)]
    monkeypatch.setattr(planner.transcribe, "transcribe", lambda p, i: transcribe.Transcript(words=words, language="hi", duration=600.0, engine="sarvam"))

    seen_used: list[moments.Moment] = []

    def fake_pick_speech(w, s, *, count, title="", used=(), **kw):
        seen_used.extend(used)
        return [
            moments.Moment(80.0, 145.0, title="Glass Bridge Terror", score=88.0, kind="speech"),
            moments.Moment(160.0, 225.0, title="Collected Stallion Sperm On Farm", score=90.0, kind="speech"),
        ]

    monkeypatch.setattr(planner.moments, "pick_speech", fake_pick_speech)
    segs = planner._moments_from_source(src, "@souravjvlogs", existing_used=[], max_needed=4)
    assert any(m.start == 0.0 and m.end == 45.0 for m in seen_used)
    assert any(m.start == 575.0 and m.end == 600.0 for m in seen_used)
    assert len(segs) == 1 and "Glass Bridge" in segs[0].moment_title

    # 2. _curate_story_arc preserves chronological order within the same source_id and cleans chapter titles.
    cands = [
        _seg("v1", "@souravjvlogs", 120.0, 190.0, 82.0),
        _seg("v1", "@souravjvlogs", 480.0, 560.0, 95.0),
        _seg("v2", "@souravjvlogs", 200.0, 270.0, 86.0),
        _seg("v1", "@souravjvlogs", 60.0, 115.0, 80.0),
    ]
    monkeypatch.setattr(planner.ai, "available", lambda: True)
    monkeypatch.setattr(
        planner.ai,
        "ask_json",
        lambda prompt: {
            "indices": [1, 2, 0, 3],
            "titles": {"1": "Wild Climax... #Shorts", "2": "Train Arrival", "0": "Bridge Terror", "3": "Morning Hike"},
            "theme_hook": "Vietnam trip",
        },
    )
    curated = planner._curate_story_arc(
        cands, format_kind="best_of", targets=["@souravjvlogs"], theme="", language="hi", target_sec=300.0, min_total_sec=120.0,
    )
    v1_starts = [s.start for s in curated if s.source_id == "v1"]
    assert v1_starts == sorted(v1_starts)
    assert all("#Shorts" not in s.moment_title and "..." not in s.moment_title for s in curated)

    # 3. pick_cold_open avoids 0..4s dark fade-in when climax starts at 0.0.
    co = planner.pick_cold_open([_seg("v0", "@A", 0.0, 60.0, 99.0)])
    assert co.start >= 4.0 and 4.0 <= co.duration <= 6.0


def test_curate_story_arc_8_to_9_min_act_cap_and_hinglish_titles(monkeypatch):
    cands = [
        planner.SegmentPlan(
            source_path=f"/tmp/v{i // 4}.mp4",
            source_url=f"https://youtu.be/v{i // 4}",
            source_id=f"v{i // 4}",
            creator="@souravjvlogs",
            creator_name="Sourav Joshi",
            video_title=f"Vlog {i}",
            start=50.0 + (i % 4) * 70.0,
            end=50.0 + (i % 4) * 70.0 + 58.0,
            score=75.0 + i,
            moment_title="Mountain Magic" if i % 2 == 0 else "Pahadon Mein Train",
            language="hi",
            transcript_excerpt="yahan par bahut bada bridge aur train ride dikh rahi hai",
        )
        for i in range(12)
    ]
    seen_prompts: list[str] = []
    monkeypatch.setattr(planner.ai, "available", lambda: True)

    def fake_ask_8(prompt):
        seen_prompts.append(prompt)
        return {
            "indices": list(range(9)),
            "titles": {
                "0": "Pahadon Mein Train Cafe",
                "1": "Mountain Magic",  # pure English should be normalized to Hinglish
            },
        }

    monkeypatch.setattr(planner.ai, "ask_json", fake_ask_8)
    curated = planner._curate_story_arc(
        cands, format_kind="best_of", targets=["@souravjvlogs"], theme="", language="hi",
        target_sec=480.0, min_total_sec=120.0,
    )
    assert len(curated) == 9
    assert sum(s.duration for s in curated) >= 480.0
    assert "Hinglish/English" not in seen_prompts[0]
    assert "Hinglish" in seen_prompts[0]
    assert curated[0].moment_title == "Pahadon Mein Train Cafe"
    assert all(planner._HINGLISH_CUE_RE.search(s.moment_title) for s in curated)

    # Top-up triggers when AI picks only 3 acts (< desired_min for 480s).
    monkeypatch.setattr(planner.ai, "ask_json", lambda p: {"indices": [0, 1, 2], "titles": {"0": "Bridge Par Darr"}})
    topped = planner._curate_story_arc(
        cands, format_kind="best_of", targets=["@souravjvlogs"], theme="", language="hi",
        target_sec=480.0, min_total_sec=120.0,
    )
    assert len(topped) >= 8 and sum(s.duration for s in topped) >= 440.0
    assert all(planner._HINGLISH_CUE_RE.search(s.moment_title) for s in topped)

    fb_title, fb_desc, _ = planner._fallback_title_and_desc(
        format_kind="best_of", targets=["@souravjvlogs"], theme="", part_number=1, niche="", language="hi",
    )
    assert "Sourav Joshi" in fb_title and not fb_title.startswith("Best of ")
    assert "sabse" in fb_desc.lower()


def test_planner_filters_boring_vlog_filler_and_tightens_segment_bounds(tmp_path, monkeypatch):
    assert 35 <= planner._SEGMENT_MIN_SEC <= 42
    assert planner._SEGMENT_TARGET_SEC <= 62
    assert planner._SEGMENT_MAX_SEC <= 80

    vid = tmp_path / "vlog.mp4"
    vid.write_bytes(b"MP4")
    src = ingest.SourceVideo(path=str(vid), title="Vlog", url="https://youtu.be/v1", source="youtube", source_id="v1", author="@souravjvlogs")
    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(600.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, i: Signals(600.0, np.zeros(600), np.zeros(600), np.zeros(600), np.ones(600), np.ones(600), []))

    # Segment 1 (60..118s): boring hotel washroom + laundry filler (>=2 cues)
    # Segment 2 (150..210s): bunker challenge with only 1 incidental "bathroom" mention (kept!)
    words = [
        transcribe.Word("बहुत", 65.0, 65.5),
        transcribe.Word("छोटा", 66.0, 66.5),
        transcribe.Word("वॉशरूम", 67.0, 67.5),
        transcribe.Word("है", 68.0, 68.5),
        transcribe.Word("और", 85.0, 85.4),
        transcribe.Word("कपड़े", 86.0, 86.5),
        transcribe.Word("डाल", 87.0, 87.4),
        transcribe.Word("दिए", 88.0, 88.4),
        transcribe.Word("सुखाने", 89.0, 89.5),
        transcribe.Word("Survive", 160.0, 160.5),
        transcribe.Word("bunker", 161.0, 161.5),
        transcribe.Word("with", 162.0, 162.4),
        transcribe.Word("your", 163.0, 163.4),
        transcribe.Word("own", 164.0, 164.4),
        transcribe.Word("bathroom", 165.0, 165.5),
    ]
    monkeypatch.setattr(planner.transcribe, "transcribe", lambda p, i: transcribe.Transcript(words=words, language="hi", duration=600.0, engine="sarvam"))
    monkeypatch.setattr(
        planner.moments,
        "pick_speech",
        lambda *a, **k: [
            moments.Moment(60.0, 118.0, title="Hotel Room Tour", score=88.0, kind="speech"),
            moments.Moment(150.0, 210.0, title="Underground Bunker Challenge", score=92.0, kind="speech"),
        ],
    )
    segs = planner._moments_from_source(src, "@souravjvlogs", existing_used=[], max_needed=5)
    assert len(segs) == 1
    assert segs[0].moment_title == "Underground Bunker Challenge"


def test_plan_compilation_pool_collects_over_12_candidates_for_8_min(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    long_dir = tmp_path / "content" / "long"
    long_dir.mkdir(parents=True)

    sources: dict[str, Path] = {}
    entries: list[dict] = []
    for idx in range(1, 5):
        vid = f"v{idx}"
        mp4 = long_dir / f"Youtube_{vid}.mp4"
        mp4.write_bytes(b"V" * 60_000)
        Path(f"{mp4}.transcript.json").write_text('{"words": []}', encoding="utf-8")
        sources[vid] = mp4
        entries.append({"id": vid, "url": f"https://youtube.com/watch?v={vid}", "creator": "@Creator", "duration": 900})

    # Cached video with >=3 used spans ranks tier 1 (ahead of uncached tier 2).
    assert planner._entry_cache_tier(entries[0], {"v1": [moments.Moment(0, 10)] * 3}) == 1
    assert planner._entry_cache_tier({"id": "uncached_id"}, {}) == 2

    monkeypatch.setattr(planner, "_pick_source_candidates", lambda **k: (["@Creator"], entries))
    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(900.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, i: Signals(900.0, np.zeros(900), np.zeros(900), np.zeros(900), np.ones(900), np.ones(900), []))
    words = [transcribe.Word(f"word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(1200)]
    monkeypatch.setattr(planner.transcribe, "transcribe", lambda p, i: transcribe.Transcript(words=words, language="en", duration=900.0))
    monkeypatch.setattr(
        planner.moments,
        "pick_speech",
        lambda *a, **k: [
            moments.Moment(50.0 + j * 80.0, 105.0 + j * 80.0, title=f"Beat {j}", score=85.0 + j, kind="speech")
            for j in range(4)
        ],
    )
    monkeypatch.setattr(planner.ai, "available", lambda: True)
    seen_candidates: list[int] = []

    def fake_ask(prompt):
        if "Candidate moments:" in prompt:
            seen_candidates.append(prompt.count("[") - prompt.count('["'))
            return {"indices": list(range(9)), "titles": {str(i): f"Act {i} Showdown" for i in range(9)}}
        return {"title": "Creator Best Moments", "description": "Fun reel.", "tags": ["vlog"]}

    monkeypatch.setattr(planner.ai, "ask_json", fake_ask)
    plan = planner.plan_compilation(
        format_kind="best_of",
        channel_slug="default",
        targets=["@Creator"],
        target_minutes=8,
        min_total_sec=120.0,
    )
    assert seen_candidates and seen_candidates[0] > 12
    assert len(plan.segments) == 9


def test_plan_compilation_cold_open_uses_peak_score_after_chronology_swap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    long_dir = tmp_path / "content" / "long"
    long_dir.mkdir(parents=True)
    for vid in ("v1", "v2"):
        mp4 = long_dir / f"Youtube_{vid}.mp4"
        mp4.write_bytes(b"V" * 60_000)
        Path(f"{mp4}.transcript.json").write_text('{"words": []}', encoding="utf-8")

    entries = [
        {"id": "v1", "url": "https://youtube.com/watch?v=v1", "creator": "@Creator", "duration": 900},
        {"id": "v2", "url": "https://youtube.com/watch?v=v2", "creator": "@Creator", "duration": 900},
    ]
    monkeypatch.setattr(planner, "_pick_source_candidates", lambda **k: (["@Creator"], entries))
    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(900.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, i: Signals(900.0, np.zeros(900), np.zeros(900), np.zeros(900), np.ones(900), np.ones(900), []))
    words = [transcribe.Word(f"word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(1200)]
    monkeypatch.setattr(planner.transcribe, "transcribe", lambda p, i: transcribe.Transcript(words=words, language="en", duration=900.0))

    # v1 has its peak score (99) EARLIER (60..115s) than a lower-score segment (480..535s, score 81).
    # _enforce_source_chronology swaps the earlier v1 peak out of ordered[-1] to keep v1 chronological.
    call_idx = 0

    def fake_pick_speech(w, s, *, count, title="", used=(), **kw):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return [
                moments.Moment(60.0, 115.0, title="Peak Early Twist", score=99.0, kind="speech"),
                moments.Moment(480.0, 535.0, title="Late Calm Outro", score=81.0, kind="speech"),
            ]
        return [
            moments.Moment(150.0, 205.0, title="Mid Challenge Beat", score=86.0, kind="speech"),
            moments.Moment(280.0, 335.0, title="Second Beat", score=84.0, kind="speech"),
        ]

    monkeypatch.setattr(planner.moments, "pick_speech", fake_pick_speech)
    monkeypatch.setattr(planner.ai, "available", lambda: False)
    plan = planner.plan_compilation(
        format_kind="best_of",
        channel_slug="default",
        targets=["@Creator"],
        target_minutes=6,
        min_total_sec=120.0,
    )
    v1_starts = [s.start for s in plan.segments if s.source_id == "v1"]
    assert v1_starts == sorted(v1_starts)
    assert plan.segments[-1].score == 81.0
    assert plan.cold_open.score == 99.0
    assert 60.0 <= plan.cold_open.start < plan.cold_open.end <= 115.0


def test_vs_format_balances_two_creators_and_downgrades_single_creator_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    long_dir = tmp_path / "content" / "long"
    long_dir.mkdir(parents=True)
    for vid in ("a1", "b1"):
        mp4 = long_dir / f"Youtube_{vid}.mp4"
        mp4.write_bytes(b"M" * 60_000)
        Path(f"{mp4}.transcript.json").write_text('{"words": []}', encoding="utf-8")

    monkeypatch.setattr(planner, "probe", lambda p: MediaInfo(900.0, 1920, 1080, 30.0, True))
    monkeypatch.setattr(planner.signals, "analyse", lambda p, i: Signals(900.0, np.zeros(900), np.zeros(900), np.zeros(900), np.ones(900), np.ones(900), []))
    words = [transcribe.Word(f"word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(900)]
    monkeypatch.setattr(planner.transcribe, "transcribe", lambda p, i: transcribe.Transcript(words=words, language="en", duration=900.0))

    # Case 1: Only creator @Alpha actually yielded segments -> honestly downgrades vs -> best_of (no fake "Alpha vs Beta").
    entries_one = [{"id": "a1", "url": "https://youtube.com/watch?v=a1", "creator": "@Alpha", "duration": 900}]
    monkeypatch.setattr(planner, "_pick_source_candidates", lambda **k: (["@Alpha", "@Beta"], entries_one))
    monkeypatch.setattr(
        planner.moments,
        "pick_speech",
        lambda *a, **k: [
            moments.Moment(60.0 + j * 80.0, 118.0 + j * 80.0, title=f"Alpha Beat {j}", score=85.0 + j, kind="speech")
            for j in range(4)
        ],
    )
    monkeypatch.setattr(planner.ai, "available", lambda: False)
    downgraded = planner.plan_compilation(
        format_kind="vs", channel_slug="default", targets=["@Alpha", "@Beta"], target_minutes=6, min_total_sec=100.0,
    )
    assert downgraded.format_kind == "best_of"
    assert downgraded.targets == ("@Alpha",)
    assert " vs " not in downgraded.title.lower()

    # Case 2: Both @Alpha and @Beta exist, even if AI only picks indices 0..3 (@Alpha), _curate_story_arc injects >=2 @Beta acts.
    cands = [
        _seg("a1", "@Alpha", 60.0 + i * 70.0, 118.0 + i * 70.0, 90.0 - i) for i in range(4)
    ] + [
        _seg("b1", "@Beta", 60.0 + i * 70.0, 118.0 + i * 70.0, 88.0 - i) for i in range(4)
    ]
    monkeypatch.setattr(planner.ai, "available", lambda: True)
    monkeypatch.setattr(planner.ai, "ask_json", lambda p: {"indices": [0, 1, 2, 3], "titles": {}})
    balanced = planner._curate_story_arc(
        cands, format_kind="vs", targets=["@Alpha", "@Beta"], theme="", language="en", target_sec=360.0, min_total_sec=120.0,
    )
    beta_count = sum(1 for s in balanced if s.creator == "@Beta")
    assert beta_count >= 2
