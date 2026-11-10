"""One-off actions the owner can trigger from the control panel, early rather than waiting for the schedule.

The scheduler already runs these on its own cadence; this is the owner pressing the button now. The slow
ones (they reach the network or cut clips) run in a daemon thread so the phone gets an answer at once and
polls `status()` for the result. The DB is the single source of truth, so a manual run and a scheduled run
touching the same rows is safe — the growth jobs are written to be idempotent and claim their work.
"""
from __future__ import annotations

import logging
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
    "reload": ("Reload channels.json", "Re-read the channels file after a hand edit."),
}
_INLINE = {"reload"}  # fast and local; the rest reach the network, so they go to a thread


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_COUNT_WORDS = {"queued": "queued", "snapshots": "stats updated", "analytics": "retention updated",
                "findings": "new findings", "matured": "videos judged", "feeds": "feeds checked"}
_NOTE_KEYS = ("skipped", "starved", "waiting", "queued", "mined")


def _summarise(result: object) -> str:
    """A short, human line from a job's result dict, flattening the per-channel {slug: {...}} shape.

    Values are mixed: some keys are counts (watch's `queued`, learn's `findings`), others are strings
    (supply's `queued` is the source it took, `starved` is a reason), so only numbers are summed and a
    string falls through to a note. Defensive on purpose — a job result must never crash the panel.
    """
    if not isinstance(result, dict):
        return str(result)[:200]
    if result.get("error"):
        return str(result["error"])[:200]
    sections = [v for v in result.values() if isinstance(v, dict)]
    data = sections or [result]
    counts: dict[str, float] = {}
    notes: list[str] = []
    for d in data:
        for key, value in d.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                counts[key] = counts.get(key, 0) + value
            elif isinstance(value, str) and value and key in _NOTE_KEYS:
                notes.append(value)
    bits = [f"{int(n)} {_COUNT_WORDS[k]}" for k, n in counts.items() if k in _COUNT_WORDS and n]
    if isinstance(result.get("active"), list):
        bits.append(f"{len(result['active'])} channels active")
    if not bits and notes:
        return notes[0][:160]
    return ", ".join(bits) or "done, nothing to do"


def _call(action: str) -> dict:
    from . import channels, collect, learn, supply, watch

    funcs: dict[str, Callable[[], dict]] = {
        "supply": supply.supply_job, "watch": watch.watch_job,
        "collect": collect.collect_job, "learn": learn.learn_job,
        "reload": channels.reload_channels,
    }
    return funcs[action]()


def _run_tracked(action: str) -> None:
    try:
        st = {"state": "done", "result": _summarise(_call(action))}
    except Exception as exc:  # a manual run must never take the server down
        log.exception("control action %s failed", action)
        st = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
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
