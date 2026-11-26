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

``CLIP_AI_BACKEND=api`` replaces both CLIs with an HTTP API, for hosts with no logged-in
CLI such as the SaaS: the Anthropic Messages API (``CLIP_AI_API_PROVIDER=anthropic``,
``ANTHROPIC_API_KEY``) or any OpenAI-compatible chat endpoint (``openai_compatible``, with
``CLIP_AI_API_BASE_URL``, ``CLIP_AI_API_KEY`` and ``CLIP_AI_API_MODEL``: Gemini's and Groq's
free tiers both work). The request carries the same system prompt and no tools, images
travel inline as base64, and every call's token usage is recorded through
:mod:`app.clipper.metering`.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

import httpx

from app.config import settings
from app.metadata import _extract_json
from app.winproc import background_subprocess_kwargs

from . import metering

log = logging.getLogger(__name__)

LOCKDOWN_CONFIG = json.dumps({"permission": {"*": "deny"}, "tools": {"*": False}})
_CLAUDE_SYSTEM = ("You answer requests from a video-editing program. You have no tools and cannot run anything. "
                  "Reply with exactly what is asked, in the requested format, and nothing else.")
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
_OPENCODE_REST_SEC = 30 * 60
_opencode_down_until = 0.0
_state_lock = threading.Lock()
_model_down_until: dict[str, float] = {}
_last_provider = threading.local()
_COOLDOWN_ERROR_RE = re.compile(
    r"freeusagelimiterror|rate[_ -]?limit|too many requests|\b429\b|quota|provider\.no-route|model[_ -]?not[_ -]?found|model unavailable",
    re.IGNORECASE,
)


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


_CMD_EXE_RE = re.compile(r'"%(?:~)?dp0%?[\\/]([^"\r\n]+?\.exe)"', re.IGNORECASE)
_CMD_JS_RE = re.compile(r'"%(?:~)?dp0%?[\\/]([^"\r\n]+?\.(?:c|m)?js)"', re.IGNORECASE)


def _unwrap_cmd_shim(path: Path) -> Optional[list[str]]:
    """Resolve an npm-style .cmd/.bat shim to its direct .exe or [node, .js] command so cmd.exe never truncates args."""
    if not path.is_file() or path.suffix.lower() not in {".cmd", ".bat"}:
        return None
    try:
        with path.open("rb") as fh:
            text = fh.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return None
    m_exe = _CMD_EXE_RE.search(text)
    if m_exe:
        target = (path.parent / m_exe.group(1).replace("\\", "/")).resolve()
        if target.is_file():
            return [str(target)]
    m_js = _CMD_JS_RE.search(text)
    if m_js:
        target = (path.parent / m_js.group(1).replace("\\", "/")).resolve()
        if target.is_file():
            sibling = path.parent / "node.exe"
            node = str(sibling.resolve()) if sibling.is_file() else shutil.which("node")
            if node:
                return [node, str(target)]
    return None


def _command_prefix(binary: str) -> list[str]:
    """Build argv[0..] for ``binary``, unwrapping Windows .cmd shims and ``#!`` scripts."""
    if os.name == "nt":
        p = Path(binary)
        unwrapped = _unwrap_cmd_shim(p)
        if unwrapped:
            return unwrapped
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


def backend() -> str:
    """"api" when CLIP_AI_BACKEND asks for an HTTP API, else "cli" (the owner's default)."""
    return "api" if str(getattr(settings, "clip_ai_backend", "") or "").strip().lower() == "api" else "cli"


ANTHROPIC, OPENAI_COMPATIBLE = "anthropic", "openai_compatible"


def api_provider() -> str:
    raw = str(getattr(settings, "clip_ai_api_provider", "") or "").strip().lower().replace("-", "_")
    return OPENAI_COMPATIBLE if raw in ("openai_compatible", "openai", "gemini", "groq") else ANTHROPIC


def _api_key() -> str:
    generic = str(getattr(settings, "clip_ai_api_key", "") or "").strip()
    if api_provider() == ANTHROPIC:
        return generic or (settings.anthropic_api_key or "").strip()
    return generic


def _api_base() -> str:
    configured = str(getattr(settings, "clip_ai_api_base_url", "") or "").strip().rstrip("/")
    if configured:
        return configured
    return "https://api.anthropic.com" if api_provider() == ANTHROPIC else ""


def available() -> bool:
    if backend() == "api":
        return bool(_api_key() and _api_base() and _api_model(()))
    return opencode_path() is not None or claude_path() is not None


# ---- HTTP API backends: the Anthropic Messages API, or any OpenAI-compatible chat endpoint
# (Gemini's and Groq's free tiers both speak it). One request per call, no tools, the same system
# prompt as the Claude CLI, images inline as base64, and every call's token usage metered.

_API_VERSION = "2026-10-21"
_API_MAX_TOKENS = 16000
_OPENAI_MAX_TOKENS = 8192
_API_RETRIES = 2
_API_MAX_WAIT_SEC = 30.0
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# Models that take the server-side refusal fallback; a declined request is re-run on another model.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5-2", "claude-sonnet-5-5"}


def _api_model(files: Sequence[str]) -> str:
    vision = str(getattr(settings, "clip_ai_api_vision_model", "") or "").strip()
    text = str(getattr(settings, "clip_ai_api_model", "") or "").strip()
    if not text and api_provider() == ANTHROPIC:
        # Cheapest current model: the owner runs on a zero budget. Name a bigger one in CLIP_AI_API_MODEL.
        text = "claude-haiku-5-5"
    return (vision or text) if files else text


def _images(files: Sequence[str]) -> list[tuple[str, str]]:
    found = []
    for path in files:
        media = _IMAGE_TYPES.get(Path(path).suffix.lower())
        if media is None:
            raise ValueError(f"cannot send {Path(path).name} to the API: not an image")
        found.append((media, base64.b64encode(Path(path).read_bytes()).decode("ascii")))
    return found


def _api_content(prompt: str, files: Sequence[str]) -> list[dict]:
    content: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
                           for media, data in _images(files)]
    content.append({"type": "text", "text": _safe_utf8(prompt)})
    return content


def _openai_content(prompt: str, files: Sequence[str]) -> object:
    if not files:
        return _safe_utf8(prompt)
    content: list[dict] = [{"type": "text", "text": _safe_utf8(prompt)}]
    content += [{"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
                for media, data in _images(files)]
    return content


def _api_scrub(text: object) -> str:
    out = str(text or "")
    for key in {(settings.anthropic_api_key or "").strip(), str(getattr(settings, "clip_ai_api_key", "") or "").strip()}:
        if len(key) >= 3:
            out = out.replace(key, "[REDACTED]")
    return out


def _retry_wait(response: Optional[httpx.Response], attempt: int) -> float:
    try:
        wait = float(response.headers.get("retry-after", "")) if response is not None else 0.0
    except ValueError:
        wait = 0.0
    if not math.isfinite(wait) or wait <= 0:
        wait = 2.0 * (attempt + 1)
    return min(_API_MAX_WAIT_SEC, wait)


def _anthropic_request(prompt: str, files: Sequence[str], model: str, key: str, base: str) -> tuple[str, dict, dict]:
    body: dict = {
        "model": model,
        "max_tokens": _API_MAX_TOKENS,
        "system": _CLAUDE_SYSTEM,
        "messages": [{"role": "user", "content": _api_content(prompt, files)}],
    }
    effort = str(getattr(settings, "clip_ai_api_effort", "") or "").strip().lower()
    if effort in _EFFORTS:
        body["output_config"] = {"effort": effort}
    headers = {"x-api-key": key, "anthropic-version": _API_VERSION, "content-type": "application/json"}
    if getattr(settings, "clip_ai_api_fallbacks", True) and model in _FALLBACK_MODELS:
        headers["anthropic-beta"] = _FALLBACK_BETA
        body["fallbacks"] = "default"
    return f"{base}/v1/messages", headers, body


def _anthropic_reply(data: dict) -> tuple[Optional[str], dict]:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    counts = {"input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0),
              "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
              "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0)}
    if data.get("stop_reason") == "refusal":
        log.warning("anthropic api declined the request (%s)", (data.get("stop_details") or {}).get("category"))
        return None, counts
    blocks = data.get("content") if isinstance(data.get("content"), list) else []
    text = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    return text.strip() or None, counts


def _openai_request(prompt: str, files: Sequence[str], model: str, key: str, base: str) -> tuple[str, dict, dict]:
    body = {
        "model": model,
        "max_tokens": _OPENAI_MAX_TOKENS,
        "messages": [{"role": "system", "content": _CLAUDE_SYSTEM},
                     {"role": "user", "content": _openai_content(prompt, files)}],
    }
    headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    return f"{base}/chat/completions", headers, body


def _openai_reply(data: dict) -> tuple[Optional[str], dict]:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    counts = {"input_tokens": int(usage.get("prompt_tokens") or 0), "output_tokens": int(usage.get("completion_tokens") or 0)}
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    if choices and isinstance(choices[0], dict) and choices[0].get("finish_reason") == "content_filter":
        log.warning("%s declined the request", api_provider())
        return None, counts
    text = str(content or "").strip()
    return text or None, counts


def _ask_api(prompt: str, *, files: Sequence[str] = ()) -> Optional[str]:
    """One HTTP API call. None on any failure, a refusal, or an empty reply; never raises."""
    key, base, provider = _api_key(), _api_base(), api_provider()
    model = _api_model(files)
    if not key or not base or not model:
        return None
    build, parse = ((_anthropic_request, _anthropic_reply) if provider == ANTHROPIC
                    else (_openai_request, _openai_reply))
    try:
        url, headers, body = build(prompt, files, model, key, base)
    except (OSError, ValueError) as exc:
        log.warning("%s api: %s", provider, exc)
        return None
    timeout = httpx.Timeout(_timeout(), connect=15)

    def spent(ok: bool, counts: Optional[dict] = None, served_by: str = "") -> None:
        metering.record("llm", provider=provider, model=served_by or model, ok=ok, images=len(files),
                        **{"input_tokens": 0, "output_tokens": 0, **(counts or {})})

    for attempt in range(_API_RETRIES + 1):
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            if attempt < _API_RETRIES:
                time.sleep(_retry_wait(None, attempt))
                continue
            log.warning("%s api could not be reached: %s", provider, type(exc).__name__)
            spent(False)
            return None
        status = response.status_code
        if (status in (408, 409, 429) or status >= 500) and attempt < _API_RETRIES:
            time.sleep(_retry_wait(response, attempt))
            continue
        try:
            data = response.json()
        except ValueError:
            data = None
        if status != 200 or not isinstance(data, dict):
            error = data.get("error") if isinstance(data, dict) else None
            detail = error.get("message") if isinstance(error, dict) else (error or "")
            log.warning("%s api returned %s: %s", provider, status, _api_scrub(str(detail)[:300]))
            spent(False)
            return None
        text, counts = parse(data)
        spent(text is not None, counts, str(data.get("model") or ""))
        return text
    return None


def _events_text_and_error(stdout: str | bytes) -> tuple[str, str]:
    parts: list[str] = []
    errors: list[str] = []
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
            err_msg = str(event)[:400]
            log.warning("opencode reported an error: %s", err_msg)
            errors.append(err_msg)
    return "".join(parts).strip(), "\n".join(errors)


def _text_from_events(stdout: str | bytes) -> str:
    return _events_text_and_error(stdout)[0]


def ask(prompt: str, *, files: Sequence[str] = (), model: str = "") -> Optional[str]:
    """Return the model's reply, or None when every route fails."""
    global _opencode_down_until
    _last_provider.name = None
    if backend() == "api":
        reply = _ask_api(prompt, files=files)
        if reply is not None:
            _last_provider.name = api_provider()
        return reply
    with _state_lock:
        opencode_down_until = _opencode_down_until
    if opencode_path() and time.monotonic() >= opencode_down_until:
        reply = _ask_opencode(prompt, files=files, model=model)
        if reply is not None:
            _last_provider.name = "opencode"
            return reply
        if claude_path():
            with _state_lock:
                _opencode_down_until = time.monotonic() + _OPENCODE_REST_SEC
            log.warning("opencode failed; using Claude for the next %d minutes", _OPENCODE_REST_SEC // 60)
    if claude_path():
        reply = _ask_claude(prompt, files=files)
        if reply is not None:
            _last_provider.name = "claude"
        return reply
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


def _usage_from_stream(stdout: str | bytes) -> dict[str, int]:
    """Token counts from the Claude CLI's result event, or zeros when it carries none."""
    for line in _safe_utf8(stdout).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
            try:
                return {
                    "input_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                }
            except (TypeError, ValueError):
                return {}
    return {}


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
        metering.record("llm", provider="claude-cli", model=settings.claude_model, images=len(files), ok=False,
                        input_tokens=0, output_tokens=0)
        return None
    result = _result_from_stream(proc.stdout)
    usage = _usage_from_stream(proc.stdout)
    metering.record("llm", provider="claude-cli", model=settings.claude_model, images=len(files),
                    ok=result is not None, input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0))
    return result


def _model_candidates(model: str) -> list[str]:
    """Split comma-separated OpenCode models into a deduplicated preference chain."""
    raw = str(model or settings.opencode_model or "")
    seen: list[str] = []
    for part in raw.split(","):
        cand = part.strip()
        if cand and cand not in seen:
            seen.append(cand)
    return seen or [""]


def _ask_opencode(prompt: str, *, files: Sequence[str] = (), model: str = "") -> Optional[str]:
    binary = opencode_path()
    if binary is None:
        return None
    timeout = _timeout()
    clean_prompt = _safe_utf8(prompt)
    prefix = _command_prefix(binary)
    candidates = _model_candidates(model)
    now = time.monotonic()
    with _state_lock:
        active = [cand for cand in candidates if not cand or _model_down_until.get(cand, 0.0) <= now]
    if not active:
        return None

    for cand in active:
        command = [*prefix, "run", "--standalone", "--format", "json"]
        if cand:
            command += ["--model", cand]
        for path in files:
            command += ["--file", str(path)]
        # With attachments the CLI takes its message as an argument. Prompts that
        # carry attachments are short, so the argument limit does not apply.
        kwargs: dict = {"stdin": subprocess.DEVNULL} if files else {"input": clean_prompt}
        if files:
            arg_prompt = (
                re.sub(r"\s*\r?\n\s*", " ", clean_prompt)
                if (prefix and prefix[0].lower().endswith((".cmd", ".bat")))
                else clean_prompt
            )
            command.append(arg_prompt)
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
                log.warning("opencode timed out after %ss (%s)", timeout, cand or "default")
                metering.record("llm", provider="opencode", model=cand or "default", images=len(files), ok=False,
                                input_tokens=0, output_tokens=0)
                return None
            except OSError as exc:
                log.warning("opencode could not start: %s", exc)
                return None
        if proc.returncode != 0:
            metering.record("llm", provider="opencode", model=cand or "default", images=len(files), ok=False,
                            input_tokens=0, output_tokens=0)
            # --format json writes structured errors to stdout while stderr is often empty.
            err_text = (_safe_utf8(proc.stderr) or _safe_utf8(proc.stdout)).strip()
            log.warning("opencode failed (%s): %s", cand or "default", err_text[-400:])
            if cand and _COOLDOWN_ERROR_RE.search(err_text):
                with _state_lock:
                    _model_down_until[cand] = time.monotonic() + _OPENCODE_REST_SEC
            continue
        reply, event_err = _events_text_and_error(proc.stdout)
        metering.record("llm", provider="opencode", model=cand or "default", images=len(files), ok=bool(reply),
                        input_tokens=0, output_tokens=0)
        if reply:
            return reply
        if cand and event_err and _COOLDOWN_ERROR_RE.search(event_err):
            with _state_lock:
                _model_down_until[cand] = time.monotonic() + _OPENCODE_REST_SEC
    return None


_IMAGE_JSON_PREFIX = "Answer with one JSON object only, in the format asked for at the end. Do not describe the images.\n\n"
_STRICT_JSON_SUFFIX = (
    "\n\nCRITICAL: Respond with ONLY a single valid raw JSON object starting with { and ending with }. "
    "No markdown headings, no code fences, no explanation."
)


def ask_json(prompt: str, *, files: Sequence[str] = (), model: str = "") -> Optional[dict]:
    """Like :func:`ask`, for a JSON object. Retries once with strict raw-JSON enforcement.

    With images attached the format is also stated first: vision models shown a contact sheet tend to
    start describing it ("I can see the 8 vertical video shots...") and never reach the JSON, which the
    owner's log shows 87 times, each one costing the job its B-roll.
    """
    _last_provider.name = None
    if files:
        prompt = f"{_IMAGE_JSON_PREFIX}{prompt}"
    for attempt in range(2):
        req_prompt = prompt if attempt == 0 else f"{prompt.rstrip()}{_STRICT_JSON_SUFFIX}"
        reply = ask(req_prompt, files=files, model=model)
        if reply is None:
            return None
        parsed = _extract_json(reply)
        if isinstance(parsed, dict):
            return parsed
        log.info("AI reply was not a JSON object: %s", reply[:200])
    if backend() == "cli" and getattr(_last_provider, "name", None) == "opencode" and claude_path():
        fallback = _ask_claude(f"{prompt.rstrip()}{_STRICT_JSON_SUFFIX}", files=files)
        if fallback:
            parsed = _extract_json(fallback)
            if isinstance(parsed, dict):
                return parsed
    return None
