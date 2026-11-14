"""One-command launcher for Doomscroller (local autonomous mode).

Starts a single low-memory runtime (``scripts/serve_slim.py``) hosting:
  - FastAPI backend + in-process APScheduler on 127.0.0.1:8000
  - Authenticated desktop & phone web control panel on 127.0.0.1:8600
  - Supervised phone quick tunnel on Windows (on Linux, ``install_services.sh``
    manages ``doomscroller-tunnel.service`` separately)

Usage::

    python scripts/run.py            # foreground, logs to console
    python scripts/run.py --daemon   # background, logs to logs/api.log
    python scripts/run.py --ensure   # idempotent watchdog check/start
    python scripts/run.py --status   # is it running?
    python scripts/run.py --stop     # stop a running daemon
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
PID_FILE = PROJECT_ROOT / "doomscroller.pids"
ENV_FILE = PROJECT_ROOT / ".env"
EXAMPLE_ENV_FILE = PROJECT_ROOT / ".env.example"
PYTHON = sys.executable
IS_WINDOWS = os.name == "nt"

def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
    return default


API_PORT = _env_port("DOOMSCROLLER_API_PORT", 8000)
REVIEW_PORT = _env_port("DOOMSCROLLER_REVIEW_PORT", 8600)
HOST = os.environ.get("DOOMSCROLLER_HOST", "127.0.0.1").strip() or "127.0.0.1"

SLIM_CMD = [PYTHON, "scripts/serve_slim.py"]
PORTS = (("API", API_PORT), ("Review", REVIEW_PORT))
_REVIEW_TOKEN_RE = re.compile(r"^\s*REVIEW_TOKEN\s*=\s*(.*?)\s*$")
_DAEMON_CMD_MARKERS = ("serve_slim", "doomscroller", "app.main", "app.review")


def _configured_env_path() -> Path | None:
    if "DOOMSCROLLER_ENV_FILE" in os.environ:
        val = os.environ["DOOMSCROLLER_ENV_FILE"].strip()
        return Path(val) if val else None
    return ENV_FILE


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def read_review_token(env_path: Path | None = None) -> str:
    """Return REVIEW_TOKEN from environment or ``.env`` without importing Settings."""
    env_override = os.environ.get("REVIEW_TOKEN", "").strip()
    if env_override:
        return env_override
    target = _configured_env_path() if env_path is None else env_path
    if target is None or not target.is_file():
        return ""
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            m = _REVIEW_TOKEN_RE.match(line)
            if m:
                val = _strip_quotes(m.group(1).strip())
                if val:
                    return val
    except OSError:
        return ""
    return ""


def ensure_env_and_review_token(
    env_path: Path | None = None,
    example_path: Path | None = None,
) -> tuple[str, bool]:
    """Ensure ``.env`` exists and has a non-empty ``REVIEW_TOKEN``.

    Returns ``(token, created)`` where ``created`` is True only when a new token
    was generated and written. Respects ``DOOMSCROLLER_ENV_FILE=""`` test isolation.
    """
    target = _configured_env_path() if env_path is None else env_path
    if target is None:
        return os.environ.get("REVIEW_TOKEN", "").strip(), False

    template = EXAMPLE_ENV_FILE if example_path is None else example_path
    existing_token = read_review_token(target)
    if target.is_file() and existing_token:
        return existing_token, False

    raw = ""
    existed = target.is_file()
    if existed:
        raw = target.read_text(encoding="utf-8")
    elif template is not None and template.is_file():
        raw = template.read_text(encoding="utf-8")

    if existing_token:
        # Process env supplied REVIEW_TOKEN while .env lacked it; still ensure .env exists cleanly.
        if not existed:
            _atomic_write_private(target, raw)
        return existing_token, False

    token = secrets.token_urlsafe(24)
    lines = raw.splitlines()
    replaced = False
    for idx, line in enumerate(lines):
        if _REVIEW_TOKEN_RE.match(line):
            lines[idx] = f"REVIEW_TOKEN={token}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"REVIEW_TOKEN={token}")
    updated = "\n".join(lines) + "\n"
    _atomic_write_private(target, updated)
    return token, True


def _atomic_write_private(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(contents, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def local_panel_url(env_path: Path | None = None, port: int | None = None) -> str:
    """Localhost control-panel URL, pre-populated with ``?key=`` when available."""
    p = REVIEW_PORT if port is None else port
    token = read_review_token(env_path)
    if token:
        return f"http://localhost:{p}/?key={urllib.parse.quote(token, safe='')}"
    return f"http://localhost:{p}"


def _has_tunnel_provider() -> bool:
    from scripts.tunnel import find_binary

    return bool(find_binary() or shutil.which("ssh"))


# ---------------------------------------------------------------------------
# Process probing / killing
# ---------------------------------------------------------------------------
def _cmdline_matches_daemon(cmdline: str) -> bool:
    norm = cmdline.strip().lower().replace("\\", "/")
    return any(marker in norm for marker in _DAEMON_CMD_MARKERS)


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live Doomscroller daemon process. Never kills anything."""
    if pid <= 0 or pid == os.getpid():
        return False
    if IS_WINDOWS:
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
                creationflags=no_win,
            ).stdout
        except Exception:
            return False
        image_name = ""
        for row in csv.reader((out or "").splitlines()):
            if len(row) >= 2 and row[1].strip() == str(pid):
                image_name = row[0].strip().lower()
                break
        if not image_name or not image_name.startswith(("python", "py.exe", "uvicorn")):
            return False
        # Verify command line so a recycled Windows PID reused by another Python process is never killed.
        try:
            ps = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction SilentlyContinue).CommandLine",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=8,
                creationflags=no_win,
            )
            cmdline = (ps.stdout or "").strip() if ps.returncode == 0 else ""
            if cmdline:
                return _cmdline_matches_daemon(cmdline)
        except Exception:
            pass
        return True
    try:
        os.kill(pid, 0)  # POSIX: signal 0 probes without delivering
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    proc_cmd = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_cmd.is_file():
            raw = proc_cmd.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
            if raw:
                return _cmdline_matches_daemon(raw)
    except OSError:
        pass
    return True


def _kill(pid: int) -> bool:
    """Terminate ``pid`` (and its children on Windows). Returns True if it died."""
    if not _pid_alive(pid):
        return False
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
    for _ in range(20):
        if not _pid_alive(pid):
            return True
        time.sleep(0.25)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


def _read_pids() -> dict[str, int]:
    if not PID_FILE.exists():
        return {}
    try:
        data = json.loads(PID_FILE.read_text())
        return {k: int(v) for k, v in data.items()}
    except Exception:
        return {}


def _write_pids(pids: dict[str, int]) -> None:
    _atomic_write_private(PID_FILE, json.dumps(pids, indent=2) + "\n")


def _running() -> dict[str, int]:
    """Return only the recorded PIDs that are actually alive."""
    return {name: pid for name, pid in _read_pids().items() if _pid_alive(pid)}


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _status() -> int:
    alive = _running()
    if alive:
        for name, pid in alive.items():
            print(f"{name:<10} running (PID {pid})")
    else:
        print("Not running.")
    for label, port in PORTS:
        print(f"{label:<10} port {port}: {'listening' if _port_in_use(port) else 'free'}")
    if alive:
        print(f"Panel      url: {local_panel_url()}")
    tunnel_url_file = LOG_DIR / "tunnel_url.txt"
    if alive and tunnel_url_file.is_file():
        url = tunnel_url_file.read_text(encoding="utf-8").strip()
        if url:
            print(f"Tunnel     url: {url}")
    return 0 if alive else 1


def _stop() -> int:
    pids = _read_pids()
    if not pids:
        print("No PID file; nothing to stop.")
        return 0
    stopped = [name for name, pid in pids.items() if _kill(pid)]
    PID_FILE.unlink(missing_ok=True)
    print(f"Stopped: {', '.join(stopped) if stopped else 'nothing was running'}")
    return 0


def _spawn(cmd: list[str], log_path: Path) -> subprocess.Popen:
    """Start a detached child writing to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log_fh:
        kwargs: dict = {
            "cwd": PROJECT_ROOT,
            "stdout": log_fh,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            base_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            if breakaway:
                kwargs["creationflags"] = base_flags | breakaway
                try:
                    return subprocess.Popen(cmd, **kwargs)
                except (PermissionError, OSError):
                    pass
            kwargs["creationflags"] = base_flags
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(cmd, **kwargs)


def _wait_tunnel_url(tunnel_url_file: Path, attempts: int = 40) -> str:
    for _ in range(attempts):
        if tunnel_url_file.is_file():
            url = tunnel_url_file.read_text(encoding="utf-8").strip()
            if url:
                return url
        time.sleep(0.25)
    return ""


def _start(daemon: bool, slim: bool = True) -> int:
    del slim  # Kept for CLI/test compatibility; unified single-process runtime is now the sole mode.
    alive = _running()
    if alive:
        print(f"Already running: {alive}. Stop it first with --stop.")
        return 1
    for label, port in PORTS:
        if _port_in_use(port):
            print(f"[ERROR] {label} port {port} is already in use.")
            return 1

    _, created_token = ensure_env_and_review_token()
    if created_token:
        print("[setup] Initialized .env with a fresh REVIEW_TOKEN for the Control Panel.")
    panel_url = local_panel_url()

    with_tunnel = IS_WINDOWS and _has_tunnel_provider()
    tunnel_url_file = LOG_DIR / "tunnel_url.txt"
    if with_tunnel:
        tunnel_url_file.unlink(missing_ok=True)

    if daemon:
        api_log = LOG_DIR / "api.log"
        print(f"Starting Doomscroller daemon. Log: {api_log}")
        proc = _spawn(SLIM_CMD, api_log)
        _write_pids({"slim": proc.pid})
        print(f"API:       http://localhost:{API_PORT}  (PID {proc.pid})")
        print(f"Panel:     {panel_url}  (PID {proc.pid}, shared runtime)")
        if with_tunnel:
            url = _wait_tunnel_url(tunnel_url_file)
            label = url or "(connecting -- check logs/tunnel.log)"
            print(f"Tunnel:    {label}")
        print("Stop with: python scripts/run.py --stop")
        return 0

    print(f"Starting Doomscroller in foreground. Panel: {panel_url}")
    print("Press Ctrl+C to stop.")
    proc = subprocess.Popen(SLIM_CMD, cwd=PROJECT_ROOT)
    _write_pids({"slim": proc.pid})
    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        return 0
    finally:
        PID_FILE.unlink(missing_ok=True)


def _ensure(slim: bool = True) -> int:
    """Idempotent watchdog entry point: do nothing if healthy, otherwise clean up and start."""
    alive = _running()
    if alive and all(_port_in_use(port) for _, port in PORTS):
        return 0
    for pid in _read_pids().values():
        _kill(pid)
    PID_FILE.unlink(missing_ok=True)
    return _start(daemon=True, slim=slim)


def main() -> int:
    parser = argparse.ArgumentParser(description="Doomscroller launcher")
    parser.add_argument("--daemon", action="store_true", help="Run in background")
    parser.add_argument("--slim", action="store_true", help="Compatibility flag (single-process mode is default)")
    parser.add_argument("--ensure", action="store_true", help="Start in background only if not already healthy")
    parser.add_argument("--stop", action="store_true", help="Stop running daemon")
    parser.add_argument("--status", action="store_true", help="Show status")
    parser.add_argument("--panel-url", action="store_true", help="Ensure REVIEW_TOKEN and print local unlock URL")
    args = parser.parse_args()

    if args.panel_url:
        ensure_env_and_review_token()
        print(local_panel_url())
        return 0
    if args.stop:
        return _stop()
    if args.status:
        return _status()
    if args.ensure:
        return _ensure()
    return _start(daemon=args.daemon)


if __name__ == "__main__":
    sys.exit(main())
