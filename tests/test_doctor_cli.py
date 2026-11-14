"""TDD suite for scripts/doctor.py preflight CLI and Windows start/stop scripts."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def test_doctor_happy_path_and_never_leaks_secrets(tmp_path, monkeypatch, capsys):
    import scripts.doctor as doctor

    # Prepare an isolated workspace with canary secret values in .env and env vars.
    (tmp_path / "content" / "inbox").mkdir(parents=True)
    (tmp_path / "logs").mkdir(parents=True)
    db_file = tmp_path / "doomscroller.db"
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    canaries = {
        "REVIEW_TOKEN": "canary-review-secret-alpha-991",
        "DEEPGRAM_API_KEY": "canary-deepgram-secret-beta-992",
        "SARVAM_API_KEY": "canary-sarvam-secret-gamma-993",
        "PIXABAY_API_KEY": "",
        "PEXELS_API_KEY": "canary-pexels-secret-delta-994",
        "TENOR_API_KEY": "canary-tenor-secret-eta-997",
        "GIPHY_API_KEY": "",
        "NTFY_TOPIC": "canary-ntfy-topic-epsilon-995",
        "TELEGRAM_BOT_TOKEN": "",
    }
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in canaries.items()) + "\n",
        encoding="utf-8",
    )
    for k in canaries:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "canary-telegram-env-zeta-996")

    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_find_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda _p: DiskUsage(100 * 1024**3, 50 * 1024**3, 50 * 1024**3),
    )

    # Bind two distinct ephemeral localhost listeners to verify live TCP socket probing.
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv_api,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv_rev,
    ):
        srv_api.bind(("127.0.0.1", 0))
        srv_api.listen(4)
        srv_rev.bind(("127.0.0.1", 0))
        srv_rev.listen(4)
        monkeypatch.setenv("DOOMSCROLLER_API_PORT", str(srv_api.getsockname()[1]))
        monkeypatch.setenv("DOOMSCROLLER_REVIEW_PORT", str(srv_rev.getsockname()[1]))

        report = doctor.run_doctor(fix=False, root=tmp_path, env_file=env_file, db_path=db_file)

    assert report["ok"] is True
    assert report["python"]["ok"] is True
    assert report["media_tools"]["ffmpeg"] is True
    assert report["media_tools"]["ffprobe"] is True
    assert report["media_tools"]["ok"] is True
    assert report["tunnel"]["cloudflared"] is True
    assert report["tunnel"]["ok"] is True
    assert report["env"]["env_file_exists"] is True
    assert report["env"]["review_token_set"] is True
    assert report["env"]["optional_keys"] == {
        "DEEPGRAM_API_KEY": True,
        "SARVAM_API_KEY": True,
        "PIXABAY_API_KEY": False,
        "PEXELS_API_KEY": True,
        "TENOR_API_KEY": True,
        "GIPHY_API_KEY": False,
        "NTFY_TOPIC": True,
        "TELEGRAM_BOT_TOKEN": True,
    }
    assert all(isinstance(v, bool) for v in report["env"]["optional_keys"].values())
    assert report["directories"] == {
        "content": True,
        "content_inbox": True,
        "logs": True,
        "ok": True,
    }
    assert report["disk"]["low_space"] is False
    assert report["disk"]["ok"] is True
    assert report["database"]["exists"] is True
    assert report["database"]["quick_check"] == "ok"
    assert report["database"]["ok"] is True
    assert report["ports"]["api"]["listening"] is True
    assert report["ports"]["review"]["listening"] is True
    assert report["ports"]["daemon_running"] is True

    # Verify zero secret leakage in dict JSON as well as CLI human & --json stdout.
    assert doctor.main(["--root", str(tmp_path)]) == 0
    human_out = capsys.readouterr().out
    serialized = json.dumps(report) + "\n" + human_out
    for secret in [v for v in canaries.values() if v] + ["canary-telegram-env-zeta-996"]:
        assert secret not in serialized


def test_doctor_detects_missing_tools_low_disk_corrupt_db_and_old_python(tmp_path, monkeypatch):
    import scripts.doctor as doctor

    (tmp_path / "content" / "inbox").mkdir(parents=True)
    (tmp_path / "logs").mkdir(parents=True)
    env_file = tmp_path / ".env"
    env_file.write_text("REVIEW_TOKEN=placeholder\n", encoding="utf-8")

    corrupt_db = tmp_path / "doomscroller.db"
    corrupt_db.write_bytes(b"corrupt-not-a-sqlite-header" * 16)

    monkeypatch.setattr(doctor, "_python_version_info", lambda: (3, 9, 18))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "_find_cloudflared", lambda: None)
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda _p: DiskUsage(20 * 1024**3, 19 * 1024**3, int(0.75 * 1024**3)),
    )
    monkeypatch.setattr(doctor, "_check_port", lambda host, port, timeout=0.3: False)

    report = doctor.run_doctor(fix=False, root=tmp_path, env_file=env_file, db_path=corrupt_db)
    assert report["ok"] is False
    assert report["python"]["ok"] is False
    assert report["media_tools"]["ffmpeg"] is False
    assert report["media_tools"]["ffprobe"] is False
    assert report["media_tools"]["ok"] is False
    assert report["tunnel"]["provider"] == "none"
    assert report["tunnel"]["ok"] is False
    assert report["disk"]["low_space"] is True
    assert report["disk"]["ok"] is False
    assert report["database"]["exists"] is True
    assert report["database"]["ok"] is False
    assert report["ports"]["daemon_running"] is False
    assert any("2.0" in w or "2 GB" in w or "disk" in w.lower() for w in report["warnings"])


def test_doctor_fix_creates_dirs_bootstraps_env_token_and_initializes_db(tmp_path, monkeypatch, capsys):
    import scripts.doctor as doctor

    for key in ("REVIEW_TOKEN", "DEEPGRAM_API_KEY", "SARVAM_API_KEY", "PIXABAY_API_KEY",
                "PEXELS_API_KEY", "TENOR_API_KEY", "GIPHY_API_KEY", "NTFY_TOPIC", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_find_cloudflared", lambda: "/usr/bin/cloudflared")
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda _p: DiskUsage(100 * 1024**3, 20 * 1024**3, 80 * 1024**3),
    )
    monkeypatch.setattr(doctor, "_check_port", lambda host, port, timeout=0.3: False)

    env_file = tmp_path / ".env"
    db_file = tmp_path / "doomscroller.db"

    # Without --fix, nothing is created on disk.
    before = doctor.run_doctor(fix=False, root=tmp_path, env_file=env_file, db_path=db_file)
    assert before["directories"]["ok"] is False
    assert before["env"]["env_file_exists"] is False
    assert before["env"]["review_token_set"] is False
    assert before["database"]["exists"] is False
    assert not (tmp_path / "content").exists()
    assert not env_file.exists()
    assert not db_file.exists()

    # With --fix, directories, .env + REVIEW_TOKEN, and SQLite schema are bootstrapped safely.
    init_calls = []
    real_init = doctor._init_database

    def spy_init(target_db, *, custom_target):
        init_calls.append(target_db)
        return real_init(target_db, custom_target=custom_target)

    monkeypatch.setattr(doctor, "_init_database", spy_init)

    exit_code = doctor.main([
        "--fix",
        "--json",
        "--root",
        str(tmp_path),
    ])
    out = capsys.readouterr().out
    after = json.loads(out)

    assert exit_code == 0
    assert after["ok"] is True
    assert after["directories"] == {"content": True, "content_inbox": True, "logs": True, "ok": True}
    assert after["env"]["env_file_exists"] is True
    assert after["env"]["review_token_set"] is True
    assert after["database"]["exists"] is True
    assert after["database"]["quick_check"] == "ok"
    assert after["database"]["ok"] is True
    assert after["fixes_applied"]
    assert len(init_calls) == 1

    # Generated REVIEW_TOKEN must exist in .env and NEVER appear in stdout JSON.
    env_text = env_file.read_text(encoding="utf-8")
    token_line = [ln for ln in env_text.splitlines() if ln.startswith("REVIEW_TOKEN=")]
    assert len(token_line) == 1
    generated_token = token_line[0].split("=", 1)[1].strip()
    assert len(generated_token) >= 16
    assert generated_token not in out

    # Running --fix again is idempotent and preserves the generated REVIEW_TOKEN.
    again = doctor.run_doctor(fix=True, root=tmp_path, env_file=env_file, db_path=db_file)
    assert again["ok"] is True
    assert f"REVIEW_TOKEN={generated_token}" in env_file.read_text(encoding="utf-8")


def test_doctor_cli_subprocess_json_and_zero_heavy_imports():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, scripts.doctor as d; "
                "assert 'numpy' not in sys.modules and 'cv2' not in sys.modules"
            ),
        ],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    cli = subprocess.run(
        [sys.executable, "scripts/doctor.py", "--json"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli.returncode in (0, 1), cli.stderr
    payload = json.loads(cli.stdout)
    for key in ("ok", "python", "media_tools", "tunnel", "env", "directories", "disk", "database", "ports"):
        assert key in payload


def test_windows_start_stop_scripts_quote_paths_with_spaces(tmp_path):
    stop_ps1 = PROJECT_ROOT / "stop.ps1"
    stop_bat = PROJECT_ROOT / "stop.bat"
    start_bat = PROJECT_ROOT / "start.bat"
    start_ps1 = PROJECT_ROOT / "start.ps1"

    assert stop_ps1.is_file(), "stop.ps1 must exist"
    assert stop_bat.is_file()
    assert start_bat.is_file()
    assert start_ps1.is_file()

    stop_ps1_text = stop_ps1.read_text(encoding="utf-8")
    start_ps1_text = start_ps1.read_text(encoding="utf-8")
    start_bat_text = start_bat.read_text(encoding="utf-8")
    stop_bat_text = stop_bat.read_text(encoding="utf-8")

    # Verify scripts reference run.py --stop and use path-safe quoting / LiteralPath.
    assert "--stop" in stop_ps1_text and "run.py" in stop_ps1_text
    assert "LiteralPath" in stop_ps1_text
    assert "LiteralPath" in start_ps1_text
    assert '"venv\\Scripts\\python.exe"' in start_bat_text or '"%~dp0venv\\Scripts\\python.exe"' in start_bat_text
    assert '"venv\\Scripts\\python.exe"' in stop_bat_text or '"%~dp0venv\\Scripts\\python.exe"' in stop_bat_text

    if os.name == "nt":
        spaced_root = tmp_path / "My Codes With Spaces" / "doomscroller app"
        (spaced_root / "scripts").mkdir(parents=True)
        marker = spaced_root / "stopped_args.json"
        (spaced_root / "scripts" / "run.py").write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "Path(__file__).resolve().parent.parent.joinpath('stopped_args.json').write_text(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        shutil.copy2(stop_bat, spaced_root / "stop.bat")
        shutil.copy2(stop_ps1, spaced_root / "stop.ps1")

        # 1. Execute stop.bat inside a directory with spaces.
        bat_proc = subprocess.run(
            ["cmd.exe", "/c", str(spaced_root / "stop.bat"), "--no-pause"],
            cwd=spaced_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert bat_proc.returncode == 0, bat_proc.stderr + bat_proc.stdout
        assert json.loads(marker.read_text(encoding="utf-8")) == ["--stop"]

        marker.unlink()

        # 2. Execute stop.ps1 inside a directory with spaces.
        ps_proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(spaced_root / "stop.ps1"),
                "-NoPause",
            ],
            cwd=spaced_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert ps_proc.returncode == 0, ps_proc.stderr + ps_proc.stdout
        assert json.loads(marker.read_text(encoding="utf-8")) == ["--stop"]

        # 3. Parse start.ps1 and stop.ps1 with PowerShell's AST parser to guarantee zero syntax errors.
        parse_proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$errs = $null; "
                    f"[void][System.Management.Automation.Language.Parser]::ParseFile('{start_ps1}', [ref]$null, [ref]$errs); "
                    "if ($errs.Count -gt 0) { Write-Error ($errs | Out-String); exit 1 }; "
                    f"[void][System.Management.Automation.Language.Parser]::ParseFile('{stop_ps1}', [ref]$null, [ref]$errs); "
                    "if ($errs.Count -gt 0) { Write-Error ($errs | Out-String); exit 1 }"
                ),
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert parse_proc.returncode == 0, parse_proc.stderr + parse_proc.stdout

        # 4. Verify start.bat's `for /f "usebackq delims="` quoting works with spaces in %~dp0.
        (spaced_root / "venv" / "Scripts").mkdir(parents=True)
        shutil.copy2(sys.executable, spaced_root / "venv" / "Scripts" / "python.exe")
        # Copy pyvenv.cfg if venv python needs it on Windows
        src_cfg = Path(sys.executable).resolve().parent.parent / "pyvenv.cfg"
        if src_cfg.is_file():
            shutil.copy2(src_cfg, spaced_root / "venv" / "pyvenv.cfg")
        (spaced_root / "scripts" / "run.py").write_text(
            "import sys\n"
            "if '--panel-url' in sys.argv:\n"
            "    print('http://localhost:8600/?key=test_ok')\n",
            encoding="utf-8",
        )
        for_line = [ln for ln in start_bat_text.splitlines() if ln.strip().startswith("for /f")][0]
        probe_bat = spaced_root / "probe_for.bat"
        probe_bat.write_text(f"@echo off\r\nsetlocal\r\n{for_line}\r\necho %PANEL_URL%\r\n", encoding="utf-8")
        for_proc = subprocess.run(
            ["cmd.exe", "/c", str(probe_bat)],
            cwd=spaced_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert for_proc.returncode == 0, for_proc.stderr + for_proc.stdout
        assert for_proc.stdout.strip() == "http://localhost:8600/?key=test_ok"
