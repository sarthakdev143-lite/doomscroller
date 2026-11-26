"""Security event log. `log()` never raises: a failure to record must not turn into a failed request.

It writes to the caller's session and commits by default, because the usual next step after
noticing abuse is to refuse the request, and a refused request's session is closed uncommitted.
Pass `commit=False` to fold the event into a larger unit of work you commit yourself.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from ..common import utcnow
from ..models.security import SecurityEvent

_logger = logging.getLogger("saas.security.events")

SEVERITIES = ("info", "low", "medium", "high", "critical")


def _clean_detail(detail: Any) -> dict | None:
    if detail is None:
        return None
    if not isinstance(detail, dict):
        detail = {"value": detail}
    out: dict = {}
    for k, v in list(detail.items())[:30]:
        k = str(k)[:64]
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x if isinstance(x, (int, float, bool)) else str(x)[:200] for x in list(v)[:30]]
        else:
            out[k] = str(v)[:500]
    return out


def log(db: Session, kind: str, severity: str = "info", *, ip: str | None = None, user_id: str | None = None,
        fingerprint: str | None = None, detail: Any = None, visitor_id: str | None = None,
        path: str | None = None, commit: bool = True) -> SecurityEvent | None:
    if severity not in SEVERITIES:
        severity = "info"
    ev = SecurityEvent(kind=str(kind)[:64], severity=severity, ip=(ip or None) and str(ip)[:64],
                       user_id=user_id, fingerprint=(fingerprint or None) and str(fingerprint)[:128],
                       visitor_id=visitor_id, path=(path or None) and str(path)[:512],
                       detail=_clean_detail(detail), created_at=utcnow())
    try:
        db.add(ev)
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception as exc:  # pragma: no cover - only on a broken DB
        _logger.warning("could not record security event %s: %s", kind, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    if severity in ("high", "critical"):
        _logger.warning("security event %s (%s) ip=%s user=%s", kind, severity, ip, user_id)
    return ev
