"""Backup API keys: one setting may hold several keys, e.g. ``DEEPGRAM_API_KEY=key1,key2``.

Owners paste a second key "for backup" into the same line. Sent as one string, that value is no key at all and
every call fails with 401, so a key setting is read as a list (commas, semicolons, pipes or whitespace between).
The first key the provider has not refused is used. A 401/403 marks that key refused for ``_REFUSED_SEC`` and the
caller tries the next one. A 429 never switches keys: keys of one account share its rate limit, so switching
would only hide the limit. When every key has been refused they are all tried again in order, so a single key
behaves exactly as it did before this module existed.

Stdlib only, and nothing here logs or returns a key in text: ``label`` gives "key 2 of 3" for log lines.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Iterable

_SPLIT = re.compile(r"[\s,;|]+")
_REFUSED_SEC = 30 * 60
REFUSED_STATUSES = frozenset({401, 403})

_lock = threading.Lock()
_refused: dict[str, float] = {}  # sha256 of a key -> monotonic time its refusal expires


def parse(raw: object) -> list[str]:
    """The distinct keys in a setting's value, in the order written (quotes around the whole value removed)."""
    text = str(raw or "").strip().strip("\"'")
    seen: list[str] = []
    for part in _SPLIT.split(text):
        part = part.strip().strip("\"'")
        if part and part not in seen:
            seen.append(part)
    return seen


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def candidates(raw: object) -> list[str]:
    """Keys to try, in order: those not refused recently first, then the refused ones."""
    keys = parse(raw)
    now = time.monotonic()
    with _lock:
        for digest in [d for d, until in _refused.items() if until <= now]:
            del _refused[digest]
        refused = {d for d in _refused}
    fresh = [k for k in keys if _digest(k) not in refused]
    return fresh + [k for k in keys if _digest(k) in refused]


def first(raw: object) -> str:
    """The key to use now, or "" when the setting is empty."""
    keys = candidates(raw)
    return keys[0] if keys else ""


def refuse(key: str) -> None:
    """The provider rejected this key (401/403): prefer the others for a while."""
    if key:
        with _lock:
            _refused[_digest(key)] = time.monotonic() + _REFUSED_SEC


def accept(key: str) -> None:
    """The provider accepted this key: forget an earlier refusal (a key that was fixed works again at once)."""
    if key:
        with _lock:
            _refused.pop(_digest(key), None)


def label(key: str, raw: object) -> str:
    """'key 2 of 3', for log lines that must never contain the key itself."""
    keys = parse(raw)
    try:
        return f"key {keys.index(key) + 1} of {len(keys)}"
    except ValueError:
        return "key ? of %d" % len(keys)


def parts_of(values: Iterable[object]) -> list[str]:
    """Every individual key in these settings' values, for log scrubbing."""
    out: list[str] = []
    for value in values:
        for key in parse(value):
            if key not in out:
                out.append(key)
    return out


def reset() -> None:
    """Forget every refusal (tests)."""
    with _lock:
        _refused.clear()
