"""Run the SaaS product server: the web app (uvicorn, saas.main:app) plus N job-worker processes.

    .venv-saas/Scripts/python.exe scripts/saas_serve.py                # web + SAAS_WORKER_CONCURRENCY workers
    .venv-saas/Scripts/python.exe scripts/saas_serve.py --workers 2
    .venv-saas/Scripts/python.exe scripts/saas_serve.py --no-web       # workers only (a second box)
    .venv-saas/Scripts/python.exe scripts/saas_serve.py --check        # print the effective config and exit

Safety:
* Binds SAAS_HOST:SAAS_PORT (127.0.0.1:8700 by default). It never binds 0.0.0.0 unless SAAS_HOST says so.
* Never reads the owner's `.env` (it turns real YouTube uploads on). DOOMSCROLLER_ENV_FILE is forced
  empty before anything can import app.*, so the clip engine only sees what this process gives it.
* The engine's AI/STT keys (DEEPGRAM_API_KEY, SARVAM_API_KEY, the OpenAI-compatible LLM settings, ...)
  come from the operator's environment or from the SaaS env file (SAAS_ENV_FILE, default .env.saas);
  every non-SAAS_ line in that file is exported to the workers.
* One job per worker process (the clip engine's cost meter is process-wide). Workers that exit are
  restarted with backoff; Ctrl+C stops the web server, then asks workers to finish and exits.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["DOOMSCROLLER_ENV_FILE"] = ""  # must precede any import of app.*
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env_file() -> list[str]:
    """Export the SaaS env file's lines into os.environ (existing variables win). Returns names loaded."""
    name = os.environ.get("SAAS_ENV_FILE", ".env.saas")
    if not name:
        return []
    path = (ROOT / name) if not Path(name).is_absolute() else Path(name)
    if path.resolve() == (ROOT / ".env").resolve():
        raise SystemExit("refusing to load the owner's .env as the SaaS env file")
    os.environ["SAAS_ENV_FILE"] = str(path)  # saas.config reads it relative to the cwd otherwise
    if not path.is_file():
        return []
    from dotenv import dotenv_values

    loaded = []
    for key, value in dotenv_values(path).items():
        if value is None or key in os.environ or key == "DOOMSCROLLER_ENV_FILE":
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


class WorkerPool:
    def __init__(self, n: int):
        self.n = n
        self.procs: list[subprocess.Popen | None] = [None] * n
        self.stop = threading.Event()
        self.restarts = [0] * n

    def _spawn(self, i: int) -> subprocess.Popen:
        # stdin is a pipe we never write to: when this supervisor dies, however abruptly, the worker reads
        # EOF and stops after its current job instead of living on as an orphan.
        kwargs: dict = {"cwd": str(ROOT), "stdin": subprocess.PIPE,
                        "env": {**os.environ, "SAAS_WORKER_WATCH_STDIN": "1"}}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                       | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen([sys.executable, "-m", "saas.jobs.worker"], **kwargs)

    def supervise(self) -> None:
        while not self.stop.is_set():
            for i in range(self.n):
                p = self.procs[i]
                if p is None or p.poll() is not None:
                    if p is not None:
                        self.restarts[i] += 1
                        print(f"[saas] worker {i} exited ({p.returncode}); restarting", flush=True)
                        if self.stop.wait(min(60, 2 ** min(self.restarts[i], 5))):
                            return
                    self.procs[i] = self._spawn(i)
            self.stop.wait(2)

    def shutdown(self, grace: float = 60.0) -> None:
        self.stop.set()
        for p in self.procs:
            if p is not None and p.poll() is None:
                try:
                    if p.stdin is not None:
                        p.stdin.close()
                    if os.name == "nt":
                        p.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        p.terminate()
                except OSError:
                    pass
        deadline = time.monotonic() + grace
        for p in self.procs:
            if p is None:
                continue
            try:
                p.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()


def _backup_loop(stop: threading.Event) -> None:
    """SQLite: back up whenever the newest backup is older than SAAS_BACKUP_EVERY_HOURS (checked hourly)."""
    from saas.jobs import backup

    while not stop.is_set():
        try:
            if backup.due():
                out = backup.run_backup()
                print(f"[saas] backup written: {out.name}", flush=True)
        except Exception as exc:  # a failed backup must not take the server down
            print(f"[saas] BACKUP FAILED: {type(exc).__name__}: {exc}", flush=True)
        stop.wait(3600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--workers", type=int, default=None, help="worker processes (default SAAS_WORKER_CONCURRENCY)")
    ap.add_argument("--no-web", action="store_true", help="run workers only")
    ap.add_argument("--check", action="store_true", help="print the effective config and exit")
    args = ap.parse_args()

    loaded = _load_env_file()
    if not os.environ.get("SAAS_SECRET_KEY") and os.environ.get("SAAS_ENV", "dev").lower() != "prod":
        # Dev convenience: one random key shared by the web process and every worker, so links signed in
        # one process verify in another. Still dies on restart; prod requires a real SAAS_SECRET_KEY.
        import secrets as _secrets

        os.environ["SAAS_SECRET_KEY"] = _secrets.token_urlsafe(48)
    from saas.config import settings
    from saas.db import init_db

    workers = settings.worker_concurrency if args.workers is None else max(0, args.workers)
    print(f"[saas] env={settings.env} host={settings.host}:{settings.port} db={settings.database_url.split('://')[0]} "
          f"engine={settings.engine} workers={workers} launch_mode={settings.launch_mode} "
          f"payments={settings.payment_provider_default}", flush=True)
    if loaded:
        print(f"[saas] exported from the env file: {', '.join(sorted(loaded))}", flush=True)  # names only
    problems = []
    if settings.is_prod:
        if len(settings.secret_key) < 32:
            problems.append("SAAS_SECRET_KEY must be 32+ characters in prod")
        if not settings.public_url.startswith("https://"):
            problems.append("SAAS_PUBLIC_URL must be https in prod")
        if settings.engine != "clipper":
            problems.append("SAAS_ENGINE should be 'clipper' in prod (mock makes placeholder clips)")
    if settings.host == "0.0.0.0":
        print("[saas] WARNING: SAAS_HOST=0.0.0.0 exposes the server on every interface", flush=True)
    for p in problems:
        print(f"[saas] CONFIG: {p}", flush=True)
    if args.check:
        return 1 if problems else 0
    if problems:
        return 2

    init_db()
    from saas.db import session_scope
    from saas.billing import catalog

    with session_scope() as db:
        catalog.ensure_seeded(db)

    pool = WorkerPool(workers)
    sup = threading.Thread(target=pool.supervise, daemon=True, name="saas-workers")
    sup.start()
    threading.Thread(target=_backup_loop, args=(pool.stop,), daemon=True, name="saas-backup").start()
    from saas.billing import bmc_relay

    if not args.no_web and bmc_relay.configured():
        # Buy Me a Coffee posts to the edge Worker, which keeps events in D1 until we pull them (docs/saas/beta-edge.md).
        threading.Thread(target=bmc_relay.run_forever, args=(pool.stop,), daemon=True, name="saas-bmc-relay").start()
        print(f"[saas] pulling Buy Me a Coffee events from the edge relay every "
              f"{max(5, bmc_relay.relay_settings.poll_seconds)}s", flush=True)
    try:
        if args.no_web:
            while True:
                time.sleep(3600)
        import uvicorn

        uvicorn.run("saas.main:app", host=settings.host, port=settings.port, proxy_headers=False,
                    log_level="info", access_log=False)
    except KeyboardInterrupt:
        pass
    finally:
        print("[saas] stopping workers (they finish their current job first)", flush=True)
        pool.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
