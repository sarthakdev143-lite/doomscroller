"""Billing tables: catalog (plans, packs, prices, regional tiers), payments, subscriptions, webhook
events, the credit ledger, promo codes and referrals.

Money is always integer minor units (cents, paise) plus an ISO currency. Credits are integer minutes
of source video. The ledger is append-only: rows are inserted, never updated or deleted, and
`credit_accounts.balance` is the running total guarded by a CHECK so it can never go below zero.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..common import new_id, utcnow
from ..db import Base
from .core import UTCDateTime


class Plan(Base):
    __tablename__ = "billing_plans"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pln_"))
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(400), default="")
    monthly_credits: Mapped[int] = mapped_column(Integer, default=0)
    rollover_months: Mapped[int] = mapped_column(Integer, default=1)  # unused credits carried, in months of credits
    # api, mcp, priority, watermark, max_source_minutes, concurrency, daily_jobs, max_clips
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    public: Mapped[bool] = mapped_column(Boolean, default=True)  # free plan is not purchasable
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Pack(Base):
    __tablename__ = "billing_packs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pck_"))
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(400), default="")
    credits: Mapped[int] = mapped_column(Integer)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Price(Base):
    __tablename__ = "billing_prices"
    __table_args__ = (
        UniqueConstraint("item_kind", "item_slug", "interval", "currency", "tier", name="uq_price_item_tier"),
        CheckConstraint("amount_minor > 0", name="ck_price_positive"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("prc_"))
    item_kind: Mapped[str] = mapped_column(String(8))  # plan | pack
    item_slug: Mapped[str] = mapped_column(String(40), index=True)
    interval: Mapped[str] = mapped_column(String(8))  # month | year | once
    currency: Mapped[str] = mapped_column(String(3))  # ISO 4217, upper case
    tier: Mapped[str] = mapped_column(String(24), default="default")
    amount_minor: Mapped[int] = mapped_column(Integer)
    provider_refs: Mapped[dict] = mapped_column(JSON, default=dict)  # {"razorpay_plan_id": "plan_..."}
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class RegionTier(Base):
    """Country -> (currency, price tier). Countries not listed pay the default currency at the default tier."""

    __tablename__ = "billing_region_tiers"

    country: Mapped[str] = mapped_column(String(2), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    tier: Mapped[str] = mapped_column(String(24), default="default")
    note: Mapped[str] = mapped_column(String(200), default="")


class Subscription(Base):
    __tablename__ = "billing_subscriptions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sub_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_slug: Mapped[str] = mapped_column(String(40))
    interval: Mapped[str] = mapped_column(String(8))  # month | year
    status: Mapped[str] = mapped_column(String(16), default="incomplete")  # incomplete|active|past_due|canceled
    provider: Mapped[str] = mapped_column(String(16))
    provider_subscription_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    provider_customer_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(Integer)  # full price per interval (renewals are checked against it)
    monthly_credits: Mapped[int] = mapped_column(Integer, default=0)  # snapshot at purchase
    current_period_start: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    current_period_end: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    next_grant_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    grants: Mapped[int] = mapped_column(Integer, default=0)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    canceled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Payment(Base):
    """Our own order record. Webhooks are checked against it; client-sent amounts are never trusted."""

    __tablename__ = "billing_payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_order_id", name="uq_payment_provider_order"),
        CheckConstraint("amount_minor >= 0", name="ck_payment_amount"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pay_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(8))  # plan | pack
    item_slug: Mapped[str] = mapped_column(String(40))
    interval: Mapped[str] = mapped_column(String(8), default="once")
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(Integer)  # what the customer must pay, after discount
    list_amount_minor: Mapped[int] = mapped_column(Integer, default=0)  # before discount
    credits: Mapped[int] = mapped_column(Integer, default=0)  # pack credits (plans grant via the subscription)
    status: Mapped[str] = mapped_column(String(20), default="created")
    # created | paid | failed | canceled | refunded | partially_refunded | disputed
    provider_order_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # session / link / invoice id
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(
        String(40), ForeignKey("billing_subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    promo_code_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    tier: Mapped[str] = mapped_column(String(24), default="default")
    refunded_minor: Mapped[int] = mapped_column(Integer, default=0)
    credits_reversed: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    paid_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class WebhookEvent(Base):
    __tablename__ = "billing_webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("whe_"))
    provider: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[str] = mapped_column(String(160))
    event_type: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(20), default="processed")  # processed | ignored | rejected
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CreditAccount(Base):
    """The running balance, one row per user. The ledger explains it; this row enforces it."""

    __tablename__ = "credit_accounts"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_credit_balance_nonneg"),
        CheckConstraint("sub_balance >= 0", name="ck_credit_sub_nonneg"),
    )

    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    # The part of the balance that came from subscription grants and is subject to the rollover cap.
    sub_balance: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CreditEntry(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("led_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # grant | purchase | subscription_grant | reserve | settle | release | refund | expire | admin_adjust |
    # promo | referral
    kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[int] = mapped_column(Integer)  # signed
    balance_after: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # admin who adjusted
    note: Mapped[str] = mapped_column(String(300), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


Index("ix_credit_ledger_user_created", CreditEntry.user_id, CreditEntry.created_at)


class PromoCode(Base):
    __tablename__ = "billing_promo_codes"
    __table_args__ = (CheckConstraint("redemptions >= 0", name="ck_promo_redemptions"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("prm_"))
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)  # stored upper case
    kind: Mapped[str] = mapped_column(String(16))  # credits | percent_off
    credits: Mapped[int] = mapped_column(Integer, default=0)
    percent_off: Mapped[int] = mapped_column(Integer, default=0)  # 1-100
    applies_to: Mapped[str] = mapped_column(String(8), default="any")  # any | plan | pack
    max_redemptions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = unlimited
    redemptions: Mapped[int] = mapped_column(Integer, default=0)
    first_purchase_only: Mapped[bool] = mapped_column(Boolean, default=False)
    starts_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PromoRedemption(Base):
    __tablename__ = "billing_promo_redemptions"
    __table_args__ = (UniqueConstraint("promo_id", "user_id", name="uq_promo_user"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("prr_"))
    promo_id: Mapped[str] = mapped_column(String(40), ForeignKey("billing_promo_codes.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="applied")  # applied | pending | released
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Referral(Base):
    __tablename__ = "billing_referrals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ref_"))
    referrer_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    referred_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | rewarded | void
    reward_credits: Mapped[int] = mapped_column(Integer, default=0)
    payment_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    reason: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    rewarded_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)

