"""One-command launcher for Doomscroller (local autonomous mode).

Starts:
  - FastAPI backend (with APScheduler running in-process) on port 8000
  - Streamlit dashboard on port 8501

Usage::

    python scripts/run.py            # foreground, logs to console
    python scripts/run.py --daemon   # background, logs to logs/
    python scripts/run.py --status   # is it running?
    python scripts/run.py --stop     # stop a running daemon

No Docker, no Redis, no Celery required. The scheduler runs inside the FastAPI
process and triggers scrape/approve/transform/publish/cleanup jobs on
configurable intervals.

Process control is deliberately platform-split. On POSIX we manage a process
group with ``os.killpg``. Neither ``os.killpg`` nor ``os.getpgid`` exists on
Windows, and ``os.kill(pid, 0)`` there does not probe a process -- it calls
``TerminateProcess``, so the obvious liveness check would kill the very server
it was asked about. Windows therefore goes through ``tasklist``/``taskkill``.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
PID_FILE = PROJECT_ROOT / "doomscroller.pids"
PYTHON = sys.executable
IS_WINDOWS = os.name == "nt"

API_PORT = int(os.environ.get("DOOMSCROLLER_API_PORT", "8000"))
DASHBOARD_PORT = int(os.environ.get("DOOMSCROLLER_DASHBOARD_PORT", "8501"))

API_CMD = [
    PYTHON, "-m", "uvicorn", "app.main:app",
    "--host", "0.0.0.0", "--port", str(API_PORT),
]
DASHBOARD_CMD = [
    PYTHON, "-m", "streamlit", "run", "dashboard/app.py",
    "--server.port", str(DASHBOARD_PORT),
    "--server.headless", "true",
]


# ---------------------------------------------------------------------------
# Process probing / killing
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process. Never kills anything."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except Exception:
            return False
        # tasklist prints a "no tasks" notice rather than failing.
        if f'"{pid}"' not in out:
            return False
        # Windows recycles PIDs, so a stale entry can alias an unrelated
        # process started later (observed: a dead dashboard's PID got
        # reassigned to msedgewebview2.exe, which --stop would then kill).
        # Both our processes are launched via `PYTHON -m ...`, so require
        # the image name to actually be a python interpreter.
        image_name = out.split(",", 1)[0].strip('"').lower()
        return image_name.startswith("python")
    try:
        os.kill(pid, 0)  # POSIX: signal 0 probes without delivering
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # exists, owned by someone else


def _kill(pid: int) -> bool:
    """Terminate ``pid`` (and its children on Windows). Returns True if it died."""
    if not _pid_alive(pid):
        return False
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True,
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
    PID_FILE.write_text(json.dumps(pids, indent=2))


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
    for label, port in (("API", API_PORT), ("Dashboard", DASHBOARD_PORT)):
        print(f"{label:<10} port {port}: {'listening' if _port_in_use(port) else 'free'}")
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
    kwargs: dict = {
        "cwd": PROJECT_ROOT,
        "stdout": open(log_path, "ab"),
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        # Own process group, so taskkill /T can reap the whole tree.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _start(daemon: bool) -> int:
    alive = _running()
    if alive:
        print(f"Already running: {alive}. Stop it first with --stop.")
        return 1
    for label, port in (("API", API_PORT), ("Dashboard", DASHBOARD_PORT)):
        if _port_in_use(port):
            print(f"[ERROR] {label} port {port} is already in use.")
            return 1

    if daemon:
        api_log, dash_log = LOG_DIR / "api.log", LOG_DIR / "dashboard.log"
        print(f"Starting in daemon mode. Logs: {api_log}, {dash_log}")
        api = _spawn(API_CMD, api_log)
        time.sleep(2)  # let the API bind before the dashboard calls it
        dashboard = _spawn(DASHBOARD_CMD, dash_log)
        _write_pids({"api": api.pid, "dashboard": dashboard.pid})
        print(f"API:       http://localhost:{API_PORT}  (PID {api.pid})")
        print(f"Dashboard: http://localhost:{DASHBOARD_PORT}  (PID {dashboard.pid})")
        print("Stop with: python scripts/run.py --stop")
        return 0

    print("Starting Doomscroller in foreground. Press Ctrl+C to stop.")
    print(f"API:       http://localhost:{API_PORT}")
    print(f"Dashboard: http://localhost:{DASHBOARD_PORT}")
    print("-" * 60)

    api = subprocess.Popen(API_CMD, cwd=PROJECT_ROOT)
    time.sleep(2)
    dashboard = subprocess.Popen(DASHBOARD_CMD, cwd=PROJECT_ROOT)
    _write_pids({"api": api.pid, "dashboard": dashboard.pid})

    try:
        # Exit as soon as either child dies, so a crash is visible immediately.
        while True:
            for name, proc in (("API", api), ("dashboard", dashboard)):
                code = proc.poll()
                if code is not None:
                    print(f"\n[{name}] exited with code {code}; shutting down.")
                    raise KeyboardInterrupt
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        for proc in (dashboard, api):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        PID_FILE.unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Doomscroller launcher")
    parser.add_argument("--daemon", action="store_true", help="Run in background")
    parser.add_argument("--stop", action="store_true", help="Stop running daemon")
    parser.add_argument("--status", action="store_true", help="Show status")
    args = parser.parse_args()

    if args.stop:
        return _stop()
    if args.status:
        return _status()
    return _start(daemon=args.daemon)


if __name__ == "__main__":
    sys.exit(main())
