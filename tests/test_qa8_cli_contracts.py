"""Cross-cutting CLI contract suite (qa-8): --help, exit codes, --channel errors, and strict --json stdout."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import namedtuple
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import delete

from app.clipper.media import Interrupted
from app.config import settings
from app.db import init_db, session_scope
from app.growth import channels as channels_mod
from app.models import (
    Channel,
    ClipJob,
    Creator,
    Feed,
    GrowthEvent,
    GrowthState,
    MetadataDraft,
    QuotaUsage,
    Video,
    VideoSnapshot,
)
from app.publishers.youtube_publisher import READ_SCOPE, SCOPES, write_token_file
from scripts import channels as channels_cli
from scripts import clip as clip_cli
from scripts import doctor as doctor_cli
from scripts import final_audit as final_audit_cli
from scripts import growth as growth_cli
from scripts import seed as seed_cli
from scripts import youtube_oauth as oauth_cli

CANARY_SECRETS: dict[str, str] = {
    "review_token": "canary-qa8-review-tok-9001",
    "deepgram_api_key": "canary-qa8-deepgram-key-9002",
    "sarvam_api_key": "canary-qa8-sarvam-key-9003",
    "pixabay_api_key": "canary-qa8-pixabay-key-9004",
    "pexels_api_key": "canary-qa8-pexels-key-9005",
    "telegram_bot_token": "888777666:CanaryQa8TelegramSecret9006",
}
DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def _clear_all_tables() -> None:
    with session_scope() as db:
        for table in (
            Feed,
            VideoSnapshot,
            MetadataDraft,
            GrowthEvent,
            GrowthState,
            ClipJob,
            QuotaUsage,
            Creator,
            Video,
            Channel,
        ):
            db.execute(delete(table))


@pytest.fixture(autouse=True)
def _isolated_cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    init_db()
    _clear_all_tables()

    content_dir = tmp_path / "content"
    inbox_dir = content_dir / "inbox"
    inbox_dir.mkdir(parents=True)

    monkeypatch.setattr(settings, "content_dir", str(content_dir))
    monkeypatch.setattr(settings, "growth_inbox_dir", str(inbox_dir))
    monkeypatch.setattr(settings, "channels_file", str(tmp_path / "channels.json"))
    monkeypatch.setattr(settings, "growth_feeds", "[]")
    monkeypatch.setattr(settings, "growth_enabled", True)
    monkeypatch.setattr(settings, "growth_require_approval", True)
    monkeypatch.setattr(settings, "growth_timezone", "UTC")
    monkeypatch.setattr(settings, "growth_uploads_per_day", 3)
    monkeypatch.setattr(settings, "enable_publish", True)
    monkeypatch.setattr(settings, "use_mock", True)
    monkeypatch.setattr(settings, "youtube_client_secrets", "")
    monkeypatch.setattr(settings, "youtube_token_path", str(tmp_path / "secrets" / "youtube_token.json"))
    for attr, secret in CANARY_SECRETS.items():
        monkeypatch.setattr(settings, attr, secret)

    with session_scope() as db:
        channels_mod.sync_channels(db)

    yield
    _clear_all_tables()


def _invoke_cli(monkeypatch: pytest.MonkeyPatch, name: str, argv: list[str]) -> int:
    dispatch: dict[str, Callable[[list[str]], int]] = {
        "clip": clip_cli.main,
        "channels": channels_cli.main,
        "growth": growth_cli.main,
        "doctor": doctor_cli.main,
        "final_audit": final_audit_cli.main,
        "seed": seed_cli.main,
    }
    if name == "youtube_oauth":
        monkeypatch.setattr(sys, "argv", ["youtube_oauth.py", *argv])
        return oauth_cli.main()
    return dispatch[name](argv)


def _assert_strict_json(raw_stdout: str) -> dict:
    assert raw_stdout, "expected non-empty JSON on stdout"
    stripped = raw_stdout.strip()
    assert stripped.startswith("{") and stripped.endswith("}"), (
        f"stdout must be a pure JSON object with no surrounding prose: {raw_stdout!r}"
    )
    parsed = json.loads(
        raw_stdout,
        parse_constant=lambda c: (_ for _ in ()).throw(AssertionError(f"non-RFC8259 constant in JSON: {c}")),
    )
    assert isinstance(parsed, dict)
    for secret in CANARY_SECRETS.values():
        assert secret not in raw_stdout, f"secret canary leaked into CLI JSON stdout: {secret}"
    return parsed


def _seed_managed_channels(tmp_path: Path) -> None:
    cfg = {
        "channels": [
            {
                "slug": "tech",
                "name": "Tech Channel",
                "niche": "tech",
                "brand_name": "@tech",
                "client_secrets": str(tmp_path / "secrets" / "tech_cs.json"),
                "token_path": str(tmp_path / "secrets" / "tech_tok.json"),
                "uploads_per_day": 3,
                "timezone": "UTC",
                "require_approval": True,
                "enabled": True,
                "feeds": ["https://www.youtube.com/@mkbhd"],
            },
            {
                "slug": "science",
                "name": "Science Channel",
                "niche": "science",
                "brand_name": "@sci",
                "client_secrets": str(tmp_path / "secrets" / "sci_cs.json"),
                "token_path": str(tmp_path / "secrets" / "sci_tok.json"),
                "uploads_per_day": 2,
                "timezone": "UTC",
                "require_approval": False,
                "enabled": True,
                "feeds": ["https://www.youtube.com/@veritasium"],
            },
        ]
    }
    (tmp_path / "channels.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    with session_scope() as db:
        channels_mod.sync_channels(db)


# ---------------------------------------------------------------------------
# 1. --help / -h and invalid-flag argparse exit-code contracts across all 7 CLIs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cli_name",
    ["clip", "channels", "growth", "doctor", "final_audit", "youtube_oauth", "seed"],
)
@pytest.mark.parametrize("help_flag", ["-h", "--help"])
def test_all_clis_help_exits_zero_with_clean_usage_and_empty_stderr(
    cli_name: str,
    help_flag: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc_info:
        _invoke_cli(monkeypatch, cli_name, [help_flag])
    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "usage:" in captured.out.lower()
    assert captured.err == ""


@pytest.mark.parametrize(
    "subcommand",
    ["list", "add", "connect", "set", "enable", "disable", "feed", "export", "import", "clone"],
)
def test_channels_subcommands_help_exits_zero_cleanly(
    subcommand: str,
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc_info:
        channels_cli.main([subcommand, "--help"])
    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "usage:" in captured.out.lower()
    assert captured.err == ""


@pytest.mark.parametrize(
    "cli_name",
    ["clip", "channels", "growth", "doctor", "final_audit", "youtube_oauth", "seed"],
)
def test_all_clis_reject_unknown_flags_with_exit_code_2_and_clean_stdout(
    cli_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    with pytest.raises(SystemExit) as exc_info:
        _invoke_cli(monkeypatch, cli_name, ["--definitely-not-a-valid-flag-qa8"])
    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert captured.out == ""
    assert "unrecognized arguments" in captured.err.lower()


# ---------------------------------------------------------------------------
# 2. --channel error handling & whitespace-tolerant slug resolution contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cli_name", "argv", "expect_json"),
    [
        ("clip", ["https://youtu.be/demo", "--enqueue", "--channel", "no_such_chan", "--json"], True),
        ("clip", ["--jobs", "--channel", "no_such_chan", "--json"], True),
        ("clip", ["--inbox", "--channel", "no_such_chan", "--json"], True),
        ("growth", ["status", "--channel", "no_such_chan", "--json"], True),
        ("growth", ["report", "--channel", "no_such_chan", "--json"], True),
        ("growth", ["doctor", "--offline", "--channel", "no_such_chan", "--json"], True),
        ("growth", ["pause", "no_such_chan", "--json"], True),
        ("growth", ["resume", "no_such_chan", "--json"], True),
        ("youtube_oauth", ["--status", "--channel", "no_such_chan", "--json"], True),
        ("seed", ["3", "--channel", "no_such_chan", "--json"], True),
        ("clip", ["--jobs", "--channel", "no_such_chan"], False),
        ("growth", ["status", "-c", "no_such_chan"], False),
        ("youtube_oauth", ["--status", "--channel", "no_such_chan"], False),
        ("seed", ["3", "--channel", "no_such_chan"], False),
    ],
)
def test_unknown_channel_exits_1_and_names_known_channels_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_name: str,
    argv: list[str],
    expect_json: bool,
):
    _seed_managed_channels(tmp_path)
    rc = _invoke_cli(monkeypatch, cli_name, argv)
    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    if expect_json:
        payload = _assert_strict_json(captured.out)
        err_text = str(payload.get("error", ""))
        assert "no_such_chan" in err_text
        assert "tech" in err_text and "science" in err_text
    else:
        combined = captured.out + captured.err
        assert "no_such_chan" in combined
        assert "tech" in combined and "science" in combined


def test_whitespace_padded_channel_slug_resolves_across_clip_growth_and_seed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _seed_managed_channels(tmp_path)

    assert seed_cli.main(["2", "--channel", "  tech  ", "--json"]) == 0
    seed_out = _assert_strict_json(capsys.readouterr().out)
    assert seed_out["ok"] is True and seed_out["channel"] == "tech"

    assert clip_cli.main(["https://youtu.be/ws1", "--enqueue", "--channel", "  tech  ", "--json"]) == 0
    enq_out = _assert_strict_json(capsys.readouterr().out)
    assert enq_out["ok"] is True and enq_out["channel"] == "tech"

    assert clip_cli.main(["--jobs", "--channel", "  tech  ", "--json"]) == 0
    jobs_out = _assert_strict_json(capsys.readouterr().out)
    assert jobs_out["ok"] is True and jobs_out["channel"] == "tech" and jobs_out["counts"]["queued"] == 1

    assert growth_cli.main(["status", "--channel", "  tech  ", "--json"]) == 0
    st_out = _assert_strict_json(capsys.readouterr().out)
    assert [c["slug"] for c in st_out["channels"]] == ["tech"]

    assert growth_cli.main(["report", "--channel", "  tech  ", "--json"]) == 0
    rep_out = _assert_strict_json(capsys.readouterr().out)
    assert rep_out["slug"] == "tech"

    assert growth_cli.main(["pause", "  tech  ", "--json"]) == 0
    assert _assert_strict_json(capsys.readouterr().out) == {"ok": True, "paused": "tech"}
    assert growth_cli.main(["resume", "  tech  ", "--json"]) == 0
    assert _assert_strict_json(capsys.readouterr().out)["resumed"] is True


# ---------------------------------------------------------------------------
# 3. Strict --json stdout & exit-code contracts per CLI
# ---------------------------------------------------------------------------


def test_clip_cli_strict_json_error_and_interrupted_batch_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    # Validation bounds & missing arguments all return exit 1 and pure JSON on stdout (nothing on stderr).
    for bad_argv in (
        ["--json"],
        ["--enqueue", "--json"],
        ["--jobs", "--limit", "0", "--json"],
        ["--jobs", "--limit", "101", "--json"],
        ["https://youtu.be/a", "--count", "0", "--json"],
        ["https://youtu.be/a", "--count", "21", "--json"],
    ):
        rc = clip_cli.main(bad_argv)
        captured = capsys.readouterr()
        assert rc == 1, f"expected exit 1 for {bad_argv}"
        assert captured.err == ""
        payload = _assert_strict_json(captured.out)
        assert payload["ok"] is False and payload.get("error")

    # Over-limit batch (>500 sources) rejected with exit 1 & strict JSON.
    huge_batch = tmp_path / "huge.txt"
    huge_batch.write_text("\n".join(f"https://youtu.be/v{i}" for i in range(501)), encoding="utf-8")
    assert clip_cli.main(["--batch", str(huge_batch), "--enqueue", "--json"]) == 1
    assert "500" in _assert_strict_json(capsys.readouterr().out)["error"]

    # Ambiguous --retry prefix matching 2 jobs exits 1 with strict JSON.
    with session_scope() as db:
        db.add_all([
            ClipJob(id="abcd1111-0000-0000-0000-000000000001", source="https://youtu.be/1", status="failed"),
            ClipJob(id="abcd1111-0000-0000-0000-000000000002", source="https://youtu.be/2", status="failed"),
        ])
    assert clip_cli.main(["--retry", "abcd1111", "--json"]) == 1
    ambig = _assert_strict_json(capsys.readouterr().out)
    assert ambig["ok"] is False and "more than one" in ambig["error"]

    # Interrupted during batch foreground run records failure, stops remaining items, exits 1 with pure JSON.
    small_batch = tmp_path / "two.txt"
    small_batch.write_text("https://youtu.be/first\nhttps://youtu.be/second\n", encoding="utf-8")

    def _interrupting_clip(source: str, **kwargs):
        if kwargs.get("progress"):
            kwargs["progress"]("probing")
        raise Interrupted("shutting down")

    monkeypatch.setattr(clip_cli, "clip_source", _interrupting_clip)
    rc_int = clip_cli.main(["--batch", str(small_batch), "--json"])
    cap_int = capsys.readouterr()
    assert rc_int == 1
    assert "probing" in cap_int.err
    parsed_int = _assert_strict_json(cap_int.out)
    assert parsed_int["ok"] is False and parsed_int["failed"] == 1 and len(parsed_int["results"]) == 1


def test_channels_cli_subcommand_exit_codes_and_strict_json_contracts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _seed_managed_channels(tmp_path)

    # Non-JSON error exit codes for invalid edits/connect/enable/disable/feed
    assert channels_cli.main(["connect", "default"]) == 1
    assert channels_cli.main(["connect", "missing_slug"]) == 1
    assert channels_cli.main(["set", "tech"]) == 1  # no editable flags supplied
    assert channels_cli.main(["set", "missing_slug", "--name", "New"]) == 1
    assert channels_cli.main(["enable", "tech"]) == 1  # token file does not exist and --force not set
    assert channels_cli.main(["disable", "missing_slug"]) == 1
    assert channels_cli.main(["feed", "missing_slug", "--add", "@mkbhd"]) == 1
    assert channels_cli.main(["feed", "tech", "--add", "not a valid url or handle!!!"]) == 1
    capsys.readouterr()

    # Corrupt JSON file and schema-invalid snapshot on `import --json` exit 1 with strict JSON.
    bad_json_file = tmp_path / "corrupt.json"
    bad_json_file.write_text("{not valid json", encoding="utf-8")
    assert channels_cli.main(["import", str(bad_json_file), "--json"]) == 1
    assert _assert_strict_json(capsys.readouterr().out)["ok"] is False

    bad_schema_file = tmp_path / "bad_schema.json"
    bad_schema_file.write_text(json.dumps({"channels": [{"slug": "error"}]}), encoding="utf-8")
    assert channels_cli.main(["import", str(bad_schema_file), "--dry-run", "--json"]) == 1
    assert _assert_strict_json(capsys.readouterr().out)["ok"] is False

    # `clone --json` error paths (missing source, existing target, reserved slug) exit 1 with strict JSON.
    for bad_clone in (
        ["clone", "missing_src", "tech-copy", "--json"],
        ["clone", "tech", "science", "--json"],
        ["clone", "tech", "default", "--json"],
        ["clone", "tech", "Bad Slug!", "--json"],
    ):
        assert channels_cli.main(bad_clone) == 1
        assert _assert_strict_json(capsys.readouterr().out)["ok"] is False

    # `clone --json` success emits clean JSON without OAuth guidance prose.
    assert channels_cli.main(["clone", "tech", "tech-stage", "--name", "Tech Stage", "--json"]) == 0
    cloned = _assert_strict_json(capsys.readouterr().out)
    assert cloned == {
        "ok": True,
        "slug": "tech-stage",
        "cloned_from": "tech",
        "name": "Tech Stage",
        "enabled": False,
        "feeds": 1,
    }


def test_growth_cli_strict_json_across_reload_pause_unblock_and_doctor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    _seed_managed_channels(tmp_path)

    # Multi-channel `pause` without a channel slug refuses ambiguity with exit 1 & strict JSON.
    assert growth_cli.main(["pause", "--json"]) == 1
    ambig_pause = _assert_strict_json(capsys.readouterr().out)
    assert ambig_pause["ok"] is False and "tech" in ambig_pause["error"] and "science" in ambig_pause["error"]

    # `unblock --json` (list) and `unblock <name> --json` emit strict JSON and exit 0.
    assert growth_cli.main(["unblock", "--json"]) == 0
    assert _assert_strict_json(capsys.readouterr().out) == {"blocks": []}
    assert growth_cli.main(["unblock", "@nobody", "--json"]) == 0
    assert _assert_strict_json(capsys.readouterr().out) == {"ok": True, "name": "@nobody", "lifted": 0}

    # `reload --json` succeeds when channels.json is valid (exit 0) and fails cleanly (exit 1) when broken.
    assert growth_cli.main(["reload", "--json"]) == 0
    rel_ok = _assert_strict_json(capsys.readouterr().out)
    assert rel_ok["ok"] is True and set(rel_ok["active"]) == {"science", "tech"}

    (tmp_path / "channels.json").write_text("{broken json,", encoding="utf-8")
    assert growth_cli.main(["reload", "--json"]) == 1
    rel_err = _assert_strict_json(capsys.readouterr().out)
    assert "error" in rel_err and set(rel_err["active"]) == {"science", "tech"}


def test_doctor_and_final_audit_strict_json_and_exit_code_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    # Isolate doctor.py I/O and port probes for fast, deterministic exit 0 vs exit 1 checks.
    (tmp_path / "content" / "inbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text(f"REVIEW_TOKEN={CANARY_SECRETS['review_token']}\n", encoding="utf-8")
    with sqlite3.connect(str(tmp_path / "doomscroller.db")) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    monkeypatch.setattr(doctor_cli, "_check_port", lambda host, port, timeout=0.3: False)
    monkeypatch.setattr(doctor_cli, "_find_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(
        doctor_cli.shutil,
        "disk_usage",
        lambda _p: DiskUsage(100 * 1024**3, 20 * 1024**3, 80 * 1024**3),
    )

    monkeypatch.setattr(doctor_cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert doctor_cli.main(["--json", "--root", str(tmp_path)]) == 0
    doc_ok = _assert_strict_json(capsys.readouterr().out)
    assert doc_ok["ok"] is True and doc_ok["env"]["review_token_set"] is True

    monkeypatch.setattr(doctor_cli.shutil, "which", lambda name: None)
    assert doctor_cli.main(["--json", "--root", str(tmp_path)]) == 1
    doc_fail = _assert_strict_json(capsys.readouterr().out)
    assert doc_fail["ok"] is False and doc_fail["media_tools"]["ok"] is False

    # final_audit.py --offline --json exits 0 when healthy, exits 1 when surface check fails.
    assert final_audit_cli.main(["--offline", "--json"]) == 0
    aud_ok = _assert_strict_json(capsys.readouterr().out)
    assert aud_ok["all_ok"] is True and "api_health" not in aud_ok

    monkeypatch.setattr(final_audit_cli, "check_surface", lambda root=None: {"doctor_cli": False})
    assert final_audit_cli.main(["--offline", "--json"]) == 1
    aud_fail = _assert_strict_json(capsys.readouterr().out)
    assert aud_fail["all_ok"] is False and aud_fail["surface_ok"] is False


def test_youtube_oauth_and_seed_strict_json_and_validation_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    # youtube_oauth.py --status --json on missing file, malformed JSON, space-separated scope, and valid token.
    missing_tok = tmp_path / "missing_tok.json"
    assert _invoke_cli(monkeypatch, "youtube_oauth", ["--status", "--token", str(missing_tok), "--json"]) == 1
    miss_rep = _assert_strict_json(capsys.readouterr().out)
    assert miss_rep["ok"] is False and miss_rep["permissions"]["exists"] is False

    corrupt_tok = tmp_path / "corrupt_tok.json"
    corrupt_tok.write_text("{broken", encoding="utf-8")
    assert _invoke_cli(monkeypatch, "youtube_oauth", ["--check", "--token", str(corrupt_tok), "--json"]) == 1
    corr_rep = _assert_strict_json(capsys.readouterr().out)
    assert corr_rep["ok"] is False and corr_rep["valid_json"] is False

    partial_tok = tmp_path / "partial_tok.json"
    partial_tok.write_text(
        json.dumps({
            "refresh_token": "1//refresh-secret-qa8",
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret-qa8",
            "scope": READ_SCOPE,
        }),
        encoding="utf-8",
    )
    assert _invoke_cli(monkeypatch, "youtube_oauth", ["--status", "--token", str(partial_tok), "--json"]) == 1
    part_out = capsys.readouterr().out
    assert "1//refresh-secret-qa8" not in part_out and "GOCSPX-secret-qa8" not in part_out
    part_rep = _assert_strict_json(part_out)
    assert part_rep["ok"] is False and part_rep["scopes"] == [READ_SCOPE]

    good_tok = tmp_path / "good_tok.json"
    write_token_file(
        str(good_tok),
        json.dumps({
            "refresh_token": "1//good-refresh-qa8",
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "GOCSPX-good-secret-qa8",
            "scope": " ".join(SCOPES),
            "channel_id": "UC_QA8_CHANNEL",
        }),
    )
    assert _invoke_cli(monkeypatch, "youtube_oauth", ["--status", "--token", str(good_tok), "--json"]) == 0
    good_out = capsys.readouterr().out
    assert "1//good-refresh-qa8" not in good_out and "GOCSPX-good-secret-qa8" not in good_out
    good_rep = _assert_strict_json(good_out)
    assert good_rep["ok"] is True and good_rep["bound_channel_id"] == "UC_QA8_CHANNEL"

    # seed.py --json success across statuses and clean JSON errors on invalid count / status.
    for status_arg in ("pending", "APPROVED", "  transformed  "):
        assert seed_cli.main(["1", "--status", status_arg, "--json"]) == 0
        s_ok = _assert_strict_json(capsys.readouterr().out)
        assert s_ok["ok"] is True and s_ok["status"] == status_arg.strip().lower()

    for bad_seed in (
        ["0", "--json"],
        ["501", "--json"],
        ["2", "--status", "published", "--json"],
    ):
        rc_seed = seed_cli.main(bad_seed)
        cap_seed = capsys.readouterr()
        assert rc_seed == 1
        assert cap_seed.err == ""
        s_err = _assert_strict_json(cap_seed.out)
        assert s_err["ok"] is False and s_err.get("error")
