"""An optional audio-quality reading for a finished clip, from the earshot skill.

The loop learns from views; this adds a signal available *before* a clip is posted: earshot's
launch-video scorecard (loudness, sync, stereo width, spectrum, realness). It is shelled out to
the `earshot` CLI, exactly as the AI CLIs are, so the app's own venv never carries earshot's
~1.1 GB of models. Off unless EARSHOT_QA is set and the binary is found; it never raises and
never blocks a render, so a missing or slow earshot just means a clip has no score.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

# The scorecard metrics the clipper can actually act on, so a stored issue points at something
# fixable here rather than at a style choice or the owner's empty music folder.
_SURFACE_WEIGHTS = ("critical", "major", "minor")
_MAX_ISSUES = 3


def earshot_bin() -> Optional[str]:
    configured = (settings.earshot_bin or "").strip()
    if configured:
        return shutil.which(configured) or (configured if os.path.isfile(configured) else None)
    found = shutil.which("earshot")
    if found:
        return found
    default = Path.home() / ".local" / "bin" / "earshot"
    return str(default) if default.is_file() else None


def available() -> bool:
    return bool(settings.earshot_qa) and earshot_bin() is not None


def _env() -> dict:
    env = {**os.environ}
    if settings.earshot_home:
        env["EARSHOT_HOME"] = settings.earshot_home
    return env


def score(path: str) -> Optional[dict]:
    """earshot's scorecard for one clip, trimmed to what a reviewer needs, or None on any failure."""
    binary = earshot_bin()
    if binary is None or not os.path.exists(path):
        return None
    try:
        proc = subprocess.run(
            [binary, "critique", os.path.abspath(path), "--json"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=settings.earshot_qa_timeout_sec, env=_env(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        log.warning("audio_qa: earshot timed out after %ss", settings.earshot_qa_timeout_sec)
        return None
    except OSError as exc:
        log.warning("audio_qa: earshot could not start: %s", exc)
        return None
    if proc.returncode != 0:
        log.warning("audio_qa: earshot failed: %s", (proc.stderr or "")[-300:])
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        log.info("audio_qa: earshot reply was not JSON")
        return None
    return summarise(data)


def summarise(data: dict) -> Optional[dict]:
    if not isinstance(data, dict) or "verdict" not in data:
        return None
    issues = []
    for action in data.get("actions") or []:
        if not isinstance(action, dict):
            continue
        if action.get("weight") in _SURFACE_WEIGHTS:
            issues.append({"metric": action.get("metric") or "", "weight": action.get("weight"),
                           "action": str(action.get("action") or "")[:160]})
        if len(issues) >= _MAX_ISSUES:
            break
    return {
        "verdict": data.get("verdict"),
        "score": round(float(data["score"]), 3) if isinstance(data.get("score"), (int, float)) else None,
        "in_range": data.get("in_range"),
        "scored": data.get("scored"),
        "issues": issues,
    }
