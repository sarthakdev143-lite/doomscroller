"""One-off actions the owner can trigger from the control panel, early rather than waiting for the schedule.

The scheduler already runs these on its own cadence; this is the owner pressing the button now. The slow
ones (they reach the network or cut clips) run in a daemon thread so the phone gets an answer at once and
polls `status()` for the result. The DB is the single source of truth, so a manual run and a scheduled run
touching the same rows is safe — the growth jobs are written to be idempotent and claim their work.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

_lock = threading.Lock()
_status: dict[str, dict] = {}

# action -> (button label, one-line description). autopilot is deliberately left out: posting runs on its
# own 10-minute cadence, and forcing it from a second process invites a double post for no real gain.
ACTIONS: dict[str, tuple[str, str]] = {
    "supply": ("Make more clips", "Cut the next source now instead of waiting for the hourly top-up."),
    "watch": ("Check for new uploads", "Poll every watched creator's feed for a fresh upload right now."),
    "collect": ("Refresh stats", "Pull the latest views and retention from YouTube now."),
    "learn": ("Learn now", "Re-judge matured videos and update what the tests show."),
    "scrape": ("Scrape viral candidates", "Fetch new viral Shorts into the scraper queue (classic scraper mode)."),
    "transform": ("Transform approved", "Render approved scraped videos to 1080x1920 Shorts now."),
    "filter_queue": ("Re-apply safety & language filters", "Sweep PENDING backlog against current safety, topic & language rules."),
    "reload": ("Reload channels.json", "Re-read the channels file after a hand edit."),
    "cleanup": ("Reclaim expired cache", "Delete expired source videos and orphaned files now."),
    "test_notify": ("Send test alert", "Send a test notification to ntfy / Telegram now to verify delivery."),
}
_INLINE = {"reload", "cleanup", "filter_queue", "test_notify"}  # fast or interactive; heavy batch jobs go to a thread


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_COUNT_WORDS = {
    "queued": "queued", "snapshots": "stats updated", "analytics": "retention updated",
    "findings": "new findings", "matured": "videos judged", "feeds": "feeds checked",
    "created": "scraped", "transformed": "transformed",
    "rejected": "filtered out", "kept": "kept",
    "mb_reclaimed": "MB reclaimed", "sources_deleted": "sources removed", "orphans_deleted": "orphans removed",
}
_NOTE_KEYS = ("skipped", "starved", "waiting", "queued", "mined")


def _summarise(result: object) -> str:
    """A short, human line from a job's result dict, flattening the per-channel {slug: {...}} shape.

    Values are mixed: some keys are counts (watch's `queued`, learn's `findings`), others are strings
    (supply's `queued` is the source it took, `starved` is a reason), so only numbers are summed and a
    string falls through to a note. Defensive on purpose — a job result must never crash the panel.
    """
    from . import notify

    if not isinstance(result, dict):
        return notify._redact(str(result))[:200]
    if result.get("error"):
        return notify._redact(str(result["error"]))[:200]
    if "configured" in result and isinstance(result.get("channels"), dict):
        if not result.get("configured"):
            return notify._redact(str(result.get("skipped") or "no NTFY_TOPIC or Telegram bot set"))[:160]
        parts = [
            f"{name}: sent" if ch.get("ok") else f"{name}: failed ({ch.get('error', 'error')})"
            for name, ch in result["channels"].items()
            if isinstance(ch, dict)
        ]
        return notify._redact(", ".join(parts) or "no notification channels configured")[:200]
    sections = [v for v in result.values() if isinstance(v, dict)]
    data = sections or [result]
    counts: dict[str, float] = {}
    notes: list[str] = []
    for d in data:
        for key, value in d.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and math.isfinite(value):
                counts[key] = counts.get(key, 0) + value
            elif isinstance(value, str) and value and key in _NOTE_KEYS:
                notes.append(value)
    bits = [
        f"{int(n) if float(n).is_integer() else round(n, 1)} {_COUNT_WORDS[k]}"
        for k, n in counts.items() if k in _COUNT_WORDS and n
    ]
    if isinstance(result.get("active"), list):
        bits.append(f"{len(result['active'])} channels active")
    if not bits and notes:
        return notify._redact(notes[0])[:160]
    return notify._redact(", ".join(bits) or "done, nothing to do")[:200]


def _call(action: str) -> dict:
    from app import scheduler
    from tasks.pipeline import apply_queue_filters
    from . import channels, collect, learn, notify, supply, watch

    funcs: dict[str, Callable[[], dict]] = {
        "supply": supply.supply_job, "watch": watch.watch_job,
        "collect": collect.collect_job, "learn": learn.learn_job,
        "scrape": lambda: scheduler.run_job("scrape"),
        "transform": lambda: scheduler.run_job("transform"),
        "filter_queue": lambda: apply_queue_filters(dry_run=False),
        "reload": channels.reload_channels,
        "cleanup": lambda: scheduler.run_job("cleanup"),
        "test_notify": notify.send_test_notification,
    }
    return funcs[action]()


def preview_maintenance() -> dict:
    """Non-destructive dry-run preview of queue filter sweep and cache cleanup."""
    from app import scheduler
    from tasks.pipeline import apply_queue_filters

    return {
        "filters": apply_queue_filters(dry_run=True),
        "cleanup": scheduler.cleanup_job(dry_run=True),
    }


def _run_tracked(action: str) -> None:
    from . import notify

    try:
        st = {"state": "done", "result": _summarise(_call(action))}
    except Exception as exc:  # a manual run must never take the server down
        log.exception("control action %s failed", action)
        st = {"state": "error", "error": notify._redact(f"{type(exc).__name__}: {exc}")[:200]}
    with _lock:
        _status[action] = {**st, "at": _now()}


def trigger(action: str) -> dict:
    """Start one action. Returns at once; a slow one runs in the background and lands in `status()`."""
    if action not in ACTIONS:
        raise KeyError(action)
    with _lock:
        if _status.get(action, {}).get("state") == "running":
            return {"action": action, "state": "running", "at": _status[action]["at"]}
        _status[action] = {"state": "running", "at": _now()}
    if action in _INLINE:
        _run_tracked(action)
    else:
        threading.Thread(target=_run_tracked, args=(action,), daemon=True, name=f"control-{action}").start()
    with _lock:
        return {"action": action, **_status[action]}


def status() -> dict[str, dict]:
    """The last outcome of each action, for the panel to poll."""
    with _lock:
        return {action: dict(state) for action, state in _status.items()}
