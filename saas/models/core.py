"""Core identity tables: users, sessions, email tokens, TOTP, recovery codes, Google identities, API keys.

Owned by the core agent (docs/saas/CONTRACT.md). Every table name is prefixed so no other agent's
model can collide with it, and foreign keys only ever point at tables in this module or billing/jobs.

Secrets are never stored: session tokens, email tokens, recovery codes and API keys keep only a
SHA-256 of a high-entropy random value, and the TOTP secret is AES-GCM encrypted with a key derived
from SAAS_SECRET_KEY (saas.auth.crypto).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ..common import new_id, utcnow
from ..db import Base


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC in Python on every backend.

    SQLite has no timezone type and hands back naive datetimes even from DateTime(timezone=True), so
    every `expires_at < utcnow()` would raise. Values are stored as naive UTC on SQLite and as
    timestamptz on Postgres, and always come back aware.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _json_default_dict() -> dict:
    return {}


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr_"))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # normalised, lowercase
    # Gmail dots / +tags removed: one free grant per inbox, however the address is spelled.
    email_canonical: Mapped[str] = mapped_column(String(320), index=True, default="")
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")  # user | admin
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | suspended | banned | deleted
    plan_slug: Mapped[str] = mapped_column(String(40), default="free")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    signup_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    signup_fingerprint: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    signup_country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    signup_user_agent: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    visitor_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    referral_code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    referred_by: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)  # users.id
    signup_risk: Mapped[dict] = mapped_column(JSON, default=_json_default_dict)
    free_credits_granted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    first_paid_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    billing_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    flags: Mapped[dict] = mapped_column(JSON, default=_json_default_dict)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

    @property
    def is_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class AuthSession(Base):
    """A server-side browser session. The cookie carries a random token; only its SHA-256 is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ses_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)  # absolute expiry
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    method: Mapped[str] = mapped_column(String(16), default="password")  # password | magic | google | signup
    mfa_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)  # TOTP passed in this session
    reauth_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class EmailToken(Base):
    """Single-use, expiring email links: verify | reset | magic."""

    __tablename__ = "auth_email_tokens"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("etk_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320))  # the address it was sent to; a changed email voids it
    next_path: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class TotpSecret(Base):
    __tablename__ = "auth_totp"

    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    secret_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    last_step: Mapped[int] = mapped_column(Integer, default=0)  # replay guard: a code works once


class RecoveryCode(Base):
    __tablename__ = "auth_recovery_codes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rec_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class MfaChallenge(Base):
    """The gap between a correct password and a correct TOTP code. Never a full session."""

    __tablename__ = "auth_mfa_challenges"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("mfa_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    method: Mapped[str] = mapped_column(String(16), default="password")
    next_path: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class Identity(Base):
    """An external login (Google) linked to a user."""

    __tablename__ = "auth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("idn_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class OAuthState(Base):
    """One Google sign-in attempt: state, PKCE verifier and nonce, bound to the browser by a cookie."""

    __tablename__ = "auth_oauth_states"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ost_"))
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code_verifier: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[str] = mapped_column(String(64))
    next_path: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=_json_default_dict)  # invite, ref, fp, link_user_id
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("key_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    prefix: Mapped[str] = mapped_column(String(32))  # shown in lists: dsk_live_AbCd12
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    last_used_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


Index("ix_auth_sessions_user_active", AuthSession.user_id, AuthSession.revoked_at)

