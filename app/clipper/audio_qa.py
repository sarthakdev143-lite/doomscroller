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
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app.config import settings
from app.metadata import _extract_json

log = logging.getLogger(__name__)

# The scorecard metrics the clipper can actually act on, so a stored issue points at something
# fixable here rather than at a style choice or the owner's empty music folder.
_SURFACE_WEIGHTS = ("critical", "major", "minor")
_MAX_ISSUES = 3


def earshot_bin() -> Optional[str]:
    configured = (settings.earshot_bin or "").strip()
    if configured:
        expanded = str(Path(configured).expanduser())
        return shutil.which(expanded) or (expanded if os.path.isfile(expanded) else None)
    found = shutil.which("earshot")
    if found:
        return found
    for name in ("earshot.exe", "earshot"):
        default = Path.home() / ".local" / "bin" / name
        if default.is_file():
            return str(default)
    return None


def available() -> bool:
    return bool(settings.earshot_qa) and earshot_bin() is not None


def _env() -> dict:
    env = {**os.environ}
    if settings.earshot_home:
        env["EARSHOT_HOME"] = str(Path(settings.earshot_home).expanduser())
    return env


def score(path: str) -> Optional[dict]:
    """earshot's scorecard for one clip, trimmed to what a reviewer needs, or None on any failure."""
    binary = earshot_bin()
    if binary is None or not path or not os.path.isfile(path):
        return None
    from app.winproc import background_subprocess_kwargs

    raw_timeout = getattr(settings, "earshot_qa_timeout_sec", 60)
    try:
        timeout = max(1, int(raw_timeout))
    except (TypeError, ValueError):
        timeout = 60

    try:
        proc = subprocess.run(
            [binary, "critique", os.path.abspath(path), "--json"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=_env(),
            stdin=subprocess.DEVNULL,
            **background_subprocess_kwargs(low_priority=True),
        )
    except subprocess.TimeoutExpired:
        log.warning("audio_qa: earshot timed out after %ss", timeout)
        return None
    except OSError as exc:
        log.warning("audio_qa: earshot could not start: %s", exc)
        return None
    raw_out = proc.stdout.decode("utf-8", errors="replace") if isinstance(proc.stdout, (bytes, bytearray)) else (proc.stdout or "")
    raw_err = proc.stderr.decode("utf-8", errors="replace") if isinstance(proc.stderr, (bytes, bytearray)) else (proc.stderr or "")
    if proc.returncode != 0:
        log.warning("audio_qa: earshot failed: %s", raw_err[-300:])
        return None
    cleaned = raw_out.lstrip("\ufeff").strip()
    data = None
    if cleaned:
        try:
            data = json.loads(cleaned)
        except ValueError:
            data = _extract_json(cleaned)
    if not isinstance(data, dict):
        log.info("audio_qa: earshot reply was not JSON")
        return None
    return summarise(data)


def summarise(data: dict) -> Optional[dict]:
    if not isinstance(data, dict) or "verdict" not in data:
        return None
    raw_actions = data.get("actions")
    actions = raw_actions if isinstance(raw_actions, list) else []
    issues = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        if action.get("weight") in _SURFACE_WEIGHTS:
            issues.append({"metric": action.get("metric") or "", "weight": action.get("weight"),
                           "action": str(action.get("action") or "")[:160]})
        if len(issues) >= _MAX_ISSUES:
            break
    raw_score = data.get("score")
    clean_score: Optional[float] = None
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) and math.isfinite(float(raw_score)):
        clean_score = round(float(raw_score), 3)
    return {
        "verdict": data.get("verdict"),
        "score": clean_score,
        "in_range": data.get("in_range"),
        "scored": data.get("scored"),
        "issues": issues,
    }
