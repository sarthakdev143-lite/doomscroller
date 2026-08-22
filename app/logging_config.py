"""Structured file logging for Doomscroller.

Usage:
    from app.logging_config import configure_logging
    configure_logging()  # call once at startup
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .config import settings


def configure_logging() -> None:
    """Configure rotating file + console logging."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file handler — keep 5MB per file, 3 backups
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "doomscroller.log",
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Don't let individual loggers double-log
    root.propagate = False

    # Reduce noise from noisy libraries
    for lib in ("uvicorn", "uvicorn.access", "uvicorn.error", "engineio", "socketio", "apscheduler"):
        l = logging.getLogger(lib)
        l.setLevel(logging.WARNING)
        if lib == "apscheduler":
            # APScheduler logs every job start/finish — keep it at INFO
            l.setLevel(logging.INFO)


def get_job_history_path() -> Path:
    """Path to the job history JSONL file."""
    return Path("logs") / "job_history.jsonl"


def log_job_execution(job_id: str, result: dict) -> None:
    """Append a job execution result to the history file."""
    import json
    from datetime import datetime, timezone

    hist_file = get_job_history_path()
    entry = {
        "job_id": job_id,
        "result": result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(hist_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    logging.getLogger(__name__).info("job[%s] = %s", job_id, result)


def get_recent_job_history(limit: int = 50) -> list[dict]:
    """Read recent job history entries."""
    hist_file = get_job_history_path()
    if not hist_file.exists():
        return []
    lines = hist_file.read_text(encoding="utf-8").strip().splitlines()
    results = []
    for line in lines[-limit:]:
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return results


def get_job_health() -> dict:
    """Last-executed state per job."""
    hist = get_recent_job_history(limit=200)
    latest: dict[str, dict] = {}
    for entry in reversed(hist):
        jid = entry.get("job_id")
        if jid not in latest:
            latest[jid] = entry
    return latest