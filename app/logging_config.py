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
import math
import os
import re
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

_SECRET_SETTINGS_ATTRS = (
    "review_token",
    "telegram_bot_token",
    "pixabay_api_key",
    "pexels_api_key",
    "tenor_api_key",
    "giphy_api_key",
    "deepgram_api_key",
    "sarvam_api_key",
)
_SECRET_DICT_KEYS = frozenset({
    *_SECRET_SETTINGS_ATTRS,
    "access_token",
    "refresh_token",
    "client_secret",
})
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{4,}")
_ENV_SECRET_RE = re.compile(
    r"(\b(?:REVIEW_TOKEN|TELEGRAM_BOT_TOKEN|PIXABAY_API_KEY|PEXELS_API_KEY|TENOR_API_KEY|GIPHY_API_KEY|"
    r"DEEPGRAM_API_KEY|SARVAM_API_KEY)\s*[=:]\s*)([^\s,;'\">&]+)",
    re.IGNORECASE,
)
_JSON_SECRET_RE = re.compile(
    r'("(?:review_token|telegram_bot_token|pixabay_api_key|pexels_api_key|tenor_api_key|giphy_api_key|'
    r'deepgram_api_key|sarvam_api_key|access_token|refresh_token|client_secret)"\s*:\s*")([^"]*)(")',
    re.IGNORECASE,
)
_URL_PARAM_SECRET_RE = re.compile(
    r"([?&](?:key|api_key|apikey|token|access_token|refresh_token)=)([^&\s'\"]+)",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"(Authorization\s*:\s*(?:Bearer|Token)\s+)([^\s,;'\"]+)",
    re.IGNORECASE,
)


def _configured_secret_values() -> list[str]:
    try:
        from .config import settings
    except Exception:
        return []
    vals: list[str] = []
    for attr in _SECRET_SETTINGS_ATTRS:
        raw = getattr(settings, attr, None)
        if isinstance(raw, str):
            cleaned = raw.strip()
            if len(cleaned) >= 3:
                vals.append(cleaned)
    # Longest first so overlapping secrets never leave suffix fragments behind.
    vals.sort(key=len, reverse=True)
    return vals


def scrub_secrets(text: str) -> str:
    """Redact configured credentials and inline token patterns from a log string."""
    if not text:
        return text
    out = str(text)
    for secret in _configured_secret_values():
        if secret in out:
            out = out.replace(secret, "[REDACTED]")
    out = _BOT_TOKEN_RE.sub("bot[REDACTED]", out)
    out = _JSON_SECRET_RE.sub(r"\1[REDACTED]\3", out)
    out = _ENV_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _URL_PARAM_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", out)
    return out


def _scrub_value(value: Any) -> Any:
    """Recursively sanitize structured job-history payloads before write or read."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        cleaned_dict: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if key_str.strip().lower() in _SECRET_DICT_KEYS and v not in (None, ""):
                cleaned_dict[key_str] = "[REDACTED]"
            else:
                cleaned_dict[key_str] = _scrub_value(v)
        return cleaned_dict
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in value]
    return scrub_secrets(str(value))


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub_secrets(super().format(record))


def get_log_file_path() -> Path:
    return LOG_DIR / LOG_FILE


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

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    resolved = (level or settings.log_level or "INFO").upper()

    formatter = _ScrubbingFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, resolved, logging.INFO))

    # Drop and close any handlers we previously installed, so force=True leaks no descriptors.
    for handler in list(root.handlers):
        if getattr(handler, "_doomscroller", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._doomscroller = True  # type: ignore[attr-defined]
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        get_log_file_path(), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
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
        "logging configured at %s -> %s", resolved, get_log_file_path().resolve()
    )


def read_recent_logs(limit: int = 100) -> list[str]:
    """Return the most recent scrubbed process log lines (newest last)."""
    if limit <= 0:
        return []
    path = get_log_file_path()
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [scrub_secrets(line) for line in raw.splitlines() if line.strip()]
    return lines[-limit:]


# ---------------------------------------------------------------------------
# Job history
# ---------------------------------------------------------------------------
def get_job_history_path() -> Path:
    return LOG_DIR / HISTORY_FILE


def record_job_run(job_id: str, result: Any, *, duration_sec: float = 0.0,
                   ok: bool = True, error: str | None = None) -> dict:
    """Append one job execution to the history file. Never raises."""
    try:
        dur = float(duration_sec)
        safe_duration = round(max(0.0, dur), 2) if math.isfinite(dur) else 0.0
    except (TypeError, ValueError):
        safe_duration = 0.0

    raw_result = (
        result
        if isinstance(result, (dict, list, tuple, str, int, float, type(None)))
        else str(result)
    )
    entry: dict[str, Any] = {
        "job_id": str(job_id or ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_sec": safe_duration,
        "ok": bool(ok),
        "result": _scrub_value(raw_result),
    }
    if error:
        entry["error"] = scrub_secrets(str(error))
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(get_job_history_path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str, allow_nan=False) + "\n")
        _trim_history()
    except Exception as exc:  # pragma: no cover - history must never break a job
        logging.getLogger(__name__).warning("could not record job history: %s", scrub_secrets(str(exc)))
    return entry


def _trim_history() -> None:
    """Keep the history file from growing without bound."""
    path = get_job_history_path()
    try:
        if path.stat().st_size < 512_000:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= MAX_HISTORY_LINES:
            return
        keep = lines[-MAX_HISTORY_LINES:]
        tmp_path = path.with_suffix(".jsonl.tmp")
        tmp_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        pass


def read_job_history(limit: int = 100, job_id: str | None = None) -> list[dict]:
    """Most recent job runs, newest last. Returns [] if there is no history."""
    if limit <= 0:
        return []
    path = get_job_history_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if job_id and entry.get("job_id") != job_id:
            continue
        entries.append(_scrub_value(entry))
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
        if not job_id or not isinstance(stamp, str) or not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        else:
            when = when.astimezone(timezone.utc)
        if job_id not in out or when > out[job_id]:
            out[job_id] = when
    return out


def job_health(limit: int = 400) -> dict[str, dict]:
    """Latest outcome per job, for /scheduler/history and the dashboard."""
    if limit <= 0:
        return {}
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
get_recent_logs = read_recent_logs
get_job_health = job_health
