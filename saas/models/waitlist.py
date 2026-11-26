"""Tables for saas.waitlist - see docs/saas/CONTRACT.md. Owned by the tracking agent.

Position in line is derived, never stored: entries are ordered by `rank_key` (their join order
`seq`, minus `waitlist_referral_bump` places per counted referral), then `seq`.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..common import new_id, utcnow
from ..db import Base
from .security import UTCDateTime


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    __table_args__ = (Index("ix_waitlist_rank", "status", "rank_key", "seq"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("wl_"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # normalised
    email_canonical: Mapped[str] = mapped_column(String(320), index=True)  # alias-collapsed, for dedupe
    role: Mapped[str | None] = mapped_column(String(64))
    channel_url: Mapped[str | None] = mapped_column(String(500))

    # pending -> confirmed -> invited -> joined; removed at any point (admin / unsubscribe).
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    rank_key: Mapped[int] = mapped_column(Integer, index=True)

    confirm_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    confirm_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    confirm_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    confirm_sends: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    confirm_ip: Mapped[str | None] = mapped_column(String(64))
    confirm_fingerprint: Mapped[str | None] = mapped_column(String(64))

    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    referred_by_id: Mapped[str | None] = mapped_column(String(40), index=True)
    # Set on the *referred* entry: pending (until confirmed) | counted | rejected
    referral_status: Mapped[str | None] = mapped_column(String(16))
    referral_reason: Mapped[str | None] = mapped_column(String(64))
    referrals_counted: Mapped[int] = mapped_column(Integer, default=0)

    ip: Mapped[str | None] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    visitor_id: Mapped[str | None] = mapped_column(String(40), index=True)
    country: Mapped[str | None] = mapped_column(String(2))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[dict | None] = mapped_column(JSON)  # first-touch referrer / utm, when tracked

    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_reasons: Mapped[list | None] = mapped_column(JSON)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # held from invites

    invite_code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    invite_batch_id: Mapped[str | None] = mapped_column(String(40), index=True)
    invited_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    invite_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    invite_sends: Mapped[int] = mapped_column(Integer, default=0)
    redeemed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    redeemed_user_id: Mapped[str | None] = mapped_column(String(64))

    notes: Mapped[str | None] = mapped_column(Text)


class InviteBatch(Base):
    __tablename__ = "waitlist_invite_batches"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ib_"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    requested: Mapped[int] = mapped_column(Integer, default=0)
    invited: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(500))
