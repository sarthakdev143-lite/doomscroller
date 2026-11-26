"""Bans on IPs/CIDRs, fingerprints, visitors, emails, email domains and ASNs.

The middleware checks IP, ASN and visitor bans on every request from an in-process cache refreshed
every `_TTL` seconds (and immediately after an admin edit in this process). Fingerprint and email
bans are checked where those values arrive: tracker ingest, waitlist joins and signups.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..common import utcnow
from ..models.security import Ban
from . import netutil

log = logging.getLogger("saas.security.bans")

KINDS = ("ip", "fingerprint", "visitor", "email", "email_domain", "asn")
_TTL = 30.0


@dataclass
class _Snapshot:
    loaded_at: float = 0.0
    ips: set[str] = field(default_factory=set)
    nets: list = field(default_factory=list)
    values: dict[str, set[str]] = field(default_factory=dict)


_snap = _Snapshot()
_lock = threading.Lock()


def reset_state() -> None:
    global _snap
    with _lock:
        _snap = _Snapshot()


def invalidate() -> None:
    _snap.loaded_at = 0.0


def _load(db: Session | None = None) -> _Snapshot:
    from ..db import SessionLocal

    snap = _Snapshot(loaded_at=time.time())
    own = db is None
    session = SessionLocal() if own else db
    try:
        now = utcnow()
        rows = session.execute(
            select(Ban.kind, Ban.value).where(Ban.active.is_(True),
                                              or_(Ban.expires_at.is_(None), Ban.expires_at > now))
        ).all()
    except Exception as exc:
        log.debug("ban cache load failed: %s", exc)
        rows = []
    finally:
        if own:
            session.close()
    for kind, value in rows:
        if kind == "ip":
            net = netutil.parse_network(value)
            if net is None:
                continue
            if net.num_addresses == 1:
                snap.ips.add(str(net.network_address))
            else:
                snap.nets.append(net)
        else:
            snap.values.setdefault(kind, set()).add(value)
    return snap


def snapshot(db: Session | None = None) -> _Snapshot:
    global _snap
    if time.time() - _snap.loaded_at > _TTL:
        with _lock:
            if time.time() - _snap.loaded_at > _TTL:
                _snap = _load(db)
    return _snap


def ip_banned(ip: str | None, db: Session | None = None) -> bool:
    norm = netutil.normalise(ip)
    if not norm:
        return False
    snap = snapshot(db)
    return norm in snap.ips or netutil.in_any(norm, snap.nets)


def value_banned(kind: str, value: str | None, db: Session | None = None) -> bool:
    if not value:
        return False
    return _norm_value(kind, value) in snapshot(db).values.get(kind, set())


def check(db: Session | None = None, *, ip: str | None = None, fingerprint: str | None = None,
          visitor_id: str | None = None, email: str | None = None, asn: int | None = None) -> str | None:
    """The first ban that matches, as 'kind', or None."""
    if ip and ip_banned(ip, db):
        return "ip"
    if fingerprint and value_banned("fingerprint", fingerprint, db):
        return "fingerprint"
    if visitor_id and value_banned("visitor", visitor_id, db):
        return "visitor"
    if asn and value_banned("asn", str(asn), db):
        return "asn"
    if email:
        if value_banned("email", email, db):
            return "email"
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if domain and value_banned("email_domain", domain, db):
            return "email_domain"
    return None


def _norm_value(kind: str, value: str) -> str:
    value = str(value).strip()
    if kind in ("email", "email_domain", "fingerprint"):
        return value.lower()
    if kind == "asn":
        return value.upper().removeprefix("AS")
    return value


def validate(kind: str, value: str) -> str:
    """Normalised value, or ValueError with a human message."""
    if kind not in KINDS:
        raise ValueError(f"unknown ban kind {kind!r}")
    value = (value or "").strip()
    if not value:
        raise ValueError("value is required")
    if kind == "ip":
        net = netutil.parse_network(value)
        if net is None:
            raise ValueError("not an IP address or CIDR network")
        if net.prefixlen < (8 if net.version == 4 else 32):
            raise ValueError("network too broad to ban")
        return str(net.network_address) if net.num_addresses == 1 else str(net)
    if kind == "email" and "@" not in value:
        raise ValueError("not an email address")
    if kind == "asn" and not _norm_value(kind, value).isdigit():
        raise ValueError("ASN must be a number like 14061 or AS14061")
    return _norm_value(kind, value)[:255]


def add(db: Session, kind: str, value: str, reason: str = "", *, created_by: str | None = None,
        hours: float | None = None) -> Ban:
    value = validate(kind, value)
    expires: datetime | None = utcnow() + timedelta(hours=hours) if hours else None
    ban = db.execute(select(Ban).where(Ban.kind == kind, Ban.value == value)).scalar_one_or_none()
    if ban is None:
        ban = Ban(kind=kind, value=value)
        db.add(ban)
    ban.reason = (reason or "")[:500]
    ban.created_by = created_by
    ban.created_at = utcnow()
    ban.expires_at = expires
    ban.active = True
    db.flush()
    invalidate()
    return ban


def remove(db: Session, ban_id: str) -> Ban | None:
    ban = db.get(Ban, ban_id)
    if ban is not None:
        ban.active = False
        db.flush()
        invalidate()
    return ban
