"""AI calls through the local OpenCode CLI, with the Claude CLI as the fallback.

OpenCode goes first, using whatever account it is linked to. When it fails or times out,
the call goes to Claude instead, and OpenCode is skipped for a while: one timeout costs
minutes, and a failing provider tends to stay down.

Claude Code is an agent too, so it gets the same treatment: no tools at all (its own or
MCP), no settings, skills or plugins, no saved session, an empty directory, and a system
prompt that replaces the agent's. Images go inside the message as data, so it never needs
to read a file.

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

import base64
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

from app.config import settings
from app.metadata import _extract_json
from app.winproc import background_subprocess_kwargs

log = logging.getLogger(__name__)

LOCKDOWN_CONFIG = json.dumps({"permission": {"*": "deny"}, "tools": {"*": False}})
_CLAUDE_SYSTEM = ("You answer requests from a video-editing program. You have no tools and cannot run anything. "
                  "Reply with exactly what is asked, in the requested format, and nothing else.")
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
_OPENCODE_REST_SEC = 30 * 60
_opencode_down_until = 0.0


def _safe_utf8(raw: object) -> str:
    if isinstance(raw, (bytes, bytearray, memoryview)):
        text = bytes(raw).decode("utf-8-sig", errors="replace")
    elif isinstance(raw, str):
        text = raw.lstrip("\ufeff")
    else:
        return ""
    return text.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")


def _timeout() -> float:
    raw = getattr(settings, "clip_ai_timeout_sec", 120)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 120.0
    return max(1.0, val) if math.isfinite(val) else 120.0


def _resolve_cli(configured: str, name: str, fallbacks: Sequence[Path]) -> Optional[str]:
    configured = (configured or "").strip()
    if configured:
        expanded = str(Path(configured).expanduser())
        return shutil.which(expanded) or (expanded if os.path.isfile(expanded) else None)
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if candidate.is_file():
            return str(candidate)
    return None


def _command_prefix(binary: str) -> list[str]:
    """Build argv[0..] for ``binary``, resolving ``#!`` scripts on Windows where CreateProcess cannot."""
    if os.name == "nt":
        p = Path(binary)
        if p.is_file() and p.suffix.lower() not in {".exe", ".com", ".bat", ".cmd"}:
            try:
                with p.open("rb") as fh:
                    first = fh.readline(512)
                if first.startswith(b"#!"):
                    target = first[2:].decode("utf-8", errors="replace").strip()
                    if target:
                        if os.path.isfile(target):
                            return [target, binary]
                        parts = target.split()
                        if parts[0] in {"/usr/bin/env", "/bin/env"} and len(parts) > 1:
                            interp = shutil.which(parts[1]) or parts[1]
                            return [interp, *parts[2:], binary]
                        return [*parts, binary]
            except OSError:
                pass
    return [binary]


def opencode_path() -> Optional[str]:
    home = Path.home()
    appdata = Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else None
    fallbacks = [home / ".opencode" / "bin" / "opencode.exe", home / ".opencode" / "bin" / "opencode"]
    if appdata:
        fallbacks.append(appdata / "npm" / "opencode.cmd")
    return _resolve_cli(settings.opencode_bin, "opencode", fallbacks)


def claude_path() -> Optional[str]:
    if not settings.claude_fallback:
        return None
    home = Path.home()
    appdata = Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else None
    fallbacks = [home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"]
    if appdata:
        fallbacks.append(appdata / "npm" / "claude.cmd")
    return _resolve_cli(settings.claude_bin, "claude", fallbacks)


def available() -> bool:
    return opencode_path() is not None or claude_path() is not None


def _text_from_events(stdout: str | bytes) -> str:
    parts = []
    for line in _safe_utf8(stdout).splitlines():
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
    """Return the model's reply, or None when every route fails."""
    global _opencode_down_until
    if opencode_path() and time.monotonic() >= _opencode_down_until:
        reply = _ask_opencode(prompt, files=files, model=model)
        if reply is not None:
            return reply
        if claude_path():
            _opencode_down_until = time.monotonic() + _OPENCODE_REST_SEC
            log.warning("opencode failed; using Claude for the next %d minutes", _OPENCODE_REST_SEC // 60)
    if claude_path():
        return _ask_claude(prompt, files=files)
    if not opencode_path():
        log.info("no AI CLI found; skipping AI")
    return None


def _claude_message(prompt: str, files: Sequence[str]) -> str:
    content: list[dict] = []
    for path in files:
        media = _IMAGE_TYPES.get(Path(path).suffix.lower())
        if media is None:
            raise ValueError(f"cannot send {path} to Claude: not an image")
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    content.append({"type": "text", "text": _safe_utf8(prompt)})
    return json.dumps({"type": "user", "session_id": "", "parent_tool_use_id": None,
                       "message": {"role": "user", "content": content}}) + "\n"


def _result_from_stream(stdout: str | bytes) -> Optional[str]:
    for line in _safe_utf8(stdout).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            if event.get("is_error"):
                log.warning("claude reported an error: %s", str(event.get("result"))[:300])
                return None
            result = event.get("result")
            return result.strip() or None if isinstance(result, str) else None
    return None


def _ask_claude(prompt: str, *, files: Sequence[str] = ()) -> Optional[str]:
    binary = claude_path()
    if binary is None:
        return None
    try:
        message = _claude_message(prompt, files)
    except (OSError, ValueError) as exc:
        log.warning("claude: %s", exc)
        return None
    timeout = _timeout()
    command = [*_command_prefix(binary), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--tools", "", "--strict-mcp-config", "--safe-mode", "--disable-slash-commands",
               "--no-session-persistence", "--model", settings.claude_model, "--system-prompt", _CLAUDE_SYSTEM]
    with tempfile.TemporaryDirectory(prefix="clipper-ai-", ignore_cleanup_errors=True) as empty_dir:
        try:
            proc = subprocess.run(command, input=message, capture_output=True, encoding="utf-8", errors="replace",
                                  cwd=empty_dir, timeout=timeout,
                                  env={**os.environ, "PWD": empty_dir},
                                  **background_subprocess_kwargs(low_priority=True))
        except subprocess.TimeoutExpired:
            log.warning("claude timed out after %ss", timeout)
            return None
        except OSError as exc:
            log.warning("claude could not start: %s", exc)
            return None
    err_text = _safe_utf8(proc.stderr) or _safe_utf8(proc.stdout)
    if proc.returncode != 0:
        log.warning("claude failed: %s", err_text[-400:])
        return None
    return _result_from_stream(proc.stdout)


def _ask_opencode(prompt: str, *, files: Sequence[str] = (), model: str = "") -> Optional[str]:
    binary = opencode_path()
    if binary is None:
        return None
    timeout = _timeout()
    clean_prompt = _safe_utf8(prompt)
    command = [*_command_prefix(binary), "run", "--standalone", "--format", "json",
               "--model", model or settings.opencode_model]
    for path in files:
        command += ["--file", str(path)]
    # With attachments the CLI takes its message as an argument. Prompts that
    # carry attachments are short, so the argument limit does not apply.
    kwargs: dict = {"stdin": subprocess.DEVNULL} if files else {"input": clean_prompt}
    if files:
        command.append(clean_prompt)
    with tempfile.TemporaryDirectory(prefix="clipper-ai-", ignore_cleanup_errors=True) as empty_dir:
        try:
            proc = subprocess.run(
                command, capture_output=True, encoding="utf-8", errors="replace", cwd=empty_dir,
                timeout=timeout,
                env={**os.environ, "PWD": empty_dir, "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                     "OPENCODE_CONFIG_CONTENT": LOCKDOWN_CONFIG},
                **background_subprocess_kwargs(low_priority=True),
                **kwargs,
            )
        except subprocess.TimeoutExpired:
            log.warning("opencode timed out after %ss", timeout)
            return None
        except OSError as exc:
            log.warning("opencode could not start: %s", exc)
            return None
    if proc.returncode != 0:
        log.warning("opencode failed: %s", _safe_utf8(proc.stderr)[-400:])
        return None
    return _text_from_events(proc.stdout) or None


def ask_json(prompt: str, *, files: Sequence[str] = (), model: str = "") -> Optional[dict]:
    """Like :func:`ask`, for a JSON object. Retries once on an unparsable reply."""
    for _attempt in range(2):
        reply = ask(prompt, files=files, model=model)
        if reply is None:
            return None
        parsed = _extract_json(reply)
        if isinstance(parsed, dict):
            return parsed
        log.info("AI reply was not a JSON object: %s", reply[:200])
    return None
