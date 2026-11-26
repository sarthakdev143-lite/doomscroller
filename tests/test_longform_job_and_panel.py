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
    assert "https://www.youtube.com/@MrBeast" in by_slug["default"]["feeds"]
    assert by_slug["indianvlogs"]["uploads_per_day"] == 3
    assert by_slug["indianvlogs"]["long_uploads_per_day"] == 1
    assert by_slug["indianvlogs"]["long_auto_publish"] is False
    amb = by_slug["ambient"]
    assert amb["uploads_per_day"] == 0
    assert amb["long_uploads_per_day"] == 3
    assert amb["long_auto_publish"] is False
    assert amb["require_approval"] is True
    assert amb["enabled"] is False
    assert amb["feeds"] == []
    assert amb["meta"]["mode"] == "procedural_ambient"
    assert amb["meta"]["target_minutes"] == 60
    assert amb["meta"]["generate_while_paused"] is True


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


def test_compile_cli_accepts_ambient_format_and_default_minutes(tmp_path, capsys):
    from scripts import compile as compile_cli

    Path(settings.channels_file).write_text(json.dumps({
        "channels": [{
            "slug": "ambient",
            "name": "Midnight Rain & Lofi",
            "uploads_per_day": 0,
            "long_uploads_per_day": 3,
            "long_auto_publish": False,
            "enabled": False,
            "feeds": [],
            "meta": {"mode": "procedural_ambient", "presets": ["lofi_chill", "rain_focus"], "target_minutes": 30},
        }]
    }), encoding="utf-8")
    with session_scope() as db:
        channels.sync_channels(db)
    rc = compile_cli.main(["--channel", "ambient", "--format", "rain_focus", "--enqueue", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    with session_scope() as db:
        amb_job = db.get(LongJob, out["job_id"])
        assert amb_job is not None and amb_job.format == "rain_focus" and amb_job.target_minutes == 30


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
    assert "#2 · One — Funny Entrance" in ass_captured[1] and "#Shorts" not in ass_captured[1]
    assert "#1 · One — Crazy Insane Ending Payoff" in ass_captured[2]
    assert [lbl for _, lbl in res.chapters] == [
        "Intro / Preview",
        "#2 One — Funny Entrance #Shorts",
        "#1 One — Crazy Insane Ending Payoff",
    ]
    assert "@" not in res.chapter_text


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


def test_voiceover_script_sarvam_tts_quota_and_live_bridges(tmp_path, monkeypatch):
    import base64
    import io
    import wave
    from app.clipper.media import MediaInfo
    from app.longform import voiceover

    # 1. _short_caption strips dangling trailing words and ellipsis cleanly.
    assert lf_render._short_caption("I Set Myself on Fire for #Shorts") == "I Set Myself on Fire"
    assert lf_render._short_caption("MrBeast vs KSI: Extreme Stunts &") == "MrBeast vs KSI: Extreme Stunts"

    # 2. Devanagari & transcript excerpt prompt check for hi plans + pronunciation normalization.
    src = tmp_path / "src.mp4"
    src.write_bytes(b"MP4")
    s1 = planner.SegmentPlan(
        str(src), "https://youtu.be/1", "1", "@souravjvlogs", "Sourav", "Vlog", 5.0, 55.0, 80.0,
        "Glass Bridge Fear", "hi", transcript_excerpt="avantika bridge par dar gayi",
    )
    s2 = planner.SegmentPlan(
        str(src), "https://youtu.be/2", "2", "@souravjvlogs", "Sourav", "Vlog", 65.0, 125.0, 99.0,
        "Alpine Coaster", "hi", transcript_excerpt="coaster bahut tez tha",
    )
    co = planner.pick_cold_open([s1, s2], climax=s2)
    plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",), theme="",
        title="Best of Sourav Joshi", description="Desc", tags=("vlog",), language="hi", target_seconds=180.0,
        cold_open=co, segments=(s1, s2),
    )

    seen_prompts: list[str] = []
    monkeypatch.setattr(voiceover.ai, "available", lambda: True)

    def fake_ask_json(prompt):
        seen_prompts.append(prompt)
        return {
            "hook": "भाई इस रोलर कोस्टर में क्या होने वाला है!",
            "bridges": ["सौरव और अवंतिका पुल पर फंस गए।", "और अब सबसे तेज राइड शुरू होती है!"],
            "outro": "आपको कौन सा सीन पसंद आया कमेंट करो!",
        }

    monkeypatch.setattr(voiceover.ai, "ask_json", fake_ask_json)
    script = voiceover.generate_commentary_script(plan)
    assert "रोलर कोस्टर" in script["hook"] and script["intro"] == script["hook"]
    assert len(script["bridges"]) == 2
    assert "Devanagari" in seen_prompts[0] and "avantika bridge par dar gayi" in seen_prompts[0]
    assert voiceover._clean_spoken("@MrBeast vs @souravjvlogs!") == "Mr. Beast vs Sourav Joshi!"

    # 3. synthesize_wav hits Sarvam bulbul:v3 REST API (rohan for hi-IN, shubh for en-IN) & raises SarvamTTSQuotaError on 402/429.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x01\x00" * 24000)
    b64_wav = base64.b64encode(buf.getvalue()).decode("ascii")
    valid_bytes = buf.getvalue()

    seen_reqs: list[dict] = []

    class FakeResp:
        def __init__(self, code, data, text=""):
            self.status_code = code
            self._data = data
            self.text = text

        def json(self):
            return self._data

    def fake_post(url, *, headers, json, timeout):
        seen_reqs.append({"url": url, "headers": headers, "json": json})
        return FakeResp(200, {"audios": [b64_wav]})

    def copy_wav_ffmpeg(args, **kw):
        out_file = Path(args[-1])
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(valid_bytes)

    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam_secret_key")
    monkeypatch.setattr(voiceover.httpx, "post", fake_post)
    monkeypatch.setattr(voiceover, "run_ffmpeg", copy_wav_ffmpeg)
    dur_hi = voiceover.synthesize_wav("@MrBeast का नया चैलेंज!", tmp_path / "hi.wav", language="hi")
    dur_en = voiceover.synthesize_wav("Crazy finish!", tmp_path / "en.wav", language="en")
    assert dur_hi and dur_hi > 0.4 and dur_en and dur_en > 0.4
    assert seen_reqs[0]["json"]["model"] == "bulbul:v3"
    assert seen_reqs[0]["json"]["target_language_code"] == "hi-IN" and seen_reqs[0]["json"]["speaker"] == "rohan"
    assert seen_reqs[0]["json"]["text"] == "Mr. Beast का नया चैलेंज!"
    assert seen_reqs[1]["json"]["target_language_code"] == "en-IN" and seen_reqs[1]["json"]["speaker"] == "shubh"

    alerts: list[str] = []
    monkeypatch.setattr(voiceover, "_last_quota_alert_at", 0.0)
    monkeypatch.setattr(voiceover.notify, "send", lambda msg, **kw: alerts.append(msg) or True)
    monkeypatch.setattr(voiceover.httpx, "post", lambda *a, **kw: FakeResp(402, {}, "insufficient credits"))
    with pytest.raises(voiceover.SarvamTTSQuotaError):
        voiceover.synthesize_wav("Test", tmp_path / "q.wav", language="hi")
    assert len(alerts) == 1 and "sarvam_secret_key" not in alerts[0]

    # render_compilation propagates SarvamTTSQuotaError rather than shipping voiceless video.
    monkeypatch.setattr(lf_render, "probe", lambda p: MediaInfo(100.0 if Path(p).name == "src.mp4" else 15.0, 1920, 1080, 30.0, False))
    monkeypatch.setattr(lf_render, "_probe_duration", lambda p, f: f)
    monkeypatch.setattr(lf_render, "run_ffmpeg", lambda args, **kw: Path(args[-1]).write_bytes(b"X" * 2000))
    with pytest.raises(voiceover.SarvamTTSQuotaError):
        lf_render.render_compilation(plan, str(tmp_path / "quota_abort.mp4"), brand_name="@Brand", include_end_screen=False, include_voiceover=True)

    # 4. Full render_compilation with mocked voiceover uses integrated cold-open hook & live-motion bridges (zero frozen intro card!).
    synthesized: list[str] = []

    def fake_synth(text, out_wav, **kw):
        synthesized.append(text)
        Path(out_wav).write_bytes(valid_bytes)
        return 2.4

    monkeypatch.setattr(voiceover, "synthesize_wav", fake_synth)
    bridges_rendered: list[str] = []
    monkeypatch.setattr(
        voiceover,
        "render_narrator_bridge",
        lambda wav, bg, out, td, *, badge_text, **kw: bridges_rendered.append(badge_text) or 2.5,
    )

    res = lf_render.render_compilation(plan, str(tmp_path / "vo_out.mp4"), brand_name="@Brand", include_end_screen=True, include_voiceover=True)
    assert len(bridges_rendered) == 2
    assert bridges_rendered[0].startswith("#2") and bridges_rendered[1].startswith("#1")
    assert len(synthesized) == 4  # hook + 2 bridges + outro
    assert [lbl.split()[0] for _, lbl in res.chapters] == ["Intro", "#2", "#1"]

    # Clean fallback when SARVAM_API_KEY absent / synthesize_wav returns None.
    monkeypatch.setattr(voiceover, "synthesize_wav", lambda *a, **k: None)
    bridges_rendered.clear()
    res_fb = lf_render.render_compilation(plan, str(tmp_path / "fb_out.mp4"), brand_name="@Brand", include_end_screen=False, include_voiceover=True)
    assert bridges_rendered == []
    assert [lbl.split()[0] for _, lbl in res_fb.chapters] == ["Intro", "#2", "#1"]


def test_enqueue_long_job_ambient_presets_and_duration_bounds(tmp_path):
    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {"slug": "indianvlogs", "name": "Mr Indian Vlogs", "long_uploads_per_day": 1},
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
                        "generate_while_paused": True,
                    },
                },
            ]
        }),
        encoding="utf-8",
    )
    with session_scope() as db:
        channels.sync_channels(db)

    j_comp = long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target_minutes=45)
    j_amb_default = long_job.enqueue_long_job("ambient", format_kind="best_of")
    j_amb_alias = long_job.enqueue_long_job("ambient", format_kind="ambient", target_minutes=200)
    j_amb_min = long_job.enqueue_long_job("ambient", format_kind="rain_focus", target_minutes=0)
    j_amb_ok = long_job.enqueue_long_job("ambient", format_kind="deep_study", target_minutes=45)

    with session_scope() as db:
        assert db.get(LongJob, j_comp).target_minutes == 10
        assert db.get(LongJob, j_amb_default).format == "lofi_chill"
        assert db.get(LongJob, j_amb_default).target_minutes == 30
        assert db.get(LongJob, j_amb_alias).format == "lofi_chill"
        assert db.get(LongJob, j_amb_alias).target_minutes == 120
        assert db.get(LongJob, j_amb_min).target_minutes == 1
        assert db.get(LongJob, j_amb_ok).format == "deep_study"
        assert db.get(LongJob, j_amb_ok).target_minutes == 45

    with pytest.raises(ValueError):
        long_job.enqueue_long_job("ambient", format_kind="invalid_preset")


def test_procedural_long_supply_builds_pending_while_disabled_and_autopilot_triple_lock(tmp_path, monkeypatch):
    from app.longform import ambient

    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {
                    "slug": "ambient",
                    "name": "Midnight Rain & Lofi",
                    "brand_name": "@MidnightRainLofi",
                    "client_secrets": str(tmp_path / "missing_cs.json"),
                    "token_path": str(tmp_path / "missing_tok.json"),
                    "uploads_per_day": 0,
                    "long_uploads_per_day": 3,
                    "long_auto_publish": False,
                    "enabled": False,
                    "feeds": [],
                    "meta": {
                        "mode": "procedural_ambient",
                        "presets": ["lofi_chill", "rain_focus", "night_ambient", "deep_study"],
                        "target_minutes": 30,
                        "generate_while_paused": True,
                    },
                }
            ]
        }),
        encoding="utf-8",
    )
    with session_scope() as db:
        channels.sync_channels(db)
    assert safety.pause("ambient") is True

    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    monkeypatch.setattr(long_job.notify, "send_long_ready", lambda *a, **kw: True)

    def fake_gen(preset, output_path, **kw):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"MP4" * 200)
        tdir = Path(kw["thumbs_dir"])
        tdir.mkdir(parents=True, exist_ok=True)
        hero = tdir / "hero_raw.jpg"
        hero.write_bytes(b"\xff\xd8\xff\xe0H")
        thumbs = []
        for i in (1, 2, 3):
            tp = tdir / f"thumb_v{i}.jpg"
            tp.write_bytes(b"\xff\xd8\xff\xe0T")
            thumbs.append({"id": f"v{i}", "label": f"V{i}", "path": str(tp)})
        return ambient.AmbientResult(
            output_path=str(out),
            preset=preset,
            duration=1800.0,
            width=1920,
            height=1080,
            fps=30,
            title=f"Title {preset}",
            description="100% original procedural audio.",
            tags=("lofi", "ambient"),
            chapters=((0.0, "Intro"), (300.0, "Drift")),
            chapter_text="0:00 Intro\n5:00 Drift",
            thumbnails=tuple(thumbs),
            hero_frame=str(hero),
            thumbnail_prompt="Cozy rain window",
            language="en",
            theme=kw.get("theme") or "",
            part_number=int(kw.get("part_number") or 1),
        )

    monkeypatch.setattr(ambient, "generate_ambient_video", fake_gen)

    # Supply queues 3 rotating presets into PENDING even while disabled + paused + unlinked
    queued_formats: list[str] = []
    for _ in range(3):
        sres = long_job.long_supply_job()
        assert sres["ambient"]["queued"] is True
        queued_formats.append(sres["ambient"]["format"])
        rres = long_job.run_next_long_job()
        assert rres["ok"] is True
    assert queued_formats == ["lofi_chill", "rain_focus", "night_ambient"]

    # 4th call blocked by stock cap / daily cap of 3
    s4 = long_job.long_supply_job()
    assert s4["ambient"]["queued"] is False

    with session_scope() as db:
        vids = db.execute(select(Video).where(Video.source == "longform")).scalars().all()
        assert len(vids) == 3
        assert all(v.status == VideoStatus.PENDING for v in vids)
        assert all(v.meta.get("procedural_ambient") is True for v in vids)
        assert vids[0].description.count("Chapters:") == 1

    # Triple-lock autopilot checks:
    # 1. enable_publish=False
    monkeypatch.setattr(settings, "enable_publish", False)
    assert long_job.long_autopilot_job()["skipped"] == "publishing disabled"

    # 2. disabled channel (active_channels excludes enabled=False)
    monkeypatch.setattr(settings, "enable_publish", True)
    assert long_job.long_autopilot_job()["skipped"] == "no channels configured for long-form"

    # 3. enabled=True but paused -> skipped
    with session_scope() as db:
        amb_ch = channels.channel_by_slug(db, "ambient")
        amb_ch.enabled = True
    assert long_job.long_autopilot_job()["ambient"]["skipped"] == "paused or held"

    # 4. unpaused, use_mock=False, missing credentials -> skipped
    assert safety.resume("ambient") is True
    monkeypatch.setattr(settings, "use_mock", False)
    assert long_job.long_autopilot_job()["ambient"]["skipped"] == "credentials missing"

    # 5. creds files exist, youtube_channel_id empty -> skipped
    (tmp_path / "missing_cs.json").write_text("{}", encoding="utf-8")
    (tmp_path / "missing_tok.json").write_text("{}", encoding="utf-8")
    assert long_job.long_autopilot_job()["ambient"]["skipped"] == "channel not linked"

    # 6. linked, but long_auto_publish=False and all videos PENDING -> none_ready
    with session_scope() as db:
        channels.channel_by_slug(db, "ambient").youtube_channel_id = "UC_AMBIENT_TEST"
    assert long_job.long_autopilot_job()["ambient"]["skipped"] == "none_ready"


def test_long_panel_api_exposes_ambient_presets_and_extended_minutes(tmp_path, monkeypatch):
    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {"slug": "indianvlogs", "name": "Mr Indian Vlogs", "long_uploads_per_day": 1},
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
                        "generate_while_paused": True,
                    },
                },
            ]
        }),
        encoding="utf-8",
    )
    with session_scope() as db:
        channels.sync_channels(db)
        amb = channels.channel_by_slug(db, "ambient")
        mp4 = tmp_path / "amb.mp4"
        mp4.write_bytes(b"MP4")
        db.add(Video(
            source="longform", source_id="amb1", source_url="", title="Rainy Focus",
            description="100% original", transformed_path=str(mp4), status=VideoStatus.PENDING,
            channel_id=amb.id, width=1920, height=1080, duration_sec=1800,
            meta={"long_form": True, "procedural_ambient": True, "long_format": "rain_focus"},
        ))

    monkeypatch.setattr(long_job, "start_long_worker", lambda: True)
    c = _signed_client()

    r = c.post("/api/long/build", json={"channel_slug": "ambient", "format": "rain_focus", "target_minutes": 45})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    with session_scope() as db:
        job_row = db.get(LongJob, jid)
        assert job_row.format == "rain_focus" and job_row.target_minutes == 45

    panel = c.get("/api/long/videos").json()
    amb_info = next(x for x in panel["channels"] if x["slug"] == "ambient")
    assert amb_info["procedural"] is True
    assert amb_info["default_target_minutes"] == 30
    assert "rain_focus" in amb_info["presets"]
    assert panel["pending"][0]["procedural"] is True

    ch_rows = c.get("/api/channels").json()["channels"]
    amb_ch = next(x for x in ch_rows if x["slug"] == "ambient")
    assert amb_ch["procedural"] is True and amb_ch["mode"] == "procedural_ambient"


def test_channel3_portrait_and_track_upload_endpoints_require_guard_and_validate(tmp_path, monkeypatch):
    import io
    import wave
    from PIL import Image
    from app.longform import ambient

    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {
                    "slug": "ambient",
                    "name": "After Midnight POV",
                    "uploads_per_day": 0,
                    "long_uploads_per_day": 3,
                    "long_auto_publish": False,
                    "enabled": False,
                    "feeds": [],
                    "meta": {"mode": "procedural_ambient", "presets": ["lofi_chill"], "target_minutes": 30},
                }
            ]
        }),
        encoding="utf-8",
    )
    with session_scope() as db:
        channels.sync_channels(db)
        amb = channels.channel_by_slug(db, "ambient")
        mp4 = tmp_path / "content" / "longform" / "ambient" / "p1.mp4"
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"MP4")
        vid = Video(
            source="longform", source_id="amb_port1", source_url="", title="i wish i could meet you one last time",
            description="100% original", transformed_path=str(mp4), status=VideoStatus.PENDING,
            channel_id=amb.id, width=1920, height=1080, duration_sec=60,
            meta={"long_form": True, "procedural_ambient": True, "ambient_preset": "lofi_chill", "long_format": "lofi_chill"},
        )
        db.add(vid)
        db.flush()
        video_id = vid.id

    anon = TestClient(review_app)
    assert anon.post(f"/api/long/{video_id}/portrait", content=b"123").status_code == 401
    assert anon.post("/api/long/ambient/tracks?filename=a.wav", content=b"123").status_code == 401

    c = _signed_client()

    tiny_buf = io.BytesIO()
    Image.new("RGB", (200, 100), (10, 10, 10)).save(tiny_buf, format="JPEG")
    assert c.post(f"/api/long/{video_id}/portrait", content=tiny_buf.getvalue()).status_code == 400

    def fake_gen_amb(preset="lofi_chill", output_path="", *, portrait_path=None, thumbs_dir=None, **kw):
        Path(output_path).write_bytes(b"ANIMATED_MP4")
        td = Path(thumbs_dir)
        td.mkdir(parents=True, exist_ok=True)
        t1 = td / "thumb_v1.jpg"
        t1.write_bytes(b"JPG")
        return ambient.AmbientResult(
            output_path=str(output_path), preset=preset, duration=60.0, width=1920, height=1080, fps=30,
            title="i wish i could meet you one last time", description="100% original", tags=("late night pov",),
            chapters=((0.0, "Fairy Light Glow"),), chapter_text="0:00 Fairy Light Glow",
            thumbnails=({"id": "v1", "label": "Intimate POV", "path": str(t1)},),
            hero_frame=str(portrait_path or t1), thumbnail_prompt="Cinematic 16:9 night",
            portrait_path=str(portrait_path or ""),
        )

    monkeypatch.setattr(ambient, "generate_ambient_video", fake_gen_amb)
    ok_buf = io.BytesIO()
    Image.new("RGB", (1280, 720), (40, 20, 25)).save(ok_buf, format="JPEG")
    rp = c.post(f"/api/long/{video_id}/portrait", content=ok_buf.getvalue())
    assert rp.status_code == 200 and rp.json()["ok"] is True
    assert Path(rp.json()["portrait_path"]).is_file()

    assert c.post("/api/long/ambient/tracks?filename=bad.exe", content=b"MZ").status_code == 400

    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(b"\x01\x00" * 48000 * 2)  # 1.0s stereo 48kHz
    rt = c.post("/api/long/ambient/tracks?filename=Midnight%20Echo.wav&preset=lofi_chill", content=wav_buf.getvalue())
    assert rt.status_code == 200 and rt.json()["tracks_count"] == 1

    panel = c.get("/api/long/videos").json()
    assert panel["ambient_stats"]["tracks_count"] == 1
    assert panel["ambient_stats"]["portraits_count"] >= 1


def test_long_unapprove_clear_finished_and_block_procedural_clip_panel(tmp_path, monkeypatch):
    Path(settings.channels_file).write_text(
        json.dumps({
            "channels": [
                {"slug": "indianvlogs", "name": "Mr Indian Vlogs", "long_uploads_per_day": 1},
                {
                    "slug": "ambient",
                    "name": "After Midnight POV",
                    "uploads_per_day": 0,
                    "long_uploads_per_day": 3,
                    "enabled": False,
                    "feeds": [],
                    "meta": {"mode": "procedural_ambient", "presets": ["lofi_chill"]},
                },
            ]
        }),
        encoding="utf-8",
    )
    with session_scope() as db:
        channels.sync_channels(db)
        hi = channels.channel_by_slug(db, "indianvlogs")
        mp4 = tmp_path / "comp.mp4"
        mp4.write_bytes(b"MP4DATA")
        v_ready = Video(
            source="longform", source_id="lr1", source_url="https://youtu.be/lr1",
            title="Ready Long", description="Desc", transformed_path=str(mp4),
            status=VideoStatus.PENDING, channel_id=hi.id, width=1920, height=1080,
            meta={"long_form": True},
        )
        v_pub = Video(
            source="longform", source_id="lp1", source_url="https://youtu.be/lp1",
            title="Pub Long", description="Desc", transformed_path=str(mp4),
            status=VideoStatus.PUBLISHED, youtube_video_id="YTPUB1", channel_id=hi.id,
            meta={"long_form": True},
        )
        j_done = LongJob(channel_id=hi.id, format="best_of", status="done")
        j_fail = LongJob(channel_id=hi.id, format="vs", status="failed", error="boom")
        j_wait = LongJob(channel_id=hi.id, format="theme", status="queued")
        db.add_all([v_ready, v_pub, j_done, j_fail, j_wait])
        db.flush()
        vid_ready, vid_pub = v_ready.id, v_pub.id
        jid_done, jid_fail, jid_wait = j_done.id, j_fail.id, j_wait.id

    anon = TestClient(review_app)
    assert anon.post(f"/api/long/{vid_ready}/unapprove").status_code == 401
    assert anon.post("/api/long/clear-finished").status_code == 401

    c = _signed_client()

    # Procedural ambient channels cannot receive Shorts clips via /api/clip.
    bad_clip = c.post("/api/clip", json={"source": "https://youtu.be/dQw4w9WgXcQ", "channel_slug": "ambient"})
    assert bad_clip.status_code == 400 and "procedural" in bad_clip.json()["detail"].lower()

    # Approve -> TRANSFORMED, then unapprove -> back to PENDING and owner_approved cleared.
    assert c.post(f"/api/long/{vid_ready}/approve", json={}).status_code == 200
    unapp = c.post(f"/api/long/{vid_ready}/unapprove")
    assert unapp.status_code == 200 and unapp.json() == {"id": vid_ready, "status": "pending"}
    with session_scope() as db:
        row = db.get(Video, vid_ready)
        assert row.status == VideoStatus.PENDING
        assert "owner_approved" not in (row.meta or {})
    assert c.post(f"/api/long/{vid_ready}/unapprove").status_code == 409
    assert c.post(f"/api/long/{vid_pub}/unapprove").status_code == 409

    # Clear finished long jobs hides done/failed but preserves rows & leaves queued intact.
    cleared = c.post("/api/long/clear-finished")
    assert cleared.status_code == 200
    body = cleared.json()
    assert body["cleared"] == 2 and set(body["ids"]) == {jid_done, jid_fail}
    visible_jobs = c.get("/api/long/jobs").json()["jobs"]
    assert [j["id"] for j in visible_jobs] == [jid_wait]
    with session_scope() as db:
        assert (db.get(LongJob, jid_done).result or {}).get("dismissed") is True
        assert (db.get(LongJob, jid_fail).result or {}).get("dismissed") is True
        assert db.get(LongJob, jid_wait).status == "queued"


def test_publish_task_uses_music_category_and_ambient_comment_for_procedural_ambient(tmp_path, monkeypatch):
    from tasks import pipeline
    from app.publishers.base import PublishResult
    from app.growth import titles

    mp4 = tmp_path / "amb_pub.mp4"
    mp4.write_bytes(b"MP4" * 200)
    with session_scope() as db:
        channels.sync_channels(db)
        v = Video(
            source="longform",
            source_id="amb_pub_1",
            source_url="",
            title="i wish you stayed",
            description="100% original procedural music",
            transformed_path=str(mp4),
            status=VideoStatus.TRANSFORMED,
            width=1920,
            height=1080,
            duration_sec=1800,
            meta={"long_form": True, "procedural_ambient": True},
        )
        db.add(v)
        db.flush()
        vid = v.id

    uploads: list[dict] = []
    comments: list[tuple[str, str]] = []

    class FakePub:
        def upload(self, **kw):
            uploads.append(kw)
            return PublishResult(video_id="AMB_YT_99", status="uploaded", raw={})

        def set_thumbnail(self, video_id, path):
            return False

        def post_comment(self, video_id, text):
            comments.append((video_id, text))
            return "c1"

    monkeypatch.setattr(pipeline, "_publisher_for", lambda cid: FakePub())
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "youtube_auto_comment", True)
    monkeypatch.setattr(titles.ai, "available", lambda: False)

    res = pipeline.publish_task(vid)
    assert res["ok"] is True and res["youtube_id"] == "AMB_YT_99"
    assert uploads[0]["category"] == 10
    assert len(comments) == 1 and "#1" not in comments[0][1]


def test_long_panel_data_exposes_ambient_presets_matching_ui_labels():
    from app.longform import ambient
    from app.review_ui import PAGE

    data = long_job.long_panel_data()
    expected = ambient.list_presets()
    assert data.get("ambient_presets") == expected
    for preset in expected:
        assert preset["label"] in PAGE
    assert "Bedroom Glow · One Last Time" not in PAGE
    assert "ambientPresetLabels" in PAGE


def test_long_job_recover_orphans_handles_interrupted_and_scrubs_errors(tmp_path, monkeypatch):
    from app.clipper.media import Interrupted
    from app.longform import ambient

    with session_scope() as db:
        channels._ensure_seeded(db)
        ch = channels.default_channel(db)
        orphan = LongJob(channel_id=ch.id, format="best_of", target_minutes=8, status="running", stage="rendering", attempts=1)
        maxed = LongJob(channel_id=ch.id, format="best_of", target_minutes=8, status="running", stage="rendering", attempts=3)
        done_job = LongJob(channel_id=ch.id, format="best_of", target_minutes=8, status="done", stage="ready", attempts=1)
        failed_job = LongJob(channel_id=ch.id, format="best_of", target_minutes=8, status="failed", error="bad", attempts=1)
        db.add_all([orphan, maxed, done_job, failed_job])
        db.flush()
        orphan_id, maxed_id, done_id, failed_id = orphan.id, maxed.id, done_job.id, failed_job.id

    recovered = long_job.recover_interrupted_long_jobs()
    assert recovered == 2
    with session_scope() as db:
        r_orphan = db.get(LongJob, orphan_id)
        r_maxed = db.get(LongJob, maxed_id)
        assert r_orphan.status == "queued" and r_orphan.stage == "" and r_orphan.started_at is None
        assert r_maxed.status == "failed" and r_maxed.error == "interrupted too many times"
        assert db.get(LongJob, done_id).status == "done"
        assert db.get(LongJob, failed_id).status == "failed"

    # Interrupted during run_next_long_job re-queues when attempts < 3 rather than failing permanently.
    with session_scope() as db:
        j_int = db.get(LongJob, orphan_id)
        j_int.format = "lofi_chill"
        j_int.target_minutes = 30
    monkeypatch.setattr(ambient, "list_ambient_portraits", lambda: [])
    monkeypatch.setattr(ambient, "generate_ambient_video", lambda **kw: (_ for _ in ()).throw(Interrupted("shutdown")))
    res_int = long_job.run_next_long_job()
    assert res_int and res_int.get("interrupted") is True
    with session_scope() as db:
        row = db.get(LongJob, orphan_id)
        assert row.status == "queued" and row.stage == "" and row.started_at is None

    # Exception error containing configured secret is scrubbed before persisting to LongJob.error.
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_sarvam_secret_wave10")
    monkeypatch.setattr(
        ambient,
        "generate_ambient_video",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("TTS fail with key=sk_sarvam_secret_wave10")),
    )
    res_err = long_job.run_next_long_job()
    assert res_err["ok"] is False
    assert "sk_sarvam_secret_wave10" not in res_err["error"]
    with session_scope() as db:
        row = db.get(LongJob, orphan_id)
        assert row.status == "failed"
        assert "sk_sarvam_secret_wave10" not in (row.error or "")


def test_wave18_long_build_maps_lookup_error_to_400_and_ui_preserves_state_and_uploads(monkeypatch):
    from app.review_ui import PAGE

    monkeypatch.setattr(
        long_job,
        "enqueue_long_job",
        lambda *a, **kw: (_ for _ in ()).throw(LookupError("missing channel")),
    )
    c = _signed_client()
    resp = c.post("/api/long/build", json={"channel_slug": "missing", "format": "best_of"})
    assert resp.status_code == 400
    assert "missing channel" in resp.json()["detail"]

    # Long-form state preservation & dirty tracking
    assert "const longEdits={}" in PAGE
    assert "Object.keys(longEdits).length>0" in PAGE
    assert "card.dataset.longId=item.id" in PAGE
    assert "thumbDet.dataset.k='thumb'" in PAGE and "metaDet.dataset.k='meta'" in PAGE
    assert "openDet" in PAGE and "vidState" in PAGE

    # Long-tab XHR api.upload limits (10 MB thumb/portrait, 30 MB track) & drag-drop wiring
    assert "30*1024*1024" in PAGE
    assert "10*1024*1024" in PAGE
    assert "wireDrop(ambientTrackWrap" in PAGE
    assert "wireDrop(upBtn" in PAGE
    assert "wireDrop(portBtn" in PAGE


def test_wave18_longcard_metadata_save_syncs_item_and_vstage_stops_propagation(tmp_path):
    import re
    import shutil
    import subprocess
    from app.review_ui import PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    script_match = re.search(r"<script>([\s\S]*?)</script>", PAGE)
    assert script_match is not None

    harness = (
        r"""
const vm = require('vm');
const assert = require('assert');

const byId = new Map();
const docListeners = [];
function matchOne(node, sel) {
  sel = sel.trim();
  if (!sel) return false;
  if (sel.startsWith('#')) return node.id === sel.slice(1);
  const attrM = sel.match(/^([a-z0-9_-]*)\[([a-z0-9_-]+)(?:=([^\]]+))?\]$/i);
  if (attrM) {
    if (attrM[1] && node.tagName !== attrM[1].toUpperCase()) return false;
    const val = node.getAttribute(attrM[2]);
    if (val === null) return false;
    if (attrM[3] !== undefined) return val === attrM[3].replace(/^['"]|['"]$/g, '');
    return true;
  }
  const parts = sel.split('.');
  const tag = parts[0];
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  for (let i = 1; i < parts.length; i++) if (!node.classList.contains(parts[i])) return false;
  return true;
}
function matches(node, sel) { return sel.split(',').some(s => matchOne(node, s)); }
class Elem {
  constructor(tag, id) {
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this._classes = new Set();
    this._listeners = [];
    this.childNodes = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    this._text = '';
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.paused = false;
    this.ended = false;
    this.currentTime = 0;
    this._id = '';
    if (id) this.id = id;
  }
  set id(v) { this._id = v; if (v) byId.set(v, this); this.attributes.set('id', v); }
  get id() { return this._id; }
  set className(v) { this._classes = new Set(String(v || '').split(/\s+/).filter(Boolean)); }
  get className() { return [...this._classes].join(' '); }
  set tabIndex(v) { this.setAttribute('tabindex', String(v)); }
  get tabIndex() { return Number(this.getAttribute('tabindex') || -1); }
  set autoplay(v) { this._autoplay = !!v; if (v) this.paused = false; }
  get autoplay() { return !!this._autoplay; }
  set textContent(v) { this._text = v == null ? '' : String(v); this.childNodes = []; }
  get textContent() { return this._text + this.childNodes.map(c => c.textContent || '').join(''); }
  get children() { return this.childNodes.filter(c => c instanceof Elem); }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const i = this.parentNode.childNodes.indexOf(this);
    return i >= 0 ? (this.parentNode.childNodes[i + 1] || null) : null;
  }
  get offsetParent() { return this.hidden ? null : {}; }
  get classList() {
    const s = this._classes;
    return {
      add: (...a) => a.forEach(x => s.add(x)),
      remove: (...a) => a.forEach(x => s.delete(x)),
      toggle: (x, f) => (f === undefined ? (s.has(x) ? (s.delete(x), false) : (s.add(x), true)) : (f ? (s.add(x), true) : (s.delete(x), false))),
      contains: x => s.has(x),
    };
  }
  setAttribute(k, v) {
    const s = String(v);
    this.attributes.set(k, s);
    if (k === 'id') { this._id = s; byId.set(s, this); }
    if (k.startsWith('data-')) {
      const ck = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[ck] = s;
    }
  }
  getAttribute(k) {
    if (k.startsWith('data-')) {
      const ck = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      if (ck in this.dataset) return this.dataset[ck];
    }
    return this.attributes.has(k) ? this.attributes.get(k) : null;
  }
  removeAttribute(k) { this.attributes.delete(k); }
  hasAttribute(k) { return this.attributes.has(k); }
  _attach(n) {
    if (typeof n === 'string') n = { textContent: n, nodeType: 3, parentNode: null };
    if (n.parentNode) {
      const i = n.parentNode.childNodes.indexOf(n);
      if (i >= 0) n.parentNode.childNodes.splice(i, 1);
    }
    n.parentNode = this;
    return n;
  }
  append(...nodes) { for (const n of nodes) this.childNodes.push(this._attach(n)); }
  appendChild(n) { this.append(n); return n; }
  replaceChildren(...nodes) { this._text = ''; this.childNodes = []; this.append(...nodes); }
  remove() {
    if (!this.parentNode) return;
    const i = this.parentNode.childNodes.indexOf(this);
    if (i >= 0) this.parentNode.childNodes.splice(i, 1);
    this.parentNode = null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = n => { for (const c of n.children || []) { if (matches(c, sel)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) {
    let cur = this;
    while (cur && cur instanceof Elem) { if (matches(cur, sel)) return cur; cur = cur.parentNode; }
    return null;
  }
  addEventListener(type, fn, cap) { this._listeners.push({ type, fn, cap: !!cap }); }
  dispatchEvent(evt) {
    evt.target = evt.target || this;
    let stopped = false, defPrev = false;
    evt.stopPropagation = () => { stopped = true; };
    evt.preventDefault = () => { defPrev = true; evt.defaultPrevented = true; };
    const chain = [];
    let cur = this;
    while (cur) { chain.push(cur); cur = cur.parentNode; }
    for (const l of docListeners.filter(x => x.cap && x.type === evt.type)) l.fn(evt);
    for (let i = 0; i < chain.length && !stopped; i++) {
      const n = chain[i];
      if (n === this && evt.type === 'click' && typeof n.onclick === 'function') n.onclick(evt);
      for (const l of (n._listeners || [])) if (!l.cap && l.type === evt.type) l.fn(evt);
      if (!evt.bubbles) break;
    }
    if (!stopped && evt.bubbles) for (const l of docListeners.filter(x => !x.cap && x.type === evt.type)) l.fn(evt);
    return !defPrev;
  }
  click() { if (typeof this.onclick === 'function') this.onclick({ target: this, preventDefault() {}, stopPropagation() {} }); }
  focus() { doc.activeElement = this; }
  blur() { if (doc.activeElement === this) doc.activeElement = doc.body; }
  select() {}
  scrollIntoView() {}
  play() { this.paused = false; }
  pause() { this.paused = true; }
  load() {}
}
const body = new Elem('body');
body.append(new Elem('nav', 'nav'));
for (const pid of ['review', 'clip', 'long', 'channels', 'status', 'report', 'controls']) {
  const sec = new Elem('section', 'tab-' + pid);
  sec.className = 'tabpane';
  body.append(sec);
}
const parentMap = {
  'rv-stats': 'tab-review', 'banner': 'tab-review', 'strip': 'tab-review', 'rv-modes': 'tab-review',
  'rv-tools': 'tab-review', 'rv-search': 'tab-review', 'rv-sort': 'tab-review', 'rv-speed': 'tab-review',
  'chips': 'tab-review', 'rv-flagged': 'tab-review', 'list': 'tab-review', 'posted': 'tab-review',
  'clip-form': 'tab-clip', 'clip-clear-finished': 'tab-clip', 'clip-jobs': 'tab-clip',
  'long-build-form': 'tab-long', 'long-clear-finished': 'tab-long', 'long-jobs': 'tab-long',
  'long-tts': 'tab-long', 'long-modes': 'tab-long', 'long-chips': 'tab-long', 'long-ab': 'tab-long', 'long-list': 'tab-long',
  'ch-list': 'tab-channels', 'add-form': 'tab-channels', 'import-form': 'tab-channels',
  'dc-head': 'tab-status', 'dc-body': 'tab-status', 'rp-pick': 'tab-report', 'rp-body': 'tab-report', 'ct-body': 'tab-controls'
};
for (const id of ['title', 'conn', 'kbd-help', 'kbd-modal', 'kbd-close', 'snack', ...Object.keys(parentMap)]) {
  const tag = id === 'rv-search' ? 'input' : (id.endsWith('-sort') || id.endsWith('-speed') || id.endsWith('-finished') || id === 'kbd-help' || id === 'kbd-close' ? 'button' : 'div');
  const el = new Elem(tag, id);
  (byId.get(parentMap[id]) || body).append(el);
}
const doc = {
  body, activeElement: body,
  getElementById: id => byId.get(id) || null,
  createElement: tag => new Elem(tag),
  createTextNode: txt => ({ textContent: String(txt), nodeType: 3, parentNode: null }),
  querySelectorAll: sel => body.querySelectorAll(sel),
  querySelector: sel => body.querySelector(sel),
  addEventListener: (type, fn, cap) => docListeners.push({ type, fn, cap: !!cap }),
};
class FakeResponse {
  constructor(status, body) { this.status = status; this.ok = status >= 200 && status < 300; this._body = body; }
  async json() { return this._body; }
}
const postedMeta = [];
const longPayload = {
  channels: [{ slug: 'ambient', name: 'After Midnight POV', procedural: true, presets: ['lofi_chill'], default_target_minutes: 30 }],
  ambient_presets: [{ id: 'lofi_chill', label: 'Lofi Chill' }],
  ambient_stats: { tracks_count: 0, portraits_count: 0 },
  ambient_ab: null,
  jobs: [],
  pending: [{
    id: 'lg1', channel_slug: 'ambient', format: 'lofi_chill', status: 'pending', procedural: true,
    title: 'Orig Long Title', description: 'Orig Desc', tags: ['orig'],
    duration_sec: 1800, width: 1920, height: 1080, video_url: '/api/long/lg1/video', thumbnails: [], chapters: []
  }],
  ready: [], posted: [], tts_ready: false
};
const ctx = {
  document: doc, window: {}, navigator: { clipboard: { writeText: async () => {} } },
  localStorage: { getItem: () => null, setItem: () => {} },
  Response: FakeResponse, TextEncoder, URL, console,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  fetch: async (url, opts) => {
    if (url.startsWith('/api/queue')) return new FakeResponse(200, { waiting: 0, approved: 0, rejected: 0, daily_target: 3, scraped: [], posted: [], channels: [], clips: [] });
    if (url === '/api/long/videos') return new FakeResponse(200, longPayload);
    if (url === '/api/long/jobs') return new FakeResponse(200, { jobs: [] });
    if (url === '/api/long/lg1/metadata') { postedMeta.push(JSON.parse(opts.body)); return new FakeResponse(200, { ok: true }); }
    return new FakeResponse(200, {});
  }
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext("""
        + json.dumps(script_match.group(1))
        + """, ctx);

(async () => {
  const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
  ctx.show('long');
  await flush();

  // 1. Save metadata button syncs item.title, item.description, item.tags before deleting longEdits
  const textInputs = cd => cd.querySelectorAll('input').filter(i => i.type !== 'file');
  let card = byId.get('long-list').children[0];
  let inputs = textInputs(card);
  let titleInp = inputs[0];
  let tagsInp = inputs[1];
  let descInp = card.querySelector('textarea');
  titleInp.value = 'Updated Title';
  titleInp.dispatchEvent({ type: 'input', bubbles: true });
  descInp.value = 'Updated Desc';
  descInp.dispatchEvent({ type: 'input', bubbles: true });
  tagsInp.value = 'alpha, beta';
  tagsInp.dispatchEvent({ type: 'input', bubbles: true });
  const saveBtn = card.querySelectorAll('button').find(b => b.textContent === 'Save metadata');
  assert.ok(saveBtn, 'Save metadata button must exist');
  saveBtn.click();
  await flush();
  assert.strictEqual(postedMeta.length, 1);

  ctx.renderLongList();
  card = byId.get('long-list').children[0];
  inputs = textInputs(card);
  assert.strictEqual(inputs[0].value, 'Updated Title', 'Title must persist across renderLongList after Save metadata');
  assert.strictEqual(card.querySelector('textarea').value, 'Updated Desc', 'Description must persist across renderLongList after Save metadata');
  assert.strictEqual(inputs[1].value, 'alpha, beta', 'Tags must persist across renderLongList after Save metadata');

  // 2. titleInp Enter-to-save syncs item.title, item.description, item.tags before deleting longEdits
  titleInp = inputs[0];
  tagsInp = inputs[1];
  descInp = card.querySelector('textarea');
  titleInp.value = 'Enter Title';
  titleInp.dispatchEvent({ type: 'input', bubbles: true });
  descInp.value = 'Enter Desc';
  descInp.dispatchEvent({ type: 'input', bubbles: true });
  tagsInp.value = 'gamma, delta';
  tagsInp.dispatchEvent({ type: 'input', bubbles: true });
  titleInp.dispatchEvent({ type: 'keydown', key: 'Enter', bubbles: true });
  await flush();
  assert.strictEqual(postedMeta.length, 2);

  ctx.renderLongList();
  card = byId.get('long-list').children[0];
  inputs = textInputs(card);
  assert.strictEqual(inputs[0].value, 'Enter Title', 'Title must persist across renderLongList after Enter');
  assert.strictEqual(card.querySelector('textarea').value, 'Enter Desc', 'Description must persist across renderLongList after Enter');
  assert.strictEqual(inputs[1].value, 'gamma, delta', 'Tags must persist across renderLongList after Enter');

  // 3. longCard .vstage Space keydown must stopPropagation()
  let docSawSpace = false;
  doc.addEventListener('keydown', e => { if (e.key === ' ') docSawSpace = true; });
  const stage = card.querySelector('.vstage');
  stage.focus();
  stage.dispatchEvent({ type: 'keydown', key: ' ', bubbles: true });
  assert.ok(stage.querySelector('video'), 'Long card video must mount on Space');
  assert.strictEqual(docSawSpace, false, 'Long card .vstage Space keydown must not bubble to document');

  console.log('ALL_OK');
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""
    )
    js_file = tmp_path / "long_meta_vstage_probe.js"
    js_file.write_text(harness, encoding="utf-8")
    proc = subprocess.run([node, str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "ALL_OK" in proc.stdout


def test_long_autopilot_uses_event_channel_for_default_quota(tmp_path, monkeypatch):
    from app import quota

    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "max_uploads_per_day", 1)
    monkeypatch.setattr(settings, "use_mock", True)

    with session_scope() as db:
        channels.sync_channels(db)
        ch_def = channels.channel_by_slug(db, "default")
        ch_ind = channels.channel_by_slug(db, "indianvlogs")
        assert ch_def is not None and ch_ind is not None

        for slug, ch in (("default", ch_def), ("indianvlogs", ch_ind)):
            mp4 = tmp_path / f"{slug}_long.mp4"
            mp4.write_bytes(b"MP4" * 500)
            db.add(Video(
                source="longform",
                source_id=f"lf_q_{slug}",
                source_url="",
                title=f"Ready {slug}",
                status=VideoStatus.TRANSFORMED,
                channel_id=ch.id,
                transformed_path=str(mp4),
                meta={"long_form": True, "long_format": "best_of", "owner_approved": True},
            ))
        # Exhaust legacy/default quota bucket (channel_id=None) used by Shorts/autopilot.
        quota.consume_one(db, channel_id=None)

    res = long_job.long_autopilot_job()
    assert res["default"] == {"skipped": "daily quota exhausted"}
    assert res["indianvlogs"]["ok"] is True


def test_long_supply_job_no_nested_session_scope_and_starts_worker_after_commit(tmp_path, monkeypatch):
    from contextlib import contextmanager

    monkeypatch.setattr(settings, "longform_auto_supply", True)
    (tmp_path / "channels_supply.json").write_text(json.dumps({
        "channels": [
            {"slug": "default", "niche": "comedy", "long_uploads_per_day": 1, "long_auto_publish": False,
             "feeds": ["https://www.youtube.com/@MrBeast"]},
            {"slug": "indianvlogs", "name": "Mr Indian Vlogs", "niche": "hindi", "uploads_per_day": 3,
             "long_uploads_per_day": 1, "long_auto_publish": False, "timezone": "Asia/Kolkata",
             "feeds": ["https://www.youtube.com/@souravjvlogs"]},
        ]
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels_supply.json"))
    with session_scope() as db:
        channels.sync_channels(db)

    orig_scope = long_job.session_scope
    active_depth = 0
    max_depth = 0
    events: list[str] = []

    @contextmanager
    def tracked_scope():
        nonlocal active_depth, max_depth
        active_depth += 1
        max_depth = max(max_depth, active_depth)
        events.append(f"enter:{active_depth}")
        try:
            with orig_scope() as db:
                yield db
            events.append(f"commit:{active_depth}")
        finally:
            active_depth -= 1

    monkeypatch.setattr(long_job, "session_scope", tracked_scope)
    monkeypatch.setattr(long_job, "recover_interrupted_long_jobs", lambda: None)
    monkeypatch.setattr(
        long_job,
        "start_long_worker",
        lambda: (events.append(f"start_worker:{active_depth}") or True),
    )

    res = long_job.long_supply_job()
    assert res["default"]["queued"] is True
    assert res["indianvlogs"]["queued"] is True
    assert max_depth == 1
    assert events.count("start_worker:0") == 1
    assert events[-1] == "start_worker:0"


def test_longform_job_scrubs_secrets_in_notify_recovery_and_portrait_errors(tmp_path, monkeypatch, caplog):
    import io
    import logging
    from PIL import Image, ImageOps
    from tasks import pipeline
    from app.longform import thumbnails as lf_thumbs

    monkeypatch.setattr(settings, "sarvam_api_key", "sk_secret_job_audit")
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "max_uploads_per_day", 5)

    with session_scope() as db:
        channels.sync_channels(db)
        ch = channels.channel_by_slug(db, "indianvlogs")
        assert ch is not None
        mp4 = tmp_path / "ready_scrub.mp4"
        mp4.write_bytes(b"MP4" * 400)
        v = Video(
            source="longform",
            source_id="lf_scrub_pub",
            source_url="",
            title="Scrub Pub",
            status=VideoStatus.TRANSFORMED,
            channel_id=ch.id,
            transformed_path=str(mp4),
            meta={"long_form": True, "long_format": "best_of", "owner_approved": True},
        )
        db.add(v)

    # 1. run_next_long_job: phone alert error + failed job exception scrubbing
    long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs", target_minutes=8)
    seg = planner.SegmentPlan("s.mp4", "https://youtu.be/abc", "abc", "@souravjvlogs", "Sourav", "Vlog 1", 5.0, 45.0, 90.0, "Moment", "hi")
    fake_plan = planner.CompilationPlan(
        format_kind="best_of", channel_slug="indianvlogs", part_number=1, targets=("@souravjvlogs",), theme="",
        title="Best", description="Desc", tags=("vlog",), language="hi", target_seconds=480.0, cold_open=seg, segments=(seg,),
    )
    monkeypatch.setattr(planner, "plan_compilation", lambda **k: fake_plan)
    monkeypatch.setattr(
        lf_render,
        "render_compilation",
        lambda plan, out_path, **k: (
            Path(out_path).parent.mkdir(parents=True, exist_ok=True),
            Path(out_path).write_bytes(b"MP4" * 400),
            lf_render.RenderResult(output_path=out_path, duration=300.0, chapters=((0.0, "Intro"),), chapter_text="0:00 Intro", timestamps_for_thumbs=(5.0,)),
        )[-1],
    )
    monkeypatch.setattr(
        lf_thumbs,
        "generate_thumbnails_with_prompt",
        lambda vp, *, out_dir, **k: (
            Path(out_dir).mkdir(parents=True, exist_ok=True),
            (Path(out_dir) / "hero_raw.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"H" * 50),
            ([{"id": "v1", "label": "V1", "path": str(Path(out_dir) / "hero_raw.jpg")}], "Prompt"),
        )[-1],
    )
    monkeypatch.setattr(long_job.notify, "send_long_ready", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("key=sk_secret_job_audit")))

    with caplog.at_level(logging.WARNING):
        res1 = long_job.run_next_long_job()
        assert res1["ok"] is True

        # Failed job path without configure_logging() must also keep caplog clean
        long_job.enqueue_long_job("indianvlogs", format_kind="best_of", target="@souravjvlogs", target_minutes=8)
        monkeypatch.setattr(planner, "plan_compilation", lambda **k: (_ for _ in ()).throw(RuntimeError("Bearer sk_secret_job_audit")))
        res_fail = long_job.run_next_long_job()
        assert res_fail["ok"] is False
        assert "sk_secret_job_audit" not in res_fail["error"]

        # 2. start_long_worker recovery warning scrubbing
        monkeypatch.setattr(long_job, "recover_interrupted_long_jobs", lambda: (_ for _ in ()).throw(RuntimeError("key=sk_secret_job_audit")))
        class DummyThread:
            def __init__(self, *a, **k):
                pass
            def start(self):
                pass
            def is_alive(self):
                return False
        monkeypatch.setattr(long_job.threading, "Thread", DummyThread)
        long_job._worker_thread = None
        long_job.start_long_worker()

    assert "sk_secret_job_audit" not in caplog.text
    assert "[REDACTED]" in caplog.text

    # 3. re_render_ambient_with_portrait image decode error scrubbing
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (10, 20, 30)).save(buf, format="JPEG")
    monkeypatch.setattr(ImageOps, "exif_transpose", lambda _im: (_ for _ in ()).throw(RuntimeError("key=sk_secret_job_audit")))
    with pytest.raises(ValueError) as exc_info:
        long_job.re_render_ambient_with_portrait("any-id", buf.getvalue())
    assert "sk_secret_job_audit" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)

    # 4. long_autopilot_job scrubs error/skipped from publish_task
    monkeypatch.setattr(pipeline, "publish_task", lambda vid: {"ok": False, "error": "Authorization: Bearer sk_secret_job_audit", "skipped": "key=sk_secret_job_audit"})
    auto_res = long_job.long_autopilot_job()
    assert "sk_secret_job_audit" not in auto_res["indianvlogs"]["error"]
    assert "sk_secret_job_audit" not in auto_res["indianvlogs"]["skipped"]
    assert "[REDACTED]" in auto_res["indianvlogs"]["error"]


