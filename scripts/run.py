"""One-command launcher for Doomscroller (local autonomous mode).

Starts:
  - FastAPI backend (with APScheduler running in-process) on port 8000
  - Streamlit dashboard on port 8501

Usage:
    python scripts/run.py            # foreground, logs to console
    python scripts/run.py --daemon   # background, logs to logs/doomscroller.log
    python scripts/run.py --stop     # stop a running daemon

No Docker, no Redis, no Celery required. The scheduler runs inside the
FastAPI process and triggers scrape/discover/publish/cleanup jobs on
configurable intervals.
"""
from __future__ import annotations

import argparse
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
PID_FILE = PROJECT_ROOT / "doomscroller.pid"
PYTHON = sys.executable


def _is_running() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, ValueError, OSError):
        return None


def _stop() -> int:
    pid = _is_running()
    if pid is None:
        print("Not running.")
        return 0
    print(f"Stopping PID {pid}…")
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    time.sleep(1)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)
    print("Stopped.")
    return 0


def _start(daemon: bool) -> int:
    if _is_running():
        print(f"Already running (PID {_is_running()}). Stop it first with --stop.")
        return 1

    if daemon:
        # Start as detached process group
        log_path = LOG_DIR / "doomscroller.log"
        api_log = LOG_DIR / "api.log"
        dash_log = LOG_DIR / "dashboard.log"
        print(f"Starting in daemon mode. Logs: {log_path}, {api_log}, {dash_log}")

        # Use nohup-like behavior with subprocess.Popen + start_new_session
        api_proc = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
            cwd=PROJECT_ROOT,
            stdout=open(api_log, "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        time.sleep(2)  # let API boot
        dash_proc = subprocess.Popen(
            [PYTHON, "-m", "streamlit", "run", "dashboard/app.py",
             "--server.port", "8501", "--server.headless", "true"],
            cwd=PROJECT_ROOT,
            stdout=open(dash_log, "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Write a parent PID (we'll kill the whole process group on stop)
        with open(PID_FILE, "w") as f:
            f.write(str(api_proc.pid))
        # Save both PIDs in a sidecar for clarity
        (PROJECT_ROOT / "doomscroller.pids").write_text(
            f"api={api_proc.pid}\ndashboard={dash_proc.pid}\n"
        )
        print(f"API:       http://localhost:8000  (PID {api_proc.pid})")
        print(f"Dashboard: http://localhost:8501  (PID {dash_proc.pid})")
        print(f"Stop with: python scripts/run.py --stop")
        return 0
    else:
        # Foreground: run API + dashboard as concurrent subprocesses
        from concurrent.futures import ThreadPoolExecutor

        api_cmd = [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
        dash_cmd = [PYTHON, "-m", "streamlit", "run", "dashboard/app.py",
                    "--server.port", "8501", "--server.headless", "true"]

        print("Starting Doomscroller in foreground. Press Ctrl+C to stop.")
        print(f"API:       http://localhost:8000")
        print(f"Dashboard: http://localhost:8501")
        print("-" * 60)

        api_proc = subprocess.Popen(api_cmd, cwd=PROJECT_ROOT)
        time.sleep(2)
        dash_proc = subprocess.Popen(dash_cmd, cwd=PROJECT_ROOT)

        def _wait(p: subprocess.Popen, name: str) -> int:
            return p.wait()

        try:
            with ThreadPoolExecutor(max_workers=2) as ex:
                futs = [ex.submit(_wait, api_proc, "api"), ex.submit(_wait, dash_proc, "dashboard")]
                done, _ = __import__("concurrent").futures.wait(futs, return_when="FIRST_COMPLETED")
                for f in done:
                    exc = f.exception()
                    if exc:
                        print(f"[{f}] crashed: {exc}")
        except KeyboardInterrupt:
            print("\nShutting down…")
        finally:
            for p in (api_proc, dash_proc):
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
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
        pid = _is_running()
        if pid:
            print(f"Running (PID {pid})")
            return 0
        print("Not running.")
        return 1
    return _start(daemon=args.daemon)


if __name__ == "__main__":
    sys.exit(main())
