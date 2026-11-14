"""Wave 10 TDD resilience suite for config, DB, logging_config, scheduler, and pipeline."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text

from app import db as db_module
from app import logging_config, scheduler
from app.config import Settings, settings
from app.db import init_db, session_scope
from app.models import ClipJob, Video, VideoStatus
from app.scrapers.base import ScrapedItem
from tasks import pipeline


# ---------------------------------------------------------------------------
# 1. app/config.py — comma-separated list settings, numeric guards, lightweight
# ---------------------------------------------------------------------------


def test_config_accepts_comma_separated_json_and_python_lists(monkeypatch):
    monkeypatch.setenv("TARGET_HANDLES", "@alice, @bob , ")
    monkeypatch.setenv("TARGET_HASHTAGS", "tech, ai, shorts")
    monkeypatch.setenv("ENABLED_PLATFORMS", "snapchat,youtube")
    monkeypatch.setenv("TARGET_LANGUAGES", "EN, hi")
    monkeypatch.setenv("TARGET_TOPICS", "Coding, Science")
    monkeypatch.setenv("EXCLUDED_TOPICS", "Politics, Drama")
    monkeypatch.setenv("GROWTH_FEEDS", "https://www.youtube.com/@a, https://www.youtube.com/@b")
    monkeypatch.setenv("YOUTUBE_DEFAULT_TAGS", "shorts, science")
    monkeypatch.setenv("BLOCKED_TITLE_WORDS", "Clickbait, Spoiler")

    cfg = Settings(_env_file=None)
    assert cfg.handles_list == ["@alice", "@bob"]
    assert cfg.hashtags_list == ["tech", "ai", "shorts"]
    assert cfg.enabled_platforms_list == ["snapchat", "youtube"]
    assert cfg.languages_list == ["en", "hi"]
    assert cfg.topics_list == ["coding", "science"]
    assert cfg.excluded_topics_list == ["politics", "drama"]
    assert cfg.growth_feeds_list == ["https://www.youtube.com/@a", "https://www.youtube.com/@b"]
    assert cfg.tags_list == ["shorts", "science"]
    assert cfg.blocked_title_words_list == ["clickbait", "spoiler"]

    # Direct Python list[str] kwargs and JSON arrays also work without breaking types
    cfg_py = Settings(
        _env_file=None,
        target_handles=["@x", "@y"],
        target_hashtags='["one","two"]',
        blocked_title_words=[" Rugpull ", "Scam"],
    )
    assert cfg_py.handles_list == ["@x", "@y"]
    assert cfg_py.hashtags_list == ["one", "two"]
    assert cfg_py.blocked_title_words_list == ["rugpull", "scam"]

    # BLOCKED_TITLE_WORDS integrates with content_safety_reason
    monkeypatch.setattr(settings, "blocked_title_words", "spoiler, clickbait")
    flagged = ScrapedItem(
        source="snapchat",
        source_id="blk1",
        source_url="https://snap/blk1",
        title="Huge SPOILER ending",
    )
    assert pipeline.content_safety_reason(flagged) == "blocked term: spoiler"


def test_config_numeric_whitespace_blank_and_nan_guards(monkeypatch):
    monkeypatch.setenv("UPLOADS_PER_DAY", "  12  ")
    monkeypatch.setenv("AUTO_APPROVE_MIN_VIRAL_SCORE", "  0.65 \t")
    cfg = Settings(_env_file=None)
    assert cfg.uploads_per_day == 12
    assert cfg.auto_approve_min_viral_score == pytest.approx(0.65)

    # Blank numeric env vars in .env fall back cleanly to defaults instead of crashing startup
    monkeypatch.setenv("UPLOADS_PER_DAY", "   ")
    monkeypatch.setenv("AUTO_APPROVE_MIN_VIRAL_SCORE", "")
    cfg_blank = Settings(_env_file=None)
    assert cfg_blank.uploads_per_day == 8
    assert cfg_blank.auto_approve_min_viral_score == pytest.approx(0.45)

    # Non-finite float strings are rejected
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auto_approve_min_viral_score="nan")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, zoom_max="inf")


def test_config_import_is_numpy_free_and_normalizes_windows_file_uri():
    from app.winproc import background_subprocess_kwargs

    proc = subprocess.run(
        [sys.executable, "-c", "import sys, app.config; assert 'numpy' not in sys.modules"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        **background_subprocess_kwargs(low_priority=True),
    )
    assert proc.returncode == 0, proc.stderr

    cfg = Settings(_env_file=None, database_url="file:///D:/data/doomscroller.db")
    assert cfg.db_url == "sqlite:///D:/data/doomscroller.db"


# ---------------------------------------------------------------------------
# 2. app/logging_config.py — secret scrubbing, malformed/non-UTF8 JSONL, limit<=0
# ---------------------------------------------------------------------------


def test_logging_scrubs_secrets_before_writing_and_on_read(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)
    secrets_map = {
        "review_token": "rv_super_secret_token_99",
        "telegram_bot_token": "777888999:AAH_tg_configured_secret_xyz",
        "pixabay_api_key": "pxb_secret_key_1111",
        "pexels_api_key": "pxl_secret_key_2222",
        "deepgram_api_key": "dg_secret_key_3333",
        "sarvam_api_key": "srv_secret_key_4444",
    }
    for attr, val in secrets_map.items():
        monkeypatch.setattr(settings, attr, val)

    inline_bot = "bot123456789:AAH_inline_bot_secret_token"
    entry = logging_config.record_job_run(
        "growth_notify",
        {
            "review_token": secrets_map["review_token"],
            "url": f"https://api.telegram.org/{inline_bot}/sendMessage",
            "nested": [f"pixabay={secrets_map['pixabay_api_key']}", f"pexels={secrets_map['pexels_api_key']}"],
        },
        duration_sec=1.25,
        ok=False,
        error=(
            f"failed dg={secrets_map['deepgram_api_key']} "
            f"sarvam={secrets_map['sarvam_api_key']} "
            f"tg={secrets_map['telegram_bot_token']} "
            "PIXABAY_API_KEY=raw_env_pixabay_999"
        ),
    )

    raw_jsonl = logging_config.get_job_history_path().read_text(encoding="utf-8")
    for leak in (*secrets_map.values(), "AAH_inline_bot_secret_token", "raw_env_pixabay_999"):
        assert leak not in raw_jsonl
        assert leak not in json.dumps(entry)

    # Also scrub legacy unredacted lines when reading back
    with open(logging_config.get_job_history_path(), "a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({
                "job_id": "legacy",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "ok": False,
                "error": f"leaked {secrets_map['review_token']} and {inline_bot}",
            }) + "\n"
        )
    loaded = logging_config.read_job_history(limit=10)
    dumped_loaded = json.dumps(loaded)
    assert secrets_map["review_token"] not in dumped_loaded
    assert "AAH_inline_bot_secret_token" not in dumped_loaded

    # Process file log + read_recent_logs also scrub secrets
    logging_config.configure_logging(force=True)
    import logging

    logging.getLogger("test.secret").warning(
        "auth failed token=%s url=https://api.telegram.org/%s/getMe DEEPGRAM_API_KEY=inline_dg_secret",
        secrets_map["review_token"],
        inline_bot,
    )
    recent_lines = logging_config.read_recent_logs(limit=25)
    joined_logs = "\n".join(recent_lines)
    assert "auth failed" in joined_logs
    assert secrets_map["review_token"] not in joined_logs
    assert "AAH_inline_bot_secret_token" not in joined_logs
    assert "inline_dg_secret" not in joined_logs


def test_logging_handles_malformed_jsonl_non_utf8_bytes_and_non_positive_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)
    hist_path = logging_config.get_job_history_path()
    payload = (
        json.dumps({"job_id": "first", "timestamp": "2026-01-01T00:00:00+00:00", "ok": True}).encode("utf-8")
        + b"\n\xff\xfeinvalid utf8 line\n"
        + b'{"job_id": "truncated_json"\n'
        + b"null\n123\n[\"list_not_dict\"]\n"
        + json.dumps({"job_id": "second", "timestamp": "2026-01-02T00:00:00+00:00", "ok": True}).encode("utf-8")
        + b"\n"
    )
    hist_path.write_bytes(payload)

    log_path = tmp_path / logging_config.LOG_FILE
    log_path.write_bytes(b"2026-01-01 INFO boot ok\n\xff\xfecorrupt bytes line\n2026-01-01 INFO ready\n")

    assert logging_config.read_job_history(limit=0) == []
    assert logging_config.read_job_history(limit=-5) == []
    entries = logging_config.read_job_history(limit=10)
    assert [e["job_id"] for e in entries] == ["first", "second"]
    assert logging_config.job_health(limit=0) == {}

    assert logging_config.read_recent_logs(limit=0) == []
    assert logging_config.read_recent_logs(limit=-2) == []
    recent = logging_config.read_recent_logs(limit=2)
    assert len(recent) == 2
    assert "ready" in recent[-1]


# ---------------------------------------------------------------------------
# 3. app/db.py & scripts/init_db.py — SQLite busy_timeout/WAL & schema upgrades
# ---------------------------------------------------------------------------


def test_sqlite_engine_configures_busy_timeout_and_wal(tmp_path):
    db_file = tmp_path / "wal_check.db"
    eng = db_module.create_engine_for_url(f"sqlite:///{db_file.as_posix()}")
    try:
        with eng.connect() as conn:
            busy_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
            journal = str(conn.execute(text("PRAGMA journal_mode")).scalar() or "").lower()
        assert busy_ms >= 30_000
        assert journal == "wal"
    finally:
        eng.dispose()


def test_init_db_upgrades_legacy_sqlite_table_missing_indexed_columns(tmp_path, monkeypatch, capsys):
    db_file = tmp_path / "legacy_upgrade.db"
    raw = sqlite3.connect(db_file)
    try:
        # Simulate an older videos table created before channel_id (indexed!), keywords, viral_score existed
        raw.execute(
            """
            CREATE TABLE videos (
                id VARCHAR(32) PRIMARY KEY,
                source VARCHAR(32),
                source_id VARCHAR(128),
                source_url TEXT,
                status VARCHAR(32)
            )
            """
        )
        raw.execute(
            "INSERT INTO videos (id, source, source_id, source_url, status) "
            "VALUES ('v1', 'snapchat', 's1', 'https://snap/1', 'PENDING')"
        )
        raw.commit()
    finally:
        raw.close()

    eng = create_engine(f"sqlite:///{db_file.as_posix()}", future=True)
    monkeypatch.setattr(db_module, "engine", eng)
    try:
        # Must NOT fail with OperationalError: no such column: channel_id when creating ix_videos_channel_id
        db_module.init_db()
        db_module.init_db()  # idempotent second run

        with eng.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(videos)"))}
            indexes = {row[1] for row in conn.execute(text("PRAGMA index_list(videos)"))}
            row = conn.execute(text("SELECT id, viral_score, keywords, channel_id FROM videos WHERE id='v1'")).one()

        assert "channel_id" in cols
        assert "ix_videos_channel_id" in indexes
        assert row == ("v1", 0.0, "[]", None)

        # scripts/init_db.py runs cleanly and redacts credentials in printed DB URLs
        spec = importlib.util.spec_from_file_location(
            "init_db_script",
            Path(__file__).resolve().parent.parent / "scripts" / "init_db.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod.settings, "database_url", "postgresql://admin:topsecretpw@db.example.com/prod")
        monkeypatch.setattr(mod, "init_db", db_module.init_db)
        mod.main()
        out = capsys.readouterr().out
        assert "topsecretpw" not in out
        assert "Database initialized." in out
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# 4. tasks/pipeline.py & app/scheduler.py — naive/aware recovery, missing files, cleanup
# ---------------------------------------------------------------------------


def test_stuck_publishing_recovery_and_overdue_normalize_naive_and_offset_timestamps(tmp_path, monkeypatch):
    init_db()
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(settings, "use_mock", False)

    base_utc = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    naive_now = base_utc.replace(tzinfo=None)  # naive UTC caller
    ist = timezone(timedelta(hours=5, minutes=30))
    claimed_ist = (base_utc - timedelta(hours=3)).astimezone(ist).isoformat()

    clip_path = tmp_path / "stuck_clip.mp4"
    clip_path.write_bytes(b"0" * 2048)

    with session_scope() as db:
        v = Video(
            source="local",
            source_id="stuck-naive-1",
            source_url="https://example.com/1",
            title="Naive Timestamp Test",
            local_path=str(clip_path),
            transformed_path=str(clip_path),
            status=VideoStatus.PUBLISHING,
            meta={
                "publish_claimed_at": claimed_ist,
                "publish_title": "Naive Timestamp Test #Shorts",
            },
        )
        db.add(v)
        db.flush()
        vid = v.id

    with session_scope() as db:
        loaded = db.get(Video, vid)
        assert pipeline.publish_claim_is_stale(loaded, naive_now) is True

    class _NaiveUploadsInsights:
        def channel_id(self):
            return "UC_TEST"

        def uploads_since(self, since):
            # Return a NAIVE published_at datetime from upstream
            return [{
                "id": "yt_recovered_1",
                "title": "Naive Timestamp Test #Shorts",
                "published_at": (base_utc - timedelta(hours=2, minutes=30)).replace(tzinfo=None),
            }]

    monkeypatch.setattr("app.growth.youtube.get_insights", lambda: _NaiveUploadsInsights())
    monkeypatch.setattr("app.growth.youtube.account_mismatch", lambda ch, ins: None)

    outcome = pipeline.recover_stuck_publishes(now=naive_now)
    assert outcome == {"published": 1, "released": 0, "left": 0}
    with session_scope() as db:
        recovered = db.get(Video, vid)
        assert recovered.status == VideoStatus.PUBLISHED
        assert recovered.youtube_video_id == "yt_recovered_1"

    # scheduler.overdue_jobs also accepts naive `now` without TypeError
    logging_config.get_job_history_path().parent.mkdir(parents=True, exist_ok=True)
    logging_config.get_job_history_path().write_text(
        json.dumps({"job_id": "scrape", "timestamp": (base_utc - timedelta(hours=10)).isoformat(), "ok": True}) + "\n",
        encoding="utf-8",
    )
    assert "scrape" in scheduler.overdue_jobs(now=naive_now)


def test_missing_local_video_files_before_upload_fail_cleanly_without_crashing(tmp_path, monkeypatch):
    init_db()
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "auto_publish", True)
    monkeypatch.setattr(settings, "growth_enabled", False)
    monkeypatch.setattr(settings, "upload_jitter_minutes", 0)

    # Clear leftover TRANSFORMED rows so publish_job sees deterministic queue
    with session_scope() as db:
        db.query(Video).filter(Video.status.in_([VideoStatus.TRANSFORMED, VideoStatus.PUBLISHING])).delete()

    valid_mp4 = tmp_path / "valid_ready.mp4"
    valid_mp4.write_bytes(b"0" * 4096)

    with session_scope() as db:
        # 1. TRANSFORMED row with transformed_path=None (previously wedged publish_job forever!)
        v_none = Video(
            source="snapchat",
            source_id="missing-none",
            source_url="https://snap/none",
            title="Missing None",
            transformed_path=None,
            transformed_at=datetime.now(timezone.utc) - timedelta(hours=3),
            status=VideoStatus.TRANSFORMED,
        )
        # 2. TRANSFORMED row whose transformed_path was deleted from disk
        v_gone = Video(
            source="snapchat",
            source_id="missing-gone",
            source_url="https://snap/gone",
            title="Missing Gone",
            transformed_path=str(tmp_path / "deleted.mp4"),
            transformed_at=datetime.now(timezone.utc) - timedelta(hours=2),
            status=VideoStatus.TRANSFORMED,
        )
        # 3. Valid TRANSFORMED row behind them in queue
        v_ok = Video(
            source="snapchat",
            source_id="ready-valid",
            source_url="https://snap/ok",
            title="Valid Clip",
            local_path=str(valid_mp4),
            transformed_path=str(valid_mp4),
            transformed_at=datetime.now(timezone.utc) - timedelta(hours=1),
            status=VideoStatus.TRANSFORMED,
        )
        db.add_all([v_none, v_gone, v_ok])
        db.flush()
        id_none, id_gone, id_ok = v_none.id, v_gone.id, v_ok.id

    res = scheduler.publish_job()
    assert res.get("ok") is True
    assert res.get("video_id") == id_ok

    with session_scope() as db:
        assert db.get(Video, id_none).status == VideoStatus.FAILED
        assert db.get(Video, id_gone).status == VideoStatus.FAILED
        assert db.get(Video, id_ok).status == VideoStatus.PUBLISHED

    # 4. Race condition: file vanishes right before publisher.upload raises FileNotFoundError
    vanishing_mp4 = tmp_path / "vanish.mp4"
    vanishing_mp4.write_bytes(b"0" * 2048)
    with session_scope() as db:
        v_race = Video(
            source="snapchat",
            source_id="missing-race",
            source_url="https://snap/race",
            title="Vanishing Clip",
            transformed_path=str(vanishing_mp4),
            status=VideoStatus.TRANSFORMED,
        )
        db.add(v_race)
        db.flush()
        id_race = v_race.id

    class _VanishingPublisher:
        def upload(self, **kwargs):
            raise FileNotFoundError(f"video file not found: {kwargs['file_path']}")

    monkeypatch.setattr(pipeline, "_publisher_for", lambda ch: _VanishingPublisher())
    out_race = pipeline.publish_task(id_race)
    assert out_race["ok"] is False
    assert out_race["retryable"] is False
    with session_scope() as db:
        assert db.get(Video, id_race).status == VideoStatus.FAILED

    # 5. Real mode (use_mock=False) transform_task with local_path=None must NOT synthesize black video,
    #    while Wave 6 rendered clip with transformed_path intact still fast-paths as "already rendered".
    monkeypatch.setattr(settings, "use_mock", False)
    rendered_mp4 = tmp_path / "rendered_clip.mp4"
    rendered_mp4.write_bytes(b"0" * 4096)
    with session_scope() as db:
        v_unrendered = Video(
            source="snapchat",
            source_id="real-no-local",
            source_url="https://snap/nolocal",
            title="No Local",
            local_path=None,
            status=VideoStatus.APPROVED,
        )
        v_rendered = Video(
            source="youtube",
            source_id="wave6-rendered",
            source_url="https://youtu.be/abc",
            title="Rendered Clip",
            local_path=None,
            transformed_path=str(rendered_mp4),
            status=VideoStatus.APPROVED,
            meta={"clip_rendered": True},
        )
        db.add_all([v_unrendered, v_rendered])
        db.flush()
        id_unrendered, id_rendered = v_unrendered.id, v_rendered.id

    out_unrendered = pipeline.transform_task(id_unrendered)
    assert out_unrendered["ok"] is False
    out_rendered = pipeline.transform_task(id_rendered)
    assert out_rendered["ok"] is True and out_rendered["action"] == "already rendered"
    with session_scope() as db:
        assert db.get(Video, id_unrendered).status == VideoStatus.FAILED
        assert db.get(Video, id_rendered).status == VideoStatus.TRANSFORMED


def test_cleanup_job_skips_symlinks_junctions_active_inbox_and_rendered_clips(tmp_path, monkeypatch):
    init_db()
    content = tmp_path / "content"
    inbox = content / "inbox"
    outside = tmp_path / "external_archive"
    content.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    outside.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(type(settings), "content_path", property(lambda self: content))
    monkeypatch.setattr(settings, "growth_inbox_dir", str(inbox))

    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).timestamp()

    def _aged_file(p: Path, size: int = 2048) -> Path:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        os.utime(p, (old_ts, old_ts))
        return p

    # 1. External target + NTFS directory junction (Windows mklink /J) under content/clips/junc_dir
    ext_clip = _aged_file(outside / "precious_master.mp4")
    clips_root = content / "clips"
    clips_root.mkdir(parents=True, exist_ok=True)
    junc_dir = clips_root / "junc_archive"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junc_dir), str(outside)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
        )
    else:
        junc_dir.symlink_to(outside, target_is_directory=True)

    # 2. Active inbox file (even if referenced by an expired REJECTED Video row!)
    inbox_video = _aged_file(inbox / "owner_podcast.mp4")

    # 3. Local file in content/ queued in an active ClipJob (mtime > 6h preserved from copy)
    queued_local = _aged_file(content / "queued_for_clipper.mp4")

    # 4. Wave 6 rendered clip in PENDING (aged 20 days, local_path == transformed_path)
    rendered_clip_file = _aged_file(content / "clips" / "yt_1" / "10_40.mp4")

    # 5. Real unreferenced orphan in content/ (aged 20 days) — SHOULD be deleted
    real_orphan = _aged_file(content / "true_orphan.mp4")

    old_dt = datetime.now(timezone.utc) - timedelta(days=20)
    with session_scope() as db:
        db.query(ClipJob).delete()
        db.add(ClipJob(source=str(queued_local), status="queued"))
        db.add(
            Video(
                source="local",
                source_id="expired-inbox-ref",
                source_url="owner_podcast.mp4",
                local_path=str(inbox_video),
                curated_at=old_dt,
                scraped_at=old_dt,
                status=VideoStatus.REJECTED,
            )
        )
        db.add(
            Video(
                source="youtube",
                source_id="rendered-pending-keep",
                source_url="https://youtu.be/xyz",
                local_path=str(rendered_clip_file),
                transformed_path=str(rendered_clip_file),
                scraped_at=old_dt,
                status=VideoStatus.PENDING,
                meta={"clip_rendered": True},
            )
        )

    try:
        res = scheduler.cleanup_job()
        assert res["orphans_deleted"] >= 1
        assert not real_orphan.exists(), "true orphan should be cleaned"
        assert ext_clip.exists(), "file behind NTFS junction / symlink must NEVER be deleted"
        assert inbox_video.exists(), "inbox file must NEVER be deleted by cleanup_job"
        assert queued_local.exists(), "local file queued in active ClipJob must NEVER be deleted"
        assert rendered_clip_file.exists(), "Wave 6 PENDING rendered clip must NEVER be deleted"
    finally:
        # Remove junction safely (rmdir on Windows removes junction without touching target)
        if junc_dir.exists() or os.path.islink(junc_dir):
            if os.name == "nt":
                os.rmdir(junc_dir)
            else:
                junc_dir.unlink()
