"""AI calls through the local OpenCode CLI, using whatever account it is linked to.

OpenCode is a coding agent with shell access and the user's MCP servers. Video
transcripts are untrusted text, so a line in a video saying "run this command"
must not be able to do anything. Every call therefore runs on a private server
(the background service would ignore our config) with all tools denied, from an
empty directory, and with project config off. OpenCode roots its session in $PWD
rather than the real working directory, so PWD has to move too.

The message goes over stdin because a long transcript exceeds the argument limit,
and stdin must always be supplied or closed: with an inherited stdin the CLI
waits on it forever.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from app.config import settings
from app.metadata import _extract_json

log = logging.getLogger(__name__)

LOCKDOWN_CONFIG = json.dumps({"permission": {"*": "deny"}, "tools": {"*": False}})


def opencode_path() -> Optional[str]:
    configured = (settings.opencode_bin or "").strip()
    if configured:
        return shutil.which(configured)
    found = shutil.which("opencode")
    if found:
        return found
    default = Path.home() / ".opencode" / "bin" / "opencode"
    return str(default) if default.is_file() else None


def available() -> bool:
    return opencode_path() is not None


def _text_from_events(stdout: str) -> str:
    parts = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "text":
            part = event.get("part")
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str):
                parts.append(text)
        elif event.get("type") == "error":
            log.warning("opencode reported an error: %s", str(event)[:400])
    return "".join(parts).strip()


def ask(prompt: str, *, files: Sequence[str] = (), model: str = "") -> Optional[str]:
    """Return the model's reply, or None on any failure."""
    binary = opencode_path()
    if binary is None:
        log.info("opencode not found; skipping AI")
        return None
    command = [binary, "run", "--standalone", "--format", "json",
               "--model", model or settings.opencode_model]
    for path in files:
        command += ["--file", str(path)]
    # With attachments the CLI takes its message as an argument. Prompts that
    # carry attachments are short, so the argument limit does not apply.
    kwargs: dict = {"stdin": subprocess.DEVNULL} if files else {"input": prompt}
    if files:
        command.append(prompt)
    with tempfile.TemporaryDirectory(prefix="clipper-ai-") as empty_dir:
        try:
            proc = subprocess.run(
                command, capture_output=True, encoding="utf-8", errors="replace", cwd=empty_dir,
                timeout=settings.clip_ai_timeout_sec,
                env={**os.environ, "PWD": empty_dir, "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                     "OPENCODE_CONFIG_CONTENT": LOCKDOWN_CONFIG},
                **kwargs,
            )
        except subprocess.TimeoutExpired:
            log.warning("opencode timed out after %ss", settings.clip_ai_timeout_sec)
            return None
        except OSError as exc:
            log.warning("opencode could not start: %s", exc)
            return None
    if proc.returncode != 0:
        log.warning("opencode failed: %s", (proc.stderr or "")[-400:])
        return None
    return _text_from_events(proc.stdout) or None


def ask_json(prompt: str, *, files: Sequence[str] = (), model: str = "") -> Optional[dict]:
    """Like :func:`ask`, for a JSON object. Retries once on an unparsable reply."""
    for _attempt in range(2):
        reply = ask(prompt, files=files, model=model)
        if reply is None:
            return None
        parsed = _extract_json(reply)
        if parsed is not None:
            return parsed
        log.info("opencode reply was not JSON: %s", reply[:200])
    return None
