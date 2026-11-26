"""Tests for the Automatic Ambient A/B testing engine and Control Panel leaderboard."""
from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels
from app.longform import ab, job as long_job
from app.models import Channel, GrowthState, LongJob, MetadataDraft, Video, VideoSnapshot, VideoStatus


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    init_db()
    with session_scope() as db:
        for tbl in (VideoSnapshot, LongJob, GrowthState, MetadataDraft, Video, Channel):
            db.execute(delete(tbl))
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {
                    "slug": "ambient",
                    "name": "Midnight Rain & Lofi",
                    "uploads_per_day": 0,
                    "long_uploads_per_day": 3,
                    "long_auto_publish": False,
                    "enabled": False,
                    "feeds": [],
                    "meta": {
                        "mode": "procedural_ambient",
                        "presets": ["lofi_chill", "rain_focus", "night_ambient", "deep_study"],
                        "target_minutes": 30,
                    },
                }
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "review_token", "ab-test-secret")
    monkeypatch.setattr(settings, "use_mock", True)


def test_ab_import_does_not_pull_numpy_or_cv2():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, app.longform.ab; "
                "assert 'numpy' not in sys.modules, 'numpy imported eagerly'; "
                "assert 'cv2' not in sys.modules, 'cv2 imported eagerly'"
            ),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_cold_start_rotates_untested_presets_and_thumbs_evenly(tmp_path):
    presets = ["lofi_chill", "rain_focus", "night_ambient", "deep_study"]
    portraits = [
        {"preset": "lofi_chill", "path": str(tmp_path / "lofi_a.jpg")},
        {"preset": "lofi_chill", "path": str(tmp_path / "lofi_b.jpg")},
    ]
    picked_presets: list[str] = []
    picked_thumbs: list[str] = []
    picked_portraits: list[str] = []

    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "ambient")
        assert ch is not None

        for idx in range(4):
            exp = ab.choose_next_experiment(db, ch, presets)
            assert exp["mode"] == "cold_start"
            picked_presets.append(exp["preset"])
            db.add(LongJob(channel_id=ch.id, format=exp["preset"], status="queued"))
            db.flush()

        for idx in range(3):
            exp = ab.choose_next_experiment(db, ch, ["lofi_chill"], portraits=portraits)
            picked_thumbs.append(exp["thumb"])
            picked_portraits.append(exp["portrait"])
            db.add(Video(
                source="longform",
                source_id=f"cs_{idx}",
                source_url="",
                title=f"Cold Start {idx}",
                status=VideoStatus.PENDING,
                channel_id=ch.id,
                meta={
                    "long_form": True,
                    "procedural_ambient": True,
                    "ambient_ab_arms": ab.record_arms(
                        preset="lofi_chill",
                        thumb_id=exp["thumb"],
                        portrait_path=exp["portrait_path"],
                    ),
                },
            ))
            db.flush()

    assert picked_presets == presets
    assert picked_thumbs == ["v1", "v2", "v3"]
    assert picked_portraits[:2] == ["lofi_a", "lofi_b"]


def test_exploitation_picks_winning_preset_portrait_and_thumb_ignoring_mock(tmp_path):
    presets = ["lofi_chill", "rain_focus"]
    portraits = [
        {"preset": "rain_focus", "path": str(tmp_path / "rain_cozy.jpg")},
        {"preset": "rain_focus", "path": str(tmp_path / "rain_dark.jpg")},
    ]
    now = datetime.now(timezone.utc)

    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "ambient")
        assert ch is not None

        # Real published winners vs losers across preset, portrait, and thumb (v2 wins).
        samples = [
            ("real_win_1", "yt_r1", "rain_focus", "v2", "rain_cozy.jpg", 12000, 62.0),
            ("real_win_2", "yt_r2", "rain_focus", "v2", "rain_cozy.jpg", 9500, 58.0),
            ("real_lose_1", "yt_l1", "lofi_chill", "v1", "rain_dark.jpg", 400, 22.0),
            ("real_lose_2", "yt_l2", "lofi_chill", "v3", "rain_dark.jpg", 350, 20.0),
            # High-view MOCK upload must be ignored by evaluate_ambient_ab
            ("mock_ignored", "MOCK_999", "lofi_chill", "v1", "rain_dark.jpg", 9_000_000, 99.0),
        ]
        for idx, (sid, ytid, preset, thumb, port, views, ret) in enumerate(samples):
            v = Video(
                source="longform",
                source_id=sid,
                source_url="",
                youtube_video_id=ytid,
                title=f"Vid {idx}",
                status=VideoStatus.PUBLISHED,
                published_at=now,
                views=views,
                channel_id=ch.id,
                meta={
                    "long_form": True,
                    "procedural_ambient": True,
                    "yt_analytics": {"averageViewPercentage": ret},
                    "ambient_ab_arms": ab.record_arms(preset=preset, thumb_id=thumb, portrait_path=port),
                },
            )
            db.add(v)
            db.flush()
            db.add(VideoSnapshot(video_id=v.id, age_hours=24.0, views=views))

        summary = ab.evaluate_ambient_ab(db, ch)
        assert summary["tested"] == 4
        assert summary["best"]["preset"] == "rain_focus"
        assert summary["best"]["thumb"] == "v2"
        assert summary["best"]["portrait"] == "rain_cozy"

        state = db.get(GrowthState, channels.state_key(ch, "ambient_ab"))
        assert state is not None and state.value["tested"] == 4

        # Zero explore_rate forces exploitation of winning arms.
        exp = ab.choose_next_experiment(
            db,
            ch,
            presets,
            portraits=portraits,
            explore_rate=0.0,
            rng=random.Random(42),
        )
        assert exp["mode"] == "exploit"
        assert exp["preset"] == "rain_focus"
        assert exp["thumb"] == "v2"
        assert exp["portrait"] == "rain_cozy"
        assert exp["portrait_path"] == str(tmp_path / "rain_cozy.jpg")


def test_select_thumbnail_updates_arms_and_panel_exposes_ambient_ab(tmp_path):
    from PIL import Image

    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "ambient")
        assert ch is not None
        tdir = tmp_path / "thumbs"
        tdir.mkdir()
        t1 = tdir / "v1.jpg"
        t2 = tdir / "v2.jpg"
        Image.new("RGB", (1280, 720), (10, 10, 20)).save(t1, format="JPEG")
        Image.new("RGB", (1280, 720), (20, 30, 50)).save(t2, format="JPEG")

        vid = Video(
            source="longform",
            source_id="sel_test",
            source_url="",
            title="Rain Mix",
            status=VideoStatus.PENDING,
            channel_id=ch.id,
            meta={
                "long_form": True,
                "procedural_ambient": True,
                "ambient_preset": "rain_focus",
                "portrait_path": str(tmp_path / "rain_cozy.jpg"),
                "thumbnails": [
                    {"id": "v1", "label": "V1", "path": str(t1)},
                    {"id": "v2", "label": "V2", "path": str(t2)},
                ],
                "selected_thumbnail": str(t1),
                "selected_thumbnail_id": "v1",
                "ambient_ab_arms": ab.record_arms(
                    preset="rain_focus", thumb_id="v1", portrait_path=str(tmp_path / "rain_cozy.jpg")
                ),
            },
        )
        db.add(vid)
        db.flush()
        video_id = vid.id

    res = long_job.select_thumbnail(video_id, "v2")
    assert res["selected_thumbnail_id"] == "v2"

    with session_scope() as db:
        row = db.get(Video, video_id)
        assert row is not None
        assert row.meta["ambient_ab_arms"] == {
            "preset": "rain_focus",
            "thumb": "v2",
            "portrait": "rain_cozy",
        }

    panel = long_job.long_panel_data()
    assert panel["ambient_ab"] is not None
    assert panel["ambient_ab"]["tested"] == 0
    assert set(panel["ambient_ab"]["factors"]) == {"preset", "thumb", "portrait"}


def test_unprefixed_universal_portraits_participate_in_ab_selection(tmp_path):
    universal = [{"preset": "", "path": str(tmp_path / "Girl_portrait_at_night_1.jpg")}]
    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "ambient")
        assert ch is not None
        exp = ab.choose_next_experiment(db, ch, ["night_ambient"], portraits=universal)
        assert exp["portrait"] == "Girl_portrait_at_night_1"
        assert exp["portrait_path"] == universal[0]["path"]


def test_re_render_ambient_with_portrait_syncs_ambient_ab_arms(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from app.longform import ambient

    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "ambient")
        assert ch is not None
        old_mp4 = tmp_path / "amb_vid.mp4"
        old_mp4.write_bytes(b"MP4")
        vid = Video(
            source="longform",
            source_id="amb_rerender",
            source_url="",
            title="Midnight Rain",
            status=VideoStatus.PENDING,
            duration_sec=1800,
            channel_id=ch.id,
            transformed_path=str(old_mp4),
            meta={
                "long_form": True,
                "procedural_ambient": True,
                "ambient_preset": "lofi_chill",
                "selected_thumbnail_id": "v2",
                "portrait_path": str(tmp_path / "old_scene.jpg"),
                "ambient_ab_arms": ab.record_arms(
                    preset="lofi_chill", thumb_id="v2", portrait_path="old_scene.jpg"
                ),
            },
        )
        db.add(vid)
        db.flush()
        video_id = vid.id

    thumb_v1 = tmp_path / "v1.jpg"
    Image.new("RGB", (1280, 720), (30, 40, 50)).save(thumb_v1, format="JPEG")
    hero_jpg = tmp_path / "hero_raw.jpg"
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(hero_jpg, format="JPEG")

    monkeypatch.setattr(
        ambient,
        "generate_ambient_video",
        lambda **kw: SimpleNamespace(
            thumbnails=[{"id": "v1", "label": "V1", "path": str(thumb_v1)}],
            output_path=kw["output_path"],
            duration=1800.0,
            hero_frame=str(hero_jpg),
            thumbnail_prompt="prompt",
            chapters=[],
        ),
    )

    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (90, 100, 110)).save(buf, format="JPEG")
    res = long_job.re_render_ambient_with_portrait(video_id, buf.getvalue())
    assert res["ok"] is True

    with session_scope() as db:
        row = db.get(Video, video_id)
        assert row is not None
        assert row.meta["ambient_ab_arms"] == {
            "preset": "lofi_chill",
            "thumb": "v1",
            "portrait": f"{video_id}_custom",
        }


def test_get_long_videos_does_not_write_ambient_ab_growth_state():
    from fastapi.testclient import TestClient
    from app.review import review_app

    now = datetime.now(timezone.utc)
    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "ambient")
        assert ch is not None
        state_key = channels.state_key(ch, "ambient_ab")
        db.add(Video(
            source="longform",
            source_id="amb_pub_1",
            source_url="",
            youtube_video_id="yt_real_1",
            title="Real Published Ambient",
            status=VideoStatus.PUBLISHED,
            published_at=now,
            views=500,
            channel_id=ch.id,
            meta={
                "long_form": True,
                "procedural_ambient": True,
                "ambient_ab_arms": ab.record_arms(preset="lofi_chill", thumb_id="v1", portrait_path="p1.jpg"),
            },
        ))
        db.flush()
        assert db.get(GrowthState, state_key) is None

    client = TestClient(review_app)
    assert client.get("/?key=ab-test-secret", follow_redirects=False).status_code == 303
    resp = client.get("/api/long/videos")
    assert resp.status_code == 200
    assert resp.json()["ambient_ab"]["tested"] == 1

    with session_scope() as db:
        ch = channels.channel_by_slug(db, "ambient")
        assert ch is not None
        state_key = channels.state_key(ch, "ambient_ab")
        assert db.get(GrowthState, state_key) is None
        ab.evaluate_ambient_ab(db, ch)
        assert db.get(GrowthState, state_key) is not None


