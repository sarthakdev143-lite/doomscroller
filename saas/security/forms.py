"""Cheap bot filters for identity-creating forms: honeypot, fill timing and fingerprint shape.

Forms post `website` (a honeypot field hidden from humans; must stay empty), `ts` (ms epoch when
the form rendered, set by the page) and `fp` (t.js's fingerprint hash). None of these stop a
determined attacker on their own; they remove the cheap scripted traffic so Turnstile, rate limits
and the risk score only have to deal with the rest.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..config import settings

_FP_RE = re.compile(r"^[a-f0-9]{16,64}$")


@dataclass
class FormCheck:
    reasons: list[str] = field(default_factory=list)
    honeypot: bool = False
    too_fast: bool = False
    fill_ms: int | None = None

    @property
    def is_bot(self) -> bool:
        return self.honeypot or self.too_fast


def clean_fingerprint(fp: object) -> str | None:
    """The fingerprint if it has t.js's shape (16-64 lowercase hex), else None."""
    if not isinstance(fp, str):
        return None
    fp = fp.strip().lower()
    return fp if _FP_RE.match(fp) else None


def check(*, honeypot: object = None, ts: object = None, now_ms: int | None = None) -> FormCheck:
    out = FormCheck()
    if honeypot not in (None, "") and str(honeypot).strip():
        out.honeypot = True
        out.reasons.append("honeypot_filled")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        rendered = int(float(ts)) if ts not in (None, "") else None
    except (TypeError, ValueError):
        rendered = None
    if rendered is None:
        out.reasons.append("form_ts_missing")
        return out
    if rendered < 10_000_000_000:  # seconds, not ms: tolerate pages that sent Date.now()/1000
        rendered *= 1000
    fill = now_ms - rendered
    out.fill_ms = fill
    if fill < 0:
        # A client clock running ahead (ts is usually the browser's Date.now()). Not proof of a
        # bot, and refusing it would lock out everyone with a wrong clock.
        out.reasons.append("form_ts_future")
    elif fill < settings.form_min_fill_ms:
        out.too_fast = True
        out.reasons.append("form_too_fast")
    elif fill > settings.form_max_age_hours * 3_600_000:
        out.reasons.append("form_stale")
    return out
