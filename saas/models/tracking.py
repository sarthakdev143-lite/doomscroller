"""Tables for saas.tracking - see docs/saas/CONTRACT.md.

Owned by the tracking agent. Visitors only exist once analytics are allowed for them (consent
granted, or implied in notice-only regions); nothing here is written for a visitor who declined.
Raw events are kept `event_retention_days`, replay chunks `replay_retention_days`.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, Float, Index, Integer, LargeBinary, String,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from ..common import new_id, utcnow
from ..db import Base
from .security import UTCDateTime

# SQLite only autoincrements INTEGER PRIMARY KEY; Postgres wants BIGINT for an event table.
_BigId = BigInteger().with_variant(Integer, "sqlite")


class Visitor(Base):
    """One browser (first-party `vid` cookie), enriched server-side and scored."""

    __tablename__ = "visitors"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # v_<32 hex>, from the vid cookie
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)

    first_ip: Mapped[str | None] = mapped_column(String(64))
    last_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    country: Mapped[str | None] = mapped_column(String(2), index=True)
    region: Mapped[str | None] = mapped_column(String(128))
    city: Mapped[str | None] = mapped_column(String(128))
    geo_timezone: Mapped[str | None] = mapped_column(String(64))
    asn: Mapped[int | None] = mapped_column(Integer)
    asn_org: Mapped[str | None] = mapped_column(String(255))
    is_datacenter: Mapped[bool] = mapped_column(Boolean, default=False)
    is_tor: Mapped[bool] = mapped_column(Boolean, default=False)
    is_vpn: Mapped[bool] = mapped_column(Boolean, default=False)

    user_agent: Mapped[str | None] = mapped_column(String(512))
    browser: Mapped[str | None] = mapped_column(String(64))
    browser_version: Mapped[str | None] = mapped_column(String(32))
    os: Mapped[str | None] = mapped_column(String(64))
    os_version: Mapped[str | None] = mapped_column(String(32))
    device_type: Mapped[str | None] = mapped_column(String(16), index=True)  # desktop|mobile|tablet|bot|other
    device_brand: Mapped[str | None] = mapped_column(String(64))
    device_model: Mapped[str | None] = mapped_column(String(64))
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    bot_reasons: Mapped[list | None] = mapped_column(JSON)

    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    client: Mapped[dict | None] = mapped_column(JSON)  # screen, languages, tz, cores, memory, touch, automation

    consent: Mapped[str] = mapped_column(String(16), default="implied", index=True)  # granted|implied|denied
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    first_referrer: Mapped[str | None] = mapped_column(String(1024))
    first_referrer_host: Mapped[str | None] = mapped_column(String(255))
    first_source: Mapped[str | None] = mapped_column(String(128), index=True)
    first_medium: Mapped[str | None] = mapped_column(String(128))
    first_campaign: Mapped[str | None] = mapped_column(String(255))
    first_term: Mapped[str | None] = mapped_column(String(255))
    first_landing: Mapped[str | None] = mapped_column(String(512))

    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    waitlist_id: Mapped[str | None] = mapped_column(String(64), index=True)

    sessions: Mapped[int] = mapped_column(Integer, default=0)
    pageviews: Mapped[int] = mapped_column(Integer, default=0)
    events: Mapped[int] = mapped_column(Integer, default=0)
    active_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    stats: Mapped[dict | None] = mapped_column(JSON)  # rolling aggregates the lead score reads

    lead_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    lead_label: Mapped[str | None] = mapped_column(String(32), index=True)
    lead_reasons: Mapped[list | None] = mapped_column(JSON)
    interests: Mapped[list | None] = mapped_column(JSON)
    risk_flags: Mapped[list | None] = mapped_column(JSON)


class TrackSession(Base):
    """A visit: events from one visitor with no gap over 30 minutes. Sessionised server-side."""

    __tablename__ = "track_sessions"
    __table_args__ = (Index("ix_track_session_visitor_last", "visitor_id", "last_seen_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ts_"))
    visitor_id: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(128))
    browser: Mapped[str | None] = mapped_column(String(64))
    os: Mapped[str | None] = mapped_column(String(64))
    device_type: Mapped[str | None] = mapped_column(String(16))
    referrer: Mapped[str | None] = mapped_column(String(1024))
    referrer_host: Mapped[str | None] = mapped_column(String(255), index=True)
    source: Mapped[str | None] = mapped_column(String(128), index=True)
    medium: Mapped[str | None] = mapped_column(String(128))
    campaign: Mapped[str | None] = mapped_column(String(255))
    term: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str | None] = mapped_column(String(255))
    landing_path: Mapped[str | None] = mapped_column(String(512))
    current_path: Mapped[str | None] = mapped_column(String(512))
    current_title: Mapped[str | None] = mapped_column(String(255))
    pageviews: Mapped[int] = mapped_column(Integer, default=0)
    events: Mapped[int] = mapped_column(Integer, default=0)
    active_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    max_scroll: Mapped[int] = mapped_column(Integer, default=0)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    has_replay: Mapped[bool] = mapped_column(Boolean, default=False)
    replay_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    fingerprint: Mapped[str | None] = mapped_column(String(64))


class TrackEvent(Base):
    """One raw tracker event. `x`/`y` are page-relative 0..1 (clicks, heat); never form values."""

    __tablename__ = "track_events"
    __table_args__ = (
        Index("ix_track_event_type_path_ts", "type", "path", "ts"),
        Index("ix_track_event_visitor_ts", "visitor_id", "ts"),
    )

    id: Mapped[int] = mapped_column(_BigId, primary_key=True, autoincrement=True)
    visitor_id: Mapped[str] = mapped_column(String(40))
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    page_id: Mapped[str | None] = mapped_column(String(40), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(16))
    path: Mapped[str | None] = mapped_column(String(512))
    name: Mapped[str | None] = mapped_column(String(128))
    section: Mapped[str | None] = mapped_column(String(64))
    value: Mapped[float | None] = mapped_column(Float)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    props: Mapped[dict | None] = mapped_column(JSON)


class PageView(Base):
    """One page load (`pv` id from t.js): the unit for time-on-page, scroll depth and replays."""

    __tablename__ = "track_pageviews"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # p_<hex> from t.js
    visitor_id: Mapped[str] = mapped_column(String(40), index=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    path: Mapped[str | None] = mapped_column(String(512), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    active_ms: Mapped[int] = mapped_column(Integer, default=0)
    max_scroll: Mapped[int] = mapped_column(Integer, default=0)
    layout: Mapped[list | None] = mapped_column(JSON)  # [{"n": section, "t": top 0..1, "h": height 0..1}]
    has_replay: Mapped[bool] = mapped_column(Boolean, default=False)


class ReplayChunk(Base):
    """A gzip'd JSON array of rrweb events for one page load. Only stored with consent."""

    __tablename__ = "replay_chunks"
    __table_args__ = (UniqueConstraint("page_id", "seq", name="uq_replay_page_seq"),)

    id: Mapped[int] = mapped_column(_BigId, primary_key=True, autoincrement=True)
    visitor_id: Mapped[str] = mapped_column(String(40), index=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    page_id: Mapped[str] = mapped_column(String(40), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    first_ts: Mapped[int | None] = mapped_column(BigInteger)  # rrweb ms timestamps
    last_ts: Mapped[int | None] = mapped_column(BigInteger)
    n_events: Mapped[int] = mapped_column(Integer, default=0)
    size_raw: Mapped[int] = mapped_column(Integer, default=0)
    size_gz: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class VisitorLink(Base):
    """visitor <-> user / waitlist entry, so a user's pre-signup journey (every device) shows."""

    __tablename__ = "visitor_links"
    __table_args__ = (UniqueConstraint("visitor_id", "user_id", "waitlist_id", name="uq_visitor_link"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visitor_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    waitlist_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
