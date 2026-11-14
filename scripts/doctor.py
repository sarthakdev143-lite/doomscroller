"""Offline, fast, cross-platform system preflight inspector for Doomscroller.

Checks Python runtime, ffmpeg/ffprobe media binaries, phone tunnel binaries,
``.env`` & ``REVIEW_TOKEN`` + optional API key presence (boolean ONLY — never
secret values), required runtime directories, free disk space, SQLite database
integrity (``PRAGMA quick_check``), and live daemon TCP ports.

Usage::

    python scripts/doctor.py            # human-readable preflight summary
    python scripts/doctor.py --json     # machine-readable JSON report
    python scripts/doctor.py --fix      # create missing dirs, bootstrap .env/token, init DB
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MIN_PYTHON = (3, 10)
WARN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
OPTIONAL_SECRET_KEYS: tuple[str, ...] = (
    "DEEPGRAM_API_KEY",
    "SARVAM_API_KEY",
    "PIXABAY_API_KEY",
    "PEXELS_API_KEY",
    "TENOR_API_KEY",
    "GIPHY_API_KEY",
    "FREESOUND_API_KEY",
    "NTFY_TOPIC",
    "TELEGRAM_BOT_TOKEN",
)
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _python_version_info() -> tuple[int, int, int]:
    info = sys.version_info
    return int(info[0]), int(info[1]), int(info[2])


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    # Strip inline unquoted comments (e.g. KEY=  # comment)
    if "#" in value and not value.startswith(("'", '"')):
        for idx, ch in enumerate(value):
            if ch == "#" and (idx == 0 or value[idx - 1].isspace()):
                return value[:idx].strip()
    return value


def _parse_dotenv(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _ENV_LINE_RE.match(line)
            if not match:
                continue
            key, raw_val = match.group(1), match.group(2)
            parsed[key] = _strip_quotes(raw_val.strip()).strip()
    except OSError:
        return {}
    return parsed


def _env_port(name: str, default: int, dotenv_vals: dict[str, str]) -> int:
    raw = (os.environ.get(name, "").strip() or dotenv_vals.get(name, "").strip())
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
    return default


def _find_cloudflared() -> str | None:
    from scripts.tunnel import find_binary

    return find_binary()


def _check_port(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def _resolve_env_path(root: Path, env_file: Path | None, *, custom_root: bool) -> Path | None:
    if env_file is not None:
        return Path(env_file)
    if custom_root:
        return root / ".env"
    if "DOOMSCROLLER_ENV_FILE" in os.environ:
        val = os.environ["DOOMSCROLLER_ENV_FILE"].strip()
        return Path(val) if val else None
    return root / ".env"


def _resolve_db_path(
    root: Path,
    db_path: Path | None,
    dotenv_vals: dict[str, str],
    *,
    custom_root: bool,
) -> Path | None:
    if db_path is not None:
        return Path(db_path)
    if custom_root:
        return root / "doomscroller.db"
    raw_url = (os.environ.get("DATABASE_URL", "").strip() or dotenv_vals.get("DATABASE_URL", "").strip())
    if not raw_url:
        return root / "doomscroller.db"
    lowered = raw_url.lower()
    if ":memory:" in lowered:
        return None
    if lowered.startswith("sqlite:///"):
        file_part = raw_url[len("sqlite:///"):].split("?", 1)[0]
        if len(file_part) >= 3 and file_part[0] == "/" and file_part[1].isalpha() and file_part[2] == ":":
            file_part = file_part[1:]
        candidate = Path(file_part)
        return candidate if candidate.is_absolute() else (root / candidate)
    if "://" not in raw_url:
        candidate = Path(raw_url)
        return candidate if candidate.is_absolute() else (root / candidate)
    return None


def _init_database(target_db: Path | None, *, custom_target: bool) -> None:
    if not custom_target:
        from app.db import init_db

        init_db()
        return
    if target_db is None:
        return
    from app.db import create_engine_for_url
    from app.models import Base

    eng = create_engine_for_url(f"sqlite:///{target_db.resolve().as_posix()}")
    try:
        Base.metadata.create_all(bind=eng)
    finally:
        eng.dispose()


def _sqlite_quick_check(target_db: Path | None) -> tuple[bool, str, bool]:
    """Return (exists, quick_check_status, ok)."""
    if target_db is None:
        return False, "non_file_url", True
    if not target_db.is_file():
        return False, "missing", True
    try:
        with closing(sqlite3.connect(str(target_db), timeout=5.0)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
        if rows == ["ok"]:
            return True, "ok", True
        detail = "; ".join(rows)[:200] if rows else "empty"
        return True, detail, False
    except sqlite3.Error as exc:
        return True, f"error: {exc}", False


def _nearest_existing_dir(target: Path) -> Path:
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe if probe.exists() else Path.cwd()


def run_doctor(
    *,
    fix: bool = False,
    root: Path | None = None,
    env_file: Path | None = None,
    db_path: Path | None = None,
) -> dict:
    """Run offline system preflight checks and return a structured report.

    Reports **boolean presence ONLY** for ``REVIEW_TOKEN`` and optional secret
    keys — secret values are never included in the returned dictionary.
    """
    custom_root = root is not None
    base_root = Path(root).resolve() if root is not None else PROJECT_ROOT
    target_env = _resolve_env_path(base_root, env_file, custom_root=custom_root)
    dotenv_vals = _parse_dotenv(target_env)

    # Resolve directories without creating them unless fix=True.
    if custom_root:
        content_dir = base_root / "content"
        inbox_dir = content_dir / "inbox"
    else:
        raw_content = os.environ.get("CONTENT_DIR", "").strip() or dotenv_vals.get("CONTENT_DIR", "").strip() or "./content"
        content_dir = Path(raw_content)
        if not content_dir.is_absolute():
            content_dir = base_root / content_dir
        raw_inbox = os.environ.get("GROWTH_INBOX_DIR", "").strip() or dotenv_vals.get("GROWTH_INBOX_DIR", "").strip()
        inbox_dir = Path(raw_inbox) if raw_inbox else (content_dir / "inbox")
        if not inbox_dir.is_absolute():
            inbox_dir = base_root / inbox_dir
    logs_dir = base_root / "logs"

    target_db = _resolve_db_path(base_root, db_path, dotenv_vals, custom_root=custom_root)
    fixes_applied: list[str] = []
    warnings: list[str] = []

    if fix:
        for label, dpath in (
            ("content/", content_dir),
            ("content/inbox/", inbox_dir),
            ("logs/", logs_dir),
        ):
            if not dpath.is_dir():
                dpath.mkdir(parents=True, exist_ok=True)
                fixes_applied.append(f"created {label}")

        if target_env is not None:
            from scripts.run import ensure_env_and_review_token

            existed_before = target_env.is_file()
            example_path = (base_root / ".env.example") if (base_root / ".env.example").is_file() else (PROJECT_ROOT / ".env.example")
            _, created_token = ensure_env_and_review_token(env_path=target_env, example_path=example_path)
            if not existed_before:
                fixes_applied.append("created .env")
            if created_token:
                fixes_applied.append("generated REVIEW_TOKEN")
            dotenv_vals = _parse_dotenv(target_env)

        db_existed = bool(target_db and target_db.is_file())
        try:
            _init_database(target_db, custom_target=(custom_root or db_path is not None))
            if not db_existed and target_db is not None and target_db.is_file():
                fixes_applied.append("initialized database")
            elif db_existed:
                fixes_applied.append("verified database schema")
        except Exception as exc:
            warnings.append(f"database init failed: {type(exc).__name__}")

    # 1. Python version check (>=3.10)
    major, minor, micro = _python_version_info()
    py_version = f"{major}.{minor}.{micro}"
    py_ok = (major, minor) >= MIN_PYTHON
    if not py_ok:
        warnings.append(f"Python {py_version} is below minimum {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")

    # 2. Media tools (ffmpeg & ffprobe)
    ffmpeg_found = bool(shutil.which("ffmpeg"))
    ffprobe_found = bool(shutil.which("ffprobe"))
    media_ok = ffmpeg_found and ffprobe_found
    if not ffmpeg_found:
        warnings.append("ffmpeg not found on PATH")
    if not ffprobe_found:
        warnings.append("ffprobe not found on PATH")

    # 3. Tunnel binary availability (cloudflared or ssh)
    cloudflared_found = bool(_find_cloudflared())
    ssh_found = bool(shutil.which("ssh"))
    tunnel_provider = "cloudflared" if cloudflared_found else ("ssh" if ssh_found else "none")
    tunnel_ok = cloudflared_found or ssh_found
    if not tunnel_ok:
        warnings.append("no tunnel binary found (install cloudflared or OpenSSH for phone access)")

    # 4. .env & REVIEW_TOKEN + optional API keys (BOOLEAN PRESENCE ONLY — NEVER VALUES)
    def _has_key(key: str) -> bool:
        return bool(os.environ.get(key, "").strip() or dotenv_vals.get(key, "").strip())

    env_exists = bool(target_env is not None and target_env.is_file())
    review_token_set = _has_key("REVIEW_TOKEN")
    optional_keys = {key: _has_key(key) for key in OPTIONAL_SECRET_KEYS}
    if not env_exists:
        warnings.append(".env file is missing (run with --fix to bootstrap)")
    if not review_token_set:
        warnings.append("REVIEW_TOKEN is not set (run with --fix to generate)")

    # 5. Directories & free disk space
    content_ok = content_dir.is_dir()
    inbox_ok = inbox_dir.is_dir()
    logs_ok = logs_dir.is_dir()
    dirs_ok = content_ok and inbox_ok and logs_ok
    if not dirs_ok:
        missing_dirs = [
            name
            for name, present in (
                ("content/", content_ok),
                ("content/inbox/", inbox_ok),
                ("logs/", logs_ok),
            )
            if not present
        ]
        warnings.append(f"missing directories: {', '.join(missing_dirs)} (run with --fix)")

    usage = shutil.disk_usage(_nearest_existing_dir(content_dir))
    free_bytes = int(usage.free)
    free_gb = round(free_bytes / (1024 ** 3), 2)
    low_space = free_bytes < WARN_FREE_BYTES
    if low_space:
        warnings.append(f"low free disk space: {free_gb:.2f} GB (< 2.0 GB)")

    # 6. SQLite DB quick_check
    db_exists, db_quick_check, db_ok = _sqlite_quick_check(target_db)
    if not db_exists and target_db is not None:
        warnings.append("SQLite database not initialized yet (run with --fix or scripts/init_db.py)")
    elif not db_ok:
        warnings.append(f"SQLite quick_check failed: {db_quick_check}")

    # 7. Live daemon ports (127.0.0.1:8000 & 127.0.0.1:8600)
    api_port = _env_port("DOOMSCROLLER_API_PORT", 8000, dotenv_vals)
    review_port = _env_port("DOOMSCROLLER_REVIEW_PORT", 8600, dotenv_vals)
    api_listening = _check_port("127.0.0.1", api_port)
    review_listening = _check_port("127.0.0.1", review_port)

    overall_ok = bool(py_ok and media_ok and db_ok)

    return {
        "ok": overall_ok,
        "python": {
            "version": py_version,
            "ok": py_ok,
        },
        "media_tools": {
            "ffmpeg": ffmpeg_found,
            "ffprobe": ffprobe_found,
            "ok": media_ok,
        },
        "tunnel": {
            "cloudflared": cloudflared_found,
            "ssh": ssh_found,
            "provider": tunnel_provider,
            "ok": tunnel_ok,
        },
        "env": {
            "env_file_exists": env_exists,
            "review_token_set": review_token_set,
            "optional_keys": optional_keys,
        },
        "directories": {
            "content": content_ok,
            "content_inbox": inbox_ok,
            "logs": logs_ok,
            "ok": dirs_ok,
        },
        "disk": {
            "free_bytes": free_bytes,
            "free_gb": free_gb,
            "warn_below_gb": 2.0,
            "low_space": low_space,
            "ok": not low_space,
        },
        "database": {
            "exists": db_exists,
            "quick_check": db_quick_check,
            "ok": db_ok,
        },
        "ports": {
            "api": {"host": "127.0.0.1", "port": api_port, "listening": api_listening},
            "review": {"host": "127.0.0.1", "port": review_port, "listening": review_listening},
            "daemon_running": bool(api_listening and review_listening),
        },
        "fixes_applied": fixes_applied,
        "warnings": warnings,
    }


def _print_human(report: dict) -> None:
    def _tag(ok: bool, *, warn: bool = False) -> str:
        if ok:
            return "[OK]  "
        return "[WARN]" if warn else "[FAIL]"

    py = report["python"]
    media = report["media_tools"]
    tun = report["tunnel"]
    env = report["env"]
    dirs = report["directories"]
    disk = report["disk"]
    db = report["database"]
    ports = report["ports"]

    print("=== Doomscroller Preflight Doctor ===")
    print(f"  {_tag(py['ok'])} Python       : {py['version']} (requires >=3.10)")
    print(
        f"  {_tag(media['ok'])} Media tools  : "
        f"ffmpeg={'yes' if media['ffmpeg'] else 'MISSING'}, "
        f"ffprobe={'yes' if media['ffprobe'] else 'MISSING'}"
    )
    print(
        f"  {_tag(tun['ok'], warn=True)} Phone tunnel : provider={tun['provider']} "
        f"(cloudflared={'yes' if tun['cloudflared'] else 'no'}, ssh={'yes' if tun['ssh'] else 'no'})"
    )
    configured_opt = [k for k, v in env["optional_keys"].items() if v]
    print(
        f"  {_tag(env['env_file_exists'] and env['review_token_set'], warn=True)} Environment  : "
        f".env={'present' if env['env_file_exists'] else 'missing'}, "
        f"REVIEW_TOKEN={'set' if env['review_token_set'] else 'unset'}, "
        f"optional_keys={len(configured_opt)}/{len(env['optional_keys'])} set"
    )
    for key, is_set in env["optional_keys"].items():
        print(f"           - {key:<20}: {'set' if is_set else 'unset'}")
    print(
        f"  {_tag(dirs['ok'], warn=True)} Directories  : "
        f"content={'yes' if dirs['content'] else 'no'}, "
        f"inbox={'yes' if dirs['content_inbox'] else 'no'}, "
        f"logs={'yes' if dirs['logs'] else 'no'}"
    )
    print(f"  {_tag(disk['ok'], warn=True)} Disk space   : {disk['free_gb']:.2f} GB free")
    print(
        f"  {_tag(db['ok'])} SQLite DB    : "
        f"{'present' if db['exists'] else 'missing'} (quick_check={db['quick_check']})"
    )
    api_p, rev_p = ports["api"], ports["review"]
    print(
        f"  [INFO] Daemon ports : API {api_p['port']}={'listening' if api_p['listening'] else 'free'}, "
        f"Panel {rev_p['port']}={'listening' if rev_p['listening'] else 'free'}"
    )
    if report["fixes_applied"]:
        print(f"\nFixes applied: {', '.join(report['fixes_applied'])}")
    if report["warnings"]:
        print("\nWarnings:")
        for item in report["warnings"]:
            print(f"  - {item}")
    print(f"\nOverall preflight: {'PASS' if report['ok'] else 'FAIL'}")


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Doomscroller offline system preflight inspector")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output")
    parser.add_argument("--fix", action="store_true", help="Create missing directories, .env/REVIEW_TOKEN, and initialize DB")
    parser.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run_doctor(fix=args.fix, root=args.root)
    if args.json:
        print(json.dumps(report, indent=2, allow_nan=False))
    else:
        _print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
