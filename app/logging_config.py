"""Logging and job-run history.

Two separate concerns live here:

**Process logging.** ``configure_logging()`` attaches a console handler and a
rotating file handler to the *root* logger. Nothing called it before, which
meant the root logger sat at its default WARNING level and every ``log.info``
in the app was silently discarded -- a full day of unattended operation left
almost no trace. It must be called once per process, before the scheduler
starts.

**Job history.** ``record_job_run()`` appends one JSON line per scheduler job
execution to ``logs/job_history.jsonl``. That file is the only durable answer to
"what did it do overnight?", and it doubles as the last-run bookkeeping the
catch-up logic reads after the machine wakes from sleep.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_DIR = Path("logs")
LOG_FILE = "doomscroller.log"
HISTORY_FILE = "job_history.jsonl"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
# Keep the history file bounded; it is append-only and read whole.
MAX_HISTORY_LINES = 4000

_configured = False


def configure_logging(level: str | None = None, *, force: bool = False) -> None:
    """Attach console + rotating file handlers to the root logger.

    Idempotent: calling it twice will not double every log line. Uvicorn and
    Streamlit each configure their own loggers, so this deliberately targets
    the root logger that application modules actually log through.
    """
    global _configured
    if _configured and not force:
        return

    from .config import settings

    LOG_DIR.mkdir(exist_ok=True)
    resolved = (level or settings.log_level or "INFO").upper()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, resolved, logging.INFO))

    # Drop any handlers we previously installed, so force=True is clean.
    for handler in list(root.handlers):
        if getattr(handler, "_doomscroller", False):
            root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._doomscroller = True  # type: ignore[attr-defined]
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._doomscroller = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    # Quieten libraries that log a line per operation.
    for name, lib_level in (
        ("uvicorn.access", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("apscheduler.executors.default", logging.WARNING),
        ("apscheduler.scheduler", logging.INFO),
    ):
        logging.getLogger(name).setLevel(lib_level)

    _configured = True
    logging.getLogger(__name__).info(
        "logging configured at %s -> %s", resolved, (LOG_DIR / LOG_FILE).resolve()
    )


# ---------------------------------------------------------------------------
# Job history
# ---------------------------------------------------------------------------
def get_job_history_path() -> Path:
    return LOG_DIR / HISTORY_FILE


def record_job_run(job_id: str, result: Any, *, duration_sec: float = 0.0,
                   ok: bool = True, error: str | None = None) -> dict:
    """Append one job execution to the history file. Never raises."""
    entry = {
        "job_id": job_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_sec": round(duration_sec, 2),
        "ok": ok,
        "result": result if isinstance(result, (dict, list, str, int, float, type(None)))
        else str(result),
    }
    if error:
        entry["error"] = error
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with open(get_job_history_path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
        _trim_history()
    except Exception as exc:  # pragma: no cover - history must never break a job
        logging.getLogger(__name__).warning("could not record job history: %s", exc)
    return entry


def _trim_history() -> None:
    """Keep the history file from growing without bound."""
    path = get_job_history_path()
    try:
        if path.stat().st_size < 512_000:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= MAX_HISTORY_LINES:
            return
        keep = lines[-MAX_HISTORY_LINES:]
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


def read_job_history(limit: int = 100, job_id: str | None = None) -> list[dict]:
    """Most recent job runs, newest last. Returns [] if there is no history."""
    path = get_job_history_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if job_id and entry.get("job_id") != job_id:
            continue
        entries.append(entry)
    return entries[-limit:]


def last_run_times() -> dict[str, datetime]:
    """Most recent successful run per job id, as UTC datetimes.

    This is what the catch-up pass consults on startup: if the machine was
    asleep past a job's interval, APScheduler drops the missed fire entirely
    rather than running it late.
    """
    out: dict[str, datetime] = {}
    for entry in read_job_history(limit=MAX_HISTORY_LINES):
        if not entry.get("ok", True):
            continue
        job_id = entry.get("job_id")
        stamp = entry.get("timestamp")
        if not job_id or not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if job_id not in out or when > out[job_id]:
            out[job_id] = when
    return out


def job_health(limit: int = 400) -> dict[str, dict]:
    """Latest outcome per job, for /scheduler/history and the dashboard."""
    latest: dict[str, dict] = {}
    counts: dict[str, dict] = {}
    for entry in read_job_history(limit=limit):
        job_id = entry.get("job_id")
        if not job_id:
            continue
        latest[job_id] = entry
        bucket = counts.setdefault(job_id, {"runs": 0, "failures": 0})
        bucket["runs"] += 1
        if not entry.get("ok", True):
            bucket["failures"] += 1
    for job_id, entry in latest.items():
        entry = dict(entry)
        entry.update(counts.get(job_id, {}))
        latest[job_id] = entry
    return latest


# Backwards-compatible aliases for the previous names.
log_job_execution = record_job_run
get_recent_job_history = read_job_history
get_job_health = job_health
