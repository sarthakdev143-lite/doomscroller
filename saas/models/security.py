"""Tables for saas.security - see docs/saas/CONTRACT.md.

Owned by the tracking agent. `user_id` columns are plain indexed strings, never foreign keys: the
users table belongs to another agent and a security event must outlive the user it names.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Index, Integer, String, Text, TypeDecorator,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from ..common import new_id, utcnow
from ..db import Base


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC in and out, on every backend.

    SQLite stores DateTime(timezone=True) without the offset and hands back naive values, and
    comparing a naive value with utcnow() raises. Everything is normalised to UTC on the way in
    (so string comparison in SQLite stays correct) and tagged UTC on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class SecurityEvent(Base):
    """Append-only stream of everything the abuse layer noticed or did."""

    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("se_"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", index=True)  # info|low|medium|high|critical
    ip: Mapped[str | None] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(128), index=True)
    visitor_id: Mapped[str | None] = mapped_column(String(64), index=True)
    path: Mapped[str | None] = mapped_column(String(512))
    detail: Mapped[dict | None] = mapped_column(JSON)


class Ban(Base):
    """A block on an IP (or CIDR), fingerprint, visitor, email, email domain or ASN."""

    __tablename__ = "security_bans"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_security_ban_kind_value"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ban_"))
    kind: Mapped[str] = mapped_column(String(16), index=True)  # ip|fingerprint|visitor|email|email_domain|asn
    value: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class RateCounter(Base):
    """One fixed window of a sliding-window rate limit. Shared by every worker through the DB."""

    __tablename__ = "rate_counters"

    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer, primary_key=True)  # epoch seconds
    count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[int] = mapped_column(Integer, index=True)  # epoch seconds; safe to delete after


class SeenSignal(Base):
    """Which IPs / fingerprints / canonical emails each identity has shown up with.

    The raw material for multi-account clusters: two subjects sharing a value are linked.
    """

    __tablename__ = "seen_signals"
    __table_args__ = (
        UniqueConstraint("kind", "value", "subject_kind", "subject_id", name="uq_seen_signal"),
        Index("ix_seen_signal_kind_value", "kind", "value"),
        Index("ix_seen_signal_subject", "subject_kind", "subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(12))  # ip | fp | email
    value: Mapped[str] = mapped_column(String(160))
    subject_kind: Mapped[str] = mapped_column(String(16))  # visitor | waitlist | user
    subject_id: Mapped[str] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    hits: Mapped[int] = mapped_column(Integer, default=1)


class AuditLog(Base):
    """Every admin action. Written by saas.admin; never edited or deleted by the app."""

    __tablename__ = "admin_audit_log"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("aud_"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor_email: Mapped[str | None] = mapped_column(String(320))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_kind: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(160))
    detail: Mapped[dict | None] = mapped_column(JSON)


class AdminNote(Base):
    """Free-text notes an admin keeps on a user, visitor, waitlist entry, IP or fingerprint."""

    __tablename__ = "admin_notes"
    __table_args__ = (Index("ix_admin_note_subject", "subject_kind", "subject_id"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("note_"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    subject_kind: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(160))
    author_id: Mapped[str | None] = mapped_column(String(64))
    author_email: Mapped[str | None] = mapped_column(String(320))
    body: Mapped[str] = mapped_column(Text)
