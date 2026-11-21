"""Tests for LongJob queue, RENDER_LOCK, strict Shorts/Long-Form isolation, autopilot, panel routes, cleanup & CLI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import scheduler
from app.clipper import job as clip_job
from app.config import settings
from app.db import init_db, session_scope
from app.growth import approval, autopilot, channels, learn, safety
from app.longform import job as long_job, planner, render as lf_render
from app.models import Channel, GrowthEvent, GrowthState, LongJob, MetadataDraft, QuotaUsage, Video, VideoStatus
from app.review import review_app


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    init_db()
    with session_scope() as db:
        for tbl in (LongJob, GrowthEvent, GrowthState, QuotaUsage, MetadataDraft, Video, Channel):
            db.execute(delete(tbl))
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    (tmp_path / "channels.json").write_text(json.dumps({
        "channels": [
            {"slug": "default", "niche": "comedy", "long_uploads_per_day": 1, "long_auto_publish": False},
            {"slug": "indianvlogs", "name": "Mr Indian Vlogs", "niche": "hindi", "uploads_per_day": 3,
             "long_uploads_per_day": 1, "long_auto_publish": False, "timezone": "Asia/Kolkata",
             "feeds": ["https://www.youtube.com/@souravjvlogs", "https://www.youtube.com/@FukraInsaan"]},
        ]
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "content_dir", str(tmp_path / "content"))
    monkeypatch.setattr(settings, "review_token", "test-secret")
    monkeypatch.setattr(settings, "use_mock", True)


def _signed_client() -> TestClient:
    client = TestClient(review_app)
    assert client.get("/?key=test-secret", follow_redirects=False).status_code == 303
    return client


def test_real_channels_json_is_valid_and_sets_both_channels_to_1_long_per_day(monkeypatch):
    real_cfg = Path(__file__).resolve().parent.parent / "channels.json"
    monkeypatch.setattr(settings, "channels_file", str(real_cfg))
    entries = channels._load_config()
    assert entries is not None
    by_slug = {e["slug"]: e for e in entries}
    assert by_slug["default"]["long_uploads_per_day"] == 1
    assert by_slug["default"]["long_auto_publish"] is False
    assert by_slug["indianvlogs"]["long_uploads_per_day"] == 1
    assert by_slug["indianvlogs"]["long_auto_publish"] is False


def test_enqueue_run_worker_isolation_and_render_lock(tmp_path, monkeypatch):
    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "indianvlogs")

    # 1. Verify RENDER_LOCK contention skips without blocking.
    clip_job.RENDER_LOCK.acquire()
    try:
        assert long_job.run_next_long_job() == {"busy": True}
    finally:
        clip_job.RENDER_LOCK.release()

    # 2. Enqueue + run mocked build.
    job_id = long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs", target_minutes=8)

    from app.longform import thumbnails as lf_thumbs

    seg = planner.SegmentPlan("s.mp4", "https://youtu.be/abc", "abc", "@souravjvlogs", "Sourav", "Vlog 1", 5.0, 45.0, 90.0, "Funny Moment", "hi")
    fake_plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",), theme="",
        title="Best of @souravjvlogs", description="Hilarious highlights", tags=("hindi vlog",),
        language="hi", target_seconds=480.0, cold_open=seg, segments=(seg,),
    )
    monkeypatch.setattr(planner, "plan_compilation", lambda **k: fake_plan)

    def fake_render(plan, out_path, **k):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"MP4" * 500)
        return lf_render.RenderResult(
            output_path=out_path, duration=390.0, chapters=((0.0, "Intro / Preview"), (5.0, "#1 Funny Moment")),
            chapter_text="0:00 Intro / Preview\n0:05 #1 Funny Moment", timestamps_for_thumbs=(15.0,),
        )

    def fake_thumbs(vp, *, out_dir, **k):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "hero_raw.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"H" * 100)
        res = []
        for i in (1, 2, 3):
            tp = Path(out_dir) / f"thumb_v{i}.jpg"
            tp.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
            res.append({"id": f"v{i}", "label": f"Variant {i}", "path": str(tp)})
        return res, "Test AI prompt"

    monkeypatch.setattr(lf_render, "render_compilation", fake_render)
    monkeypatch.setattr(lf_thumbs, "generate_thumbnails_with_prompt", fake_thumbs)
    alerts: list[dict] = []
    monkeypatch.setattr(long_job.notify, "send_long_ready", lambda t, **kw: alerts.append({"title": t, **kw}) or True)

    out = long_job.run_next_long_job()
    assert out["ok"] is True
    assert len(alerts) == 1 and alerts[0]["channel_name"] == "Mr Indian Vlogs" and alerts[0]["prompt"] == "Test AI prompt"
    video_id = out["video_id"]

    with session_scope() as db:
        v = db.get(Video, video_id)
        assert v is not None and v.status == VideoStatus.PENDING
        assert v.width == 1920 and v.height == 1080
        assert v.meta["long_form"] is True and v.meta["clip_rendered"] is False
        assert Path(v.meta["hero_frame"]).is_file()
        assert "#Shorts" not in v.title

        # Strict isolation: Shorts approval/autopilot/learn ignore long_form rows!
        assert [x.id for x in approval.awaiting_review(db)] == []
        assert [x.id for x in approval.awaiting_scraped(db)] == []
        assert [x.id for x in autopilot.ready_clips(db)] == []

        # Supply respects stock cap & skips while 1 video is already waiting.
    # Add second waiting long video -> supply stops at cap=2.
    with session_scope() as db:
        p2 = Path(settings.content_dir) / "longform" / "indianvlogs" / "v2.mp4"
        p2.parent.mkdir(parents=True, exist_ok=True)
        p2.write_bytes(b"x" * 100)
        db.add(Video(
            source="longform", source_id="v2", source_url="https://youtu.be/v2", title="V2", transformed_path=str(p2),
            status=VideoStatus.TRANSFORMED, channel_id=ch.id, meta={"long_form": True},
        ))
    res_sup = long_job.long_supply_job()
    assert res_sup["indianvlogs"]["queued"] is False and res_sup["indianvlogs"]["stock"] == 2


def test_long_autopilot_respects_daily_cap_and_opt_in_auto_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "enable_publish", True)
    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "indianvlogs")
        ch.client_secrets = ""  # MockPublisher mode
        mp4 = tmp_path / "long1.mp4"
        mp4.write_bytes(b"x" * 2048)
        thumb = tmp_path / "t.jpg"
        thumb.write_bytes(b"jpg")
        v1 = Video(
            source="longform", source_id="la1", source_url="https://youtu.be/la1", title="Best of @souravjvlogs",
            description="Chapters:\n0:00 Intro", transformed_path=str(mp4),
            status=VideoStatus.PENDING, channel_id=ch.id,
            meta={"long_form": True, "selected_thumbnail": str(thumb)},
        )
        db.add(v1)
        db.flush()
        v1_id = v1.id

    # long_auto_publish=False: PENDING stays waiting.
    r1 = long_job.long_autopilot_job()
    assert r1["indianvlogs"]["skipped"] == "none_ready"

    # Flip long_auto_publish=True -> promotes PENDING and uploads once, second run hits daily_cap!
    channels.update_channel("indianvlogs", long_auto_publish=True)
    r2 = long_job.long_autopilot_job()
    assert r2["indianvlogs"]["ok"] is True
    with session_scope() as db:
        assert db.get(Video, v1_id).status == VideoStatus.PUBLISHED
        evs = db.execute(select(GrowthEvent).where(GrowthEvent.kind == "long_posted")).scalars().all()
        assert len(evs) == 1
        # Confirm matured_results still excludes long_form videos from Shorts A/B modeling.
        assert learn.matured_results(db) == []

    r3 = long_job.long_autopilot_job()
    assert r3["indianvlogs"]["skipped"] == "daily_cap"


def test_long_panel_endpoints_guarded_and_full_lifecycle(tmp_path, monkeypatch):
    # Unauthenticated requests rejected with 401.
    anon = TestClient(review_app)
    for method, path in (
        ("GET", "/api/long/videos"),
        ("POST", "/api/long/build"),
        ("GET", "/api/long/jobs"),
        ("GET", "/api/long/x/hero"),
        ("POST", "/api/long/x/approve"),
        ("POST", "/api/long/x/reject"),
        ("POST", "/api/long/x/metadata"),
        ("POST", "/api/long/x/thumbnail"),
    ):
        resp = anon.request(method, path, json={})
        assert resp.status_code == 401

    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    client = _signed_client()

    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "indianvlogs")
        mp4 = tmp_path / "comp.mp4"
        mp4.write_bytes(b"MP4DATA")
        thumbs_dir = tmp_path / "thumbs"
        thumbs_dir.mkdir()
        tpaths = {}
        for vid_key in ("v1", "v2"):
            tp = thumbs_dir / f"{vid_key}.jpg"
            from PIL import Image
            Image.new("RGB", (1280, 720), (100, 100, 200)).save(tp, format="JPEG")
            tpaths[vid_key] = str(tp)
        hero_raw = thumbs_dir / "hero_raw.jpg"
        Image.new("RGB", (1280, 720), (20, 180, 90)).save(hero_raw, format="JPEG")
        v = Video(
            source="longform", source_id="panel1", source_url="https://youtu.be/panel1", title="Old Title", description="0:00 Intro",
            transformed_path=str(mp4), status=VideoStatus.PENDING, channel_id=ch.id, width=1920, height=1080,
            meta={
                "long_form": True,
                "hero_frame": str(hero_raw),
                "thumbnails": [
                    {"id": "v1", "label": "Hero", "path": tpaths["v1"]},
                    {"id": "v2", "label": "Split", "path": tpaths["v2"]},
                ],
                "selected_thumbnail": tpaths["v1"],
                "selected_thumbnail_id": "v1",
            },
        )
        db.add(v)
        db.flush()
        vid = v.id

    assert client.post("/api/long/build", json={"channel_slug": "indianvlogs", "format": "vs", "target": "@A vs @B"}).status_code == 200
    data = client.get("/api/long/videos").json()
    assert len(data["pending"]) == 1 and len(data["jobs"]) == 1
    assert data["pending"][0]["hero_url"].startswith(f"/api/long/{vid}/hero")
    hero_resp = client.get(f"/api/long/{vid}/hero")
    assert hero_resp.status_code == 200 and hero_resp.headers["content-type"] == "image/jpeg"
    assert "attachment" in hero_resp.headers["content-disposition"]
    assert client.get(f"/api/long/{vid}/thumb/v2").status_code == 200
    assert client.post(f"/api/long/{vid}/thumbnail", json={"variant_id": "v2"}).json()["selected_thumbnail_id"] == "v2"

    # Upload custom thumbnail
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (240, 80, 20)).save(buf, format="PNG")
    up = client.post(f"/api/long/{vid}/thumbnail/upload", content=buf.getvalue())
    assert up.status_code == 200 and up.json()["selected_thumbnail_id"] == "custom"

    # Update metadata & approve (no #Shorts!)
    app_res = client.post(f"/api/long/{vid}/approve", json={"title": "Clean New Long Title", "description": "0:00 Intro\n0:10 Best Part", "tags": ["vlog", "funny"]})
    assert app_res.status_code == 200 and app_res.json()["status"] == "transformed"
    with session_scope() as db:
        row = db.get(Video, vid)
        assert row.title == "Clean New Long Title"
        assert "#Shorts" not in row.title
        draft = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == vid)).scalar_one()
        assert draft.title == "Clean New Long Title"


def test_cleanup_protects_live_longform_and_reclaims_expired(tmp_path):
    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.default_channel(db)
        root = Path(settings.content_dir) / "longform" / ch.slug
        root.mkdir(parents=True, exist_ok=True)
        live_mp4 = root / "live.mp4"
        live_mp4.write_bytes(b"A" * 5000)
        live_thumb = root / "live_thumbs" / "thumb_v1.jpg"
        live_thumb.parent.mkdir(parents=True, exist_ok=True)
        live_thumb.write_bytes(b"B" * 2000)
        live_hero = root / "live_thumbs" / "hero_raw.jpg"
        live_hero.write_bytes(b"H" * 1500)

        dead_mp4 = root / "old.mp4"
        dead_mp4.write_bytes(b"C" * 5000)
        dead_thumb = root / "old_thumbs" / "thumb_v1.jpg"
        dead_thumb.parent.mkdir(parents=True, exist_ok=True)
        dead_thumb.write_bytes(b"D" * 2000)
        dead_hero = root / "old_thumbs" / "hero_raw.jpg"
        dead_hero.write_bytes(b"H" * 1500)

        old_ts = datetime.now(timezone.utc) - timedelta(days=30)
        db.add(Video(
            source="longform", source_id="live1", source_url="https://youtu.be/live1", title="Live",
            local_path=str(live_mp4), transformed_path=str(live_mp4),
            status=VideoStatus.PENDING, channel_id=ch.id,
            meta={"long_form": True, "hero_frame": str(live_hero), "selected_thumbnail": str(live_thumb), "thumbnails": [{"id": "v1", "path": str(live_thumb)}]},
        ))
        db.add(Video(
            source="longform", source_id="dead1", source_url="https://youtu.be/dead1", title="Dead",
            local_path=str(dead_mp4), transformed_path=str(dead_mp4),
            status=VideoStatus.PUBLISHED, published_at=old_ts, channel_id=ch.id,
            meta={"long_form": True, "hero_frame": str(dead_hero), "selected_thumbnail": str(dead_thumb), "thumbnails": [{"id": "v1", "path": str(dead_thumb)}]},
        ))

    res = scheduler.cleanup_job()
    assert res["transforms_deleted"] >= 1
    assert live_mp4.exists() and live_thumb.exists() and live_hero.exists()
    assert not dead_mp4.exists() and not dead_thumb.exists() and not dead_hero.exists()


def test_compile_cli_enqueue_and_jobs_json(capsys):
    from scripts import compile as compile_cli

    with session_scope() as db:
        channels.sync_channels(db)
    rc = compile_cli.main(["--channel", "indianvlogs", "--format", "best_of", "--creator", "@souravjvlogs", "--minutes", "7", "--enqueue", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["job_id"]
    rc2 = compile_cli.main(["--jobs", "--json"])
    assert rc2 == 0
    jobs_out = json.loads(capsys.readouterr().out)
    assert len(jobs_out["jobs"]) == 1 and jobs_out["jobs"][0]["id"] == out["job_id"]


def test_countdown_lower_thirds_and_chapters_in_render_compilation(tmp_path, monkeypatch):
    from app.clipper.media import MediaInfo

    src = tmp_path / "src.mp4"
    src.write_bytes(b"MP4")
    s1 = planner.SegmentPlan(str(src), "https://youtu.be/1", "1", "@One", "One", "Vid", 0.0, 20.0, 80.0, "Funny Entrance #Shorts", "en")
    s2 = planner.SegmentPlan(str(src), "https://youtu.be/2", "2", "@One", "One", "Vid", 25.0, 45.0, 99.0, "Crazy Insane Ending Payoff", "en")
    co = planner.pick_cold_open([s1, s2], climax=s2)
    plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="default", part_number=1, targets=("@One",), theme="",
        title="Best of One", description="Desc", tags=("vlog",), language="en", target_seconds=120.0,
        cold_open=co, segments=(s1, s2),
    )
    ass_captured: list[str] = []

    def fake_probe(path):
        return MediaInfo(20.0 if "part_" in str(path) else 100.0, 1920, 1080, 30.0, False)

    def fake_ffmpeg(args, **kwargs):
        out_file = Path(args[-1])
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(b"X" * 2000)

    monkeypatch.setattr(lf_render, "probe", fake_probe)
    monkeypatch.setattr(lf_render, "_probe_duration", lambda p, f: f)
    monkeypatch.setattr(lf_render, "run_ffmpeg", fake_ffmpeg)
    orig_write = lf_render._write_ass_overlay

    def cap_ass(ass_path, **kw):
        orig_write(ass_path, **kw)
        ass_captured.append(ass_path.read_text(encoding="utf-8"))

    monkeypatch.setattr(lf_render, "_write_ass_overlay", cap_ass)
    res = lf_render.render_compilation(plan, str(tmp_path / "out.mp4"), brand_name="@Brand", include_end_screen=False, include_voiceover=False)
    assert [lbl.split()[0] for _, lbl in res.chapters] == ["Intro", "#2", "#1"]
    assert "COMING UP · #1" in ass_captured[0]
    assert "#2 · @One — Funny Entrance" in ass_captured[1] and "#Shorts" not in ass_captured[1]
    assert "#1 · @One — Crazy Insane Ending Payoff" in ass_captured[2]


def test_repair_waiting_drafts_strips_shorts_and_unlocks_ab_safely(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"MP4")
    with session_scope() as db:
        v_auto = Video(
            source="youtube", source_id="s1", source_url="https://youtu.be/1",
            title="Shocking Prank Gone Wild #Shorts", transformed_path=str(p),
            status=VideoStatus.TRANSFORMED, meta={"clip_rendered": True, "llm_title": "Shocking Prank Gone Wild"},
        )
        v_human = Video(
            source="youtube", source_id="s2", source_url="https://youtu.be/2",
            title="Auto Title #Shorts", transformed_path=str(p),
            status=VideoStatus.TRANSFORMED, meta={"clip_rendered": True, "llm_title": "Auto Title"},
        )
        v_pub = Video(
            source="youtube", source_id="s3", source_url="https://youtu.be/3",
            title="Published Already #Shorts", transformed_path=str(p),
            status=VideoStatus.PUBLISHED, meta={"clip_rendered": True},
        )
        v_long = Video(
            source="longform", source_id="l1", source_url="https://youtu.be/l1",
            title="Long #Shorts", transformed_path=str(p),
            status=VideoStatus.PENDING, meta={"long_form": True},
        )
        db.add_all([v_auto, v_human, v_pub, v_long])
        db.flush()
        db.add(MetadataDraft(video_id=v_auto.id, title="Shocking Prank Gone Wild #Shorts", edited=True))
        db.add(MetadataDraft(video_id=v_human.id, title="My Handcrafted Title #Shorts", edited=True))
        db.add(MetadataDraft(video_id=v_pub.id, title="Published Already #Shorts", edited=True))
        db.flush()
        ids = (v_auto.id, v_human.id, v_pub.id, v_long.id)

    dry = approval.repair_waiting_drafts(dry_run=True)
    assert dry == {"checked": 2, "tags_stripped": 2, "unlocked_ab": 1, "dry_run": True}

    res = approval.repair_waiting_drafts(dry_run=False)
    assert res == {"checked": 2, "tags_stripped": 2, "unlocked_ab": 1, "dry_run": False}

    with session_scope() as db:
        va = db.get(Video, ids[0])
        da = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == ids[0])).scalar_one()
        vh = db.get(Video, ids[1])
        dh = db.execute(select(MetadataDraft).where(MetadataDraft.video_id == ids[1])).scalar_one()
        vp = db.get(Video, ids[2])
        vl = db.get(Video, ids[3])
        assert va.title == "Shocking Prank Gone Wild" and da.title == "Shocking Prank Gone Wild" and da.edited is False
        assert vh.title == "Auto Title" and dh.title == "My Handcrafted Title" and dh.edited is True
        assert vp.title == "Published Already #Shorts"
        assert vl.title == "Long #Shorts"


def test_voiceover_script_and_render_compilation_with_narrator_cards_and_fallback(tmp_path, monkeypatch):
    from app.clipper.media import MediaInfo
    from app.longform import voiceover

    src = tmp_path / "src.mp4"
    src.write_bytes(b"MP4")
    s1 = planner.SegmentPlan(str(src), "https://youtu.be/1", "1", "@One", "One", "Vid", 0.0, 20.0, 80.0, "Funny Opening", "hi")
    s2 = planner.SegmentPlan(str(src), "https://youtu.be/2", "2", "@One", "One", "Vid", 25.0, 45.0, 99.0, "Wild Finale", "hi")
    co = planner.pick_cold_open([s1, s2], climax=s2)
    plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@One",), theme="",
        title="Best of One", description="Desc", tags=("vlog",), language="hi", target_seconds=120.0,
        cold_open=co, segments=(s1, s2),
    )

    monkeypatch.setattr(voiceover.ai, "available", lambda: True)
    monkeypatch.setattr(
        voiceover.ai,
        "ask_json",
        lambda p: {"intro": "Aaj ke top 2 moments dekho!", "bridges": ["Number 2 dekho.", "Number 1 finale!"], "outro": "Comment karo!"},
    )
    script = voiceover.generate_commentary_script(plan)
    assert "top 2 moments" in script["intro"]
    assert len(script["bridges"]) == 2

    def fake_probe(path):
        name = Path(path).name
        return MediaInfo(100.0 if name == "src.mp4" else 15.0, 1920, 1080, 30.0, False)

    def fake_ffmpeg(args, **kwargs):
        out_file = Path(args[-1])
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(b"X" * 2000)

    synthesized: list[str] = []

    def fake_synth(text, out_wav, **kw):
        synthesized.append(text)
        Path(out_wav).write_bytes(b"RIFF" + b"0" * 500)
        return 2.4

    monkeypatch.setattr(lf_render, "probe", fake_probe)
    monkeypatch.setattr(lf_render, "_probe_duration", lambda p, f: f)
    monkeypatch.setattr(lf_render, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(voiceover, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(voiceover, "synthesize_wav", fake_synth)

    cards_rendered: list[str] = []
    monkeypatch.setattr(
        voiceover,
        "render_narrator_card",
        lambda wav, bg, out, td, *, badge_text, **kw: cards_rendered.append(badge_text) or 2.5,
    )

    res = lf_render.render_compilation(plan, str(tmp_path / "vo_out.mp4"), brand_name="@Brand", include_end_screen=True, include_voiceover=True)
    assert cards_rendered == ["COUNTDOWN", "#2", "#1"]
    assert len(synthesized) == 4  # intro + 2 bridges + outro
    assert [lbl.split()[0] for _, lbl in res.chapters] == ["Intro", "#2", "#1"]

    # Graceful fallback when synthesize_wav returns None
    monkeypatch.setattr(voiceover, "synthesize_wav", lambda *a, **k: None)
    cards_rendered.clear()
    res_fb = lf_render.render_compilation(plan, str(tmp_path / "fb_out.mp4"), brand_name="@Brand", include_end_screen=False, include_voiceover=True)
    assert cards_rendered == []
    assert [lbl.split()[0] for _, lbl in res_fb.chapters] == ["Intro", "#2", "#1"]
