"""Tests for portable channel snapshot export/import, channel cloning, and scripts/channels.py CLI."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels
from app.models import Channel, ClipJob, Feed, GrowthEvent, GrowthState, QuotaUsage, Video


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    init_db()
    with session_scope() as db:
        for table in (Feed, Channel, ClipJob, GrowthEvent, GrowthState, QuotaUsage, Video):
            db.execute(delete(table))
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    monkeypatch.setattr(settings, "youtube_token_path", "./secrets/youtube_token.json")
    monkeypatch.setattr(settings, "youtube_client_secrets", "")
    monkeypatch.setattr(settings, "growth_uploads_per_day", 3)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "growth_require_approval", True)
    monkeypatch.setattr(settings, "brand_name", "@doom")


def _write_channels(tmp_path, data: dict) -> Path:
    path = tmp_path / "channels.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "channels_cli_w11",
        Path(__file__).resolve().parent.parent / "scripts" / "channels.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_export_snapshot_strips_oauth_tokens_and_secret_paths(tmp_path):
    _write_channels(
        tmp_path,
        {
            "channels": [
                {
                    "slug": "tech",
                    "name": "Tech & AI",
                    "niche": "technology",
                    "brand_name": "@techclips",
                    "client_secrets": "/private/secrets/tech_client_secret.json",
                    "token_path": "/private/secrets/tech_oauth_token.json",
                    "uploads_per_day": 4,
                    "timezone": "Asia/Kolkata",
                    "require_approval": False,
                    "auto_approve_score": 0.75,
                    "cadence_backstop": True,
                    "enabled": True,
                    "feeds": ["@mkbhd", "https://m.youtube.com/@Mrwhosetheboss/videos"],
                },
                {
                    "slug": "gaming",
                    "name": "Gaming Highlights",
                    "client_secrets": "secrets/gaming_cs.json",
                    "token_path": "secrets/gaming_tok.json",
                    "uploads_per_day": 2,
                    "timezone": "UTC",
                    "enabled": False,
                    "feeds": ["UC-lHJZR3Gqxm24_Vd_AJ5Yw"],
                },
            ]
        },
    )
    with session_scope() as db:
        channels.sync_channels(db)
        tech = channels.channel_by_slug(db, "tech")
        assert tech is not None
        tech.youtube_channel_id = "UC_SUPER_SECRET_BINDING"
        db.flush()

        snap = channels.export_channels_snapshot(db)

    assert snap["version"] == 1
    assert snap["count"] == 2
    raw_json = json.dumps(snap)
    for forbidden in (
        "token_path",
        "client_secrets",
        "youtube_channel_id",
        "UC_SUPER_SECRET_BINDING",
        "/private/secrets",
        "gaming_tok.json",
    ):
        assert forbidden not in raw_json

    by_slug = {c["slug"]: c for c in snap["channels"]}
    assert set(by_slug) == {"tech", "gaming"}
    assert by_slug["tech"] == {
        "slug": "tech",
        "name": "Tech & AI",
        "niche": "technology",
        "brand_name": "@techclips",
        "uploads_per_day": 4,
        "timezone": "Asia/Kolkata",
        "require_approval": False,
        "auto_approve_score": 0.75,
        "cadence_backstop": True,
        "enabled": True,
        "feeds": [
            "https://www.youtube.com/@mkbhd",
            "https://www.youtube.com/@Mrwhosetheboss",
        ],
    }
    assert by_slug["gaming"]["enabled"] is False
    assert by_slug["gaming"]["feeds"] == ["https://www.youtube.com/channel/UC-lHJZR3Gqxm24_Vd_AJ5Yw"]


def test_import_snapshot_dry_run_validates_without_mutating_file_or_db(tmp_path):
    cfg_path = _write_channels(
        tmp_path,
        {
            "channels": [
                {
                    "slug": "tech",
                    "name": "Tech",
                    "uploads_per_day": 2,
                    "feeds": ["https://www.youtube.com/@mkbhd"],
                }
            ]
        },
    )
    before_text = cfg_path.read_text(encoding="utf-8")
    with session_scope() as db:
        channels.sync_channels(db)
        channels.channel_by_slug(db, "tech").youtube_channel_id = "UCkeep"

    payload = {
        "version": 1,
        "channels": [
            {
                "slug": "tech",
                "uploads_per_day": 5,
                "feeds": ["https://www.youtube.com/@mkbhd"],
            },
            {
                "slug": "science",
                "name": "Science",
                "uploads_per_day": 3,
                "timezone": "Europe/London",
                "feeds": ["@veritasium", "https://youtube.com/@Veritasium/shorts"],
            },
        ],
    }
    with session_scope() as db:
        res = channels.import_channels_snapshot(db, payload, dry_run=True)
        assert res["ok"] is True
        assert res["dry_run"] is True
        assert res["created"] == ["science"]
        assert res["updated"] == ["tech"]
        assert res["feeds"] == 2
        assert channels.channel_by_slug(db, "science") is None
        assert channels.channel_by_slug(db, "tech").uploads_per_day == 2
        assert channels.channel_by_slug(db, "tech").youtube_channel_id == "UCkeep"

    assert cfg_path.read_text(encoding="utf-8") == before_text


def test_import_snapshot_upserts_preserves_oauth_binding_and_ignores_smuggled_paths(tmp_path):
    _write_channels(
        tmp_path,
        {
            "channels": [
                {
                    "slug": "tech",
                    "name": "Tech Old",
                    "client_secrets": "secrets/custom_tech_cs.json",
                    "token_path": "secrets/custom_tech_tok.json",
                    "uploads_per_day": 2,
                    "feeds": ["https://www.youtube.com/@mkbhd"],
                },
                {
                    "slug": "comedy",
                    "name": "Comedy",
                    "uploads_per_day": 3,
                    "feeds": ["https://www.youtube.com/@KeyAndPeele"],
                },
            ]
        },
    )
    with session_scope() as db:
        channels.sync_channels(db)
        channels.channel_by_slug(db, "tech").youtube_channel_id = "UCbound_tech"

    payload = {
        "version": 1,
        "channels": [
            {
                "slug": "tech",
                "name": "Tech Updated",
                "niche": "gadgets",
                "brand_name": "@techup",
                "uploads_per_day": 6,
                "timezone": "America/New_York",
                "require_approval": False,
                "auto_approve_score": 0.8,
                "cadence_backstop": True,
                "token_path": "../../etc/passwd",
                "client_secrets": "/tmp/evil_secret.json",
                "youtube_channel_id": "UCattacker",
                "feeds": ["@mkbhd", "https://m.youtube.com/@MKBHD/videos", "@LinusTechTips"],
            },
            {
                "slug": "finance",
                "name": "Finance",
                "uploads_per_day": 4,
                "timezone": "UTC",
                "enabled": False,
                "token_path": "/tmp/evil_finance.json",
                "feeds": ["UC_x5XG1OV2P6uZZ5FSM9Ttw"],
            },
        ],
    }
    with session_scope() as db:
        res = channels.import_channels_snapshot(db, payload, dry_run=False)
        assert res["ok"] is True
        assert res["dry_run"] is False
        assert res["created"] == ["finance"]
        assert res["updated"] == ["tech"]

        tech = channels.channel_by_slug(db, "tech")
        assert tech is not None
        assert tech.name == "Tech Updated"
        assert tech.niche == "gadgets"
        assert tech.brand_name == "@techup"
        assert tech.uploads_per_day == 6
        assert tech.timezone == "America/New_York"
        assert tech.require_approval is False
        assert tech.auto_approve_score == pytest.approx(0.8)
        assert tech.cadence_backstop is True
        # Existing OAuth binding and credential paths MUST remain intact!
        assert tech.youtube_channel_id == "UCbound_tech"
        assert tech.token_path == "secrets/custom_tech_tok.json"
        assert tech.client_secrets == "secrets/custom_tech_cs.json"
        assert channels.feeds_for(db, tech) == [
            "https://www.youtube.com/@mkbhd",
            "https://www.youtube.com/@LinusTechTips",
        ]

        finance = channels.channel_by_slug(db, "finance")
        assert finance is not None
        assert finance.youtube_channel_id is None
        assert finance.token_path == "secrets/finance_token.json"
        assert finance.client_secrets == "secrets/finance_client_secret.json"
        assert finance.enabled is False
        assert channels.feeds_for(db, finance) == [
            "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw"
        ]

        # Pre-existing unmentioned sibling channel remains intact and enabled.
        comedy = channels.channel_by_slug(db, "comedy")
        assert comedy is not None and comedy.enabled is True


@pytest.mark.parametrize(
    "bad_payload,match",
    [
        ("not-a-dict", "JSON object"),
        ({}, "channels"),
        ({"channels": [{"slug": "Bad Slug!"}]}, "lowercase"),
        ({"channels": [{"slug": "error"}]}, "reserved"),
        ({"channels": [{"slug": "default"}]}, "default"),
        ({"channels": [{"slug": "dup"}, {"slug": "dup"}]}, "used twice"),
        ({"channels": [{"slug": "tech", "uploads_per_day": 51}]}, "uploads_per_day"),
        ({"channels": [{"slug": "tech", "uploads_per_day": True}]}, "uploads_per_day"),
        ({"channels": [{"slug": "tech", "auto_approve_score": 1.2}]}, "auto_approve_score"),
        ({"channels": [{"slug": "tech", "auto_approve_score": float("nan")}]}, "auto_approve_score"),
        ({"channels": [{"slug": "tech", "timezone": "Mars/Olympus"}]}, "timezone"),
        (
            {
                "channels": [
                    {"slug": "a", "feeds": ["@mkbhd"]},
                    {"slug": "b", "feeds": ["https://youtube.com/@MKBHD/"]},
                ]
            },
            "already listed under 'a'",
        ),
        ({"channels": [{"slug": "newchan", "feeds": ["https://www.youtube.com/@existing"]}]}, "existing_chan"),
    ],
)
def test_import_snapshot_rejects_invalid_payloads_leaving_fleet_unchanged(tmp_path, bad_payload, match):
    cfg_path = _write_channels(
        tmp_path,
        {"channels": [{"slug": "existing_chan", "feeds": ["https://www.youtube.com/@existing"]}]},
    )
    before = cfg_path.read_text(encoding="utf-8")
    with session_scope() as db:
        channels.sync_channels(db)
        with pytest.raises(channels.ChannelsConfigError, match=match):
            channels.import_channels_snapshot(db, bad_payload)  # type: ignore[arg-type]
        assert {c.slug for c in channels.active_channels(db)} == {"existing_chan"}
    assert cfg_path.read_text(encoding="utf-8") == before


def test_clone_channel_copies_settings_and_feeds_without_oauth_binding(tmp_path):
    _write_channels(
        tmp_path,
        {
            "channels": [
                {
                    "slug": "tech",
                    "name": "Tech Flagship",
                    "niche": "consumer-tech",
                    "brand_name": "@techflag",
                    "client_secrets": "secrets/custom_cs.json",
                    "token_path": "secrets/custom_tok.json",
                    "uploads_per_day": 5,
                    "timezone": "Asia/Kolkata",
                    "require_approval": False,
                    "auto_approve_score": 0.7,
                    "cadence_backstop": True,
                    "enabled": True,
                    "feeds": ["https://www.youtube.com/@mkbhd", "https://www.youtube.com/@Mrwhosetheboss"],
                }
            ]
        },
    )
    with session_scope() as db:
        channels.sync_channels(db)
        channels.channel_by_slug(db, "tech").youtube_channel_id = "UC_BOUND_TECH"
        db.flush()

        cloned = channels.clone_channel(db, "tech", "tech-2", name="Tech Secondary")
        assert cloned.slug == "tech-2"
        assert cloned.name == "Tech Secondary"
        assert cloned.niche == "consumer-tech"
        assert cloned.brand_name == "@techflag"
        assert cloned.uploads_per_day == 5
        assert cloned.timezone == "Asia/Kolkata"
        assert cloned.require_approval is False
        assert cloned.auto_approve_score == pytest.approx(0.7)
        assert cloned.cadence_backstop is True
        assert cloned.enabled is False
        assert cloned.youtube_channel_id is None
        assert cloned.client_secrets == "secrets/tech-2_client_secret.json"
        assert cloned.token_path == "secrets/tech-2_token.json"
        assert channels.feeds_for(db, cloned) == [
            "https://www.youtube.com/@mkbhd",
            "https://www.youtube.com/@Mrwhosetheboss",
        ]
        # Original channel stays running and retains its OAuth binding.
        orig = channels.channel_by_slug(db, "tech")
        assert orig.enabled is True
        assert orig.youtube_channel_id == "UC_BOUND_TECH"

        # Cloning nonexistent source or duplicate target is refused.
        with pytest.raises(channels.ChannelsConfigError, match="no channel"):
            channels.clone_channel(db, "missing", "tech-3")
        with pytest.raises(channels.ChannelsConfigError, match="already exists"):
            channels.clone_channel(db, "tech", "tech-2")

    # Cannot switch the clone on while the source is still enabled with the exact same feeds.
    with pytest.raises(channels.ChannelsConfigError, match="already listed under 'tech'"):
        channels.set_enabled("tech-2", True)

    # Once the original channel is disabled (handoff) or feeds differ, enabling the clone succeeds.
    channels.set_enabled("tech", False)
    res = channels.set_enabled("tech-2", True)
    assert "tech-2" in res["active"]


def test_round_trip_export_and_import_preserves_cloned_and_active_channels(tmp_path):
    _write_channels(
        tmp_path,
        {
            "channels": [
                {
                    "slug": "tech",
                    "name": "Tech",
                    "uploads_per_day": 4,
                    "timezone": "UTC",
                    "enabled": True,
                    "feeds": ["https://www.youtube.com/@mkbhd"],
                }
            ]
        },
    )
    with session_scope() as db:
        channels.sync_channels(db)
        channels.clone_channel(db, "tech", "tech-backup", name="Tech Backup")
        snap = channels.export_channels_snapshot(db)

    # Simulate importing onto a fresh machine.
    (tmp_path / "channels.json").unlink()
    with session_scope() as db:
        for table in (Feed, Channel, GrowthState):
            db.execute(delete(table))
        imported = channels.import_channels_snapshot(db, snap)
        assert imported["ok"] is True
        assert set(imported["created"]) == {"tech", "tech-backup"}
        assert channels.channel_by_slug(db, "tech").enabled is True
        assert channels.channel_by_slug(db, "tech-backup").enabled is False
        assert channels.feeds_for(db, channels.channel_by_slug(db, "tech-backup")) == [
            "https://www.youtube.com/@mkbhd"
        ]


def test_channels_cli_export_import_and_clone(tmp_path, capsys):
    cli = _load_cli()
    _write_channels(
        tmp_path,
        {
            "channels": [
                {
                    "slug": "tech",
                    "name": "Tech",
                    "niche": "tech",
                    "brand_name": "@tech",
                    "uploads_per_day": 4,
                    "timezone": "UTC",
                    "enabled": True,
                    "feeds": ["https://www.youtube.com/@mkbhd"],
                }
            ]
        },
    )
    with session_scope() as db:
        channels.sync_channels(db)
        channels.channel_by_slug(db, "tech").youtube_channel_id = "UC_SECRET"

    # 1. CLI clone
    rc = cli.main(["clone", "tech", "tech-clone", "--name", "Tech Clone"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tech-clone" in out

    # 2. CLI export --out
    snap_file = tmp_path / "portable_snapshot.json"
    rc = cli.main(["export", "--out", str(snap_file), "--json"])
    assert rc == 0
    export_meta = json.loads(capsys.readouterr().out)
    assert export_meta["ok"] is True and export_meta["count"] == 2
    snap_data = json.loads(snap_file.read_text(encoding="utf-8"))
    assert "UC_SECRET" not in json.dumps(snap_data)

    # 3. CLI export to stdout
    rc = cli.main(["export", "--json"])
    assert rc == 0
    stdout_snap = json.loads(capsys.readouterr().out)
    assert stdout_snap["count"] == 2

    # 4. Modify snapshot and test CLI import --dry-run --json then live import --json
    snap_data["channels"][0]["uploads_per_day"] = 6
    snap_file.write_text(json.dumps(snap_data), encoding="utf-8")

    rc = cli.main(["import", str(snap_file), "--dry-run", "--json"])
    assert rc == 0
    dry_out = json.loads(capsys.readouterr().out)
    assert dry_out["ok"] is True and dry_out["dry_run"] is True
    with session_scope() as db:
        assert channels.channel_by_slug(db, "tech").uploads_per_day == 4

    rc = cli.main(["import", str(snap_file), "--json"])
    assert rc == 0
    live_out = json.loads(capsys.readouterr().out)
    assert live_out["ok"] is True and live_out["dry_run"] is False
    with session_scope() as db:
        tech = channels.channel_by_slug(db, "tech")
        assert tech.uploads_per_day == 6
        assert tech.youtube_channel_id == "UC_SECRET"

    # 5. Missing file returns non-zero cleanly
    rc = cli.main(["import", str(tmp_path / "does_not_exist.json"), "--json"])
    assert rc == 1
    err_out = json.loads(capsys.readouterr().out)
    assert err_out["ok"] is False
