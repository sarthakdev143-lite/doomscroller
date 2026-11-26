"""Checkout, fulfilment, subscriptions, refunds, promo codes and referrals.

Every money path is driven by our own `Payment` row:
* checkout creates it (price looked up server-side, tier from the server-side country),
* a webhook may only move it from created/pending to paid, and only if the provider reports exactly
  our amount and currency (`fulfil_one_time`, `record_subscription_payment`),
* the status transition is a guarded UPDATE and the grant an idempotent ledger entry, so a replayed
  or duplicated webhook changes nothing.
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..auth import compat
from ..auth.errors import ApiError
from ..common import client_country, utcnow
from ..config import settings
from ..models.billing import Payment, PromoCode, PromoRedemption, Referral, Subscription
from ..models.core import User
from . import catalog, ledger, providers

log = logging.getLogger("saas.billing")

OPEN_STATUSES = ("created", "pending")


class RetryLater(Exception):
    """The webhook arrived before something it depends on; answer 5xx so the provider retries."""


# ---- helpers --------------------------------------------------------------------------------------

def add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def from_unix(ts) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def server_country(request: Request) -> Optional[str]:
    """The country used for pricing: Cloudflare's header (when trusted) or the tracker's GeoIP."""
    cc = client_country(request)
    if cc:
        return cc
    try:
        from ..tracking import geo  # optional, owned by the tracking agent

        lookup = getattr(geo, "country_for_request", None) or getattr(geo, "country", None)
        if lookup is not None:
            val = lookup(request)
            if isinstance(val, str) and len(val) == 2:
                return val.upper()
    except Exception:
        pass
    return None


def _flag(user: User, key: str, value) -> None:
    flags = dict(user.flags or {})
    flags[key] = value
    user.flags = flags


# ---- checkout -------------------------------------------------------------------------------------

def start_checkout(db: Session, user: User, request: Request, *, kind: str, slug: str, interval: Optional[str],
                   promo_code: Optional[str]) -> dict:
    catalog.ensure_seeded(db)
    if user.billing_hold:
        raise ApiError(403, "billing_hold", f"Your account has a billing hold. Contact {settings.support_email}.")
    country = server_country(request)
    if kind == "plan":
        interval = interval or "month"
        if interval not in ("month", "year"):
            raise ApiError(400, "invalid_interval", "Choose monthly or yearly billing.")
        plan = catalog.get_plan(db, slug)
        if plan is None or not plan.active or not plan.public:
            raise ApiError(404, "not_found", "That plan doesn't exist.")
        active = (db.query(Subscription)
                  .filter(Subscription.user_id == user.id, Subscription.status.in_(("active", "past_due"))).first())
        if active is not None:
            raise ApiError(409, "already_subscribed",
                           "You already have a subscription. Cancel it first to switch plans.")
        item_name = f"{plan.name} plan ({'monthly' if interval == 'month' else 'yearly'})"
        credits = 0
    elif kind == "pack":
        interval = "once"
        pack = catalog.get_pack(db, slug)
        if pack is None or not pack.active:
            raise ApiError(404, "not_found", "That credit pack doesn't exist.")
        item_name = f"{pack.name} of clip credits"
        credits = pack.credits
    else:
        raise ApiError(400, "invalid_kind", "kind must be 'plan' or 'pack'.")

    price = catalog.price_for(db, kind, slug, interval, country)
    if price is None:
        raise ApiError(404, "not_found", "No price is available for that item.")
    provider = providers.for_currency(price.currency)

    amount = price.amount_minor
    promo: Optional[PromoCode] = None
    if promo_code:
        promo = _checkout_promo(db, user, promo_code, kind)
        amount = max(0, amount - (amount * promo.percent_off) // 100)
        if amount <= 0:
            raise ApiError(400, "promo_not_applicable", "That code can't be used for a free checkout.")

    payment = Payment(user_id=user.id, provider=provider.name, kind=kind, item_slug=slug, interval=interval,
                      currency=price.currency, amount_minor=amount, list_amount_minor=price.amount_minor,
                      credits=credits, status="created", country=country, tier=price.tier,
                      promo_code_id=promo.id if promo else None)
    db.add(payment)
    db.flush()
    sub = None
    if kind == "plan":
        plan = catalog.get_plan(db, slug)
        sub = Subscription(user_id=user.id, plan_slug=slug, interval=interval, status="incomplete",
                           provider=provider.name, currency=price.currency, amount_minor=price.amount_minor,
                           monthly_credits=plan.monthly_credits if plan else 0)
        db.add(sub)
        db.flush()
        payment.subscription_id = sub.id
    if promo is not None:
        _hold_promo(db, promo, user, payment)

    session = provider.create_checkout(providers.CheckoutRequest(
        payment_id=payment.id, kind=kind, item_name=item_name, interval=interval, currency=price.currency,
        amount_minor=amount, customer_email=user.email, user_id=user.id,
        subscription_id=sub.id if sub else None, provider_refs=price.provider_refs or {}))
    payment.provider_order_id = session.provider_order_id
    payment.status = "pending"
    if sub is not None and session.provider_subscription_id:
        sub.provider_subscription_id = session.provider_subscription_id
    db.commit()
    return {"redirect_url": session.redirect_url, "payment_id": payment.id, "provider": provider.name,
            "amount": catalog.format_amount(amount, price.currency)}


# ---- promo codes ----------------------------------------------------------------------------------

def _find_promo(db: Session, code: str) -> PromoCode:
    norm = "".join(ch for ch in (code or "").upper() if ch.isalnum() or ch in "-_")[:40]
    promo = db.query(PromoCode).filter(PromoCode.code == norm).one_or_none() if norm else None
    now = utcnow()
    if (promo is None or not promo.active or (promo.expires_at and promo.expires_at <= now)
            or (promo.starts_at and promo.starts_at > now)):
        raise ApiError(404, "invalid_promo", "That code isn't valid.")
    return promo


def _claim_slot(db: Session, promo: PromoCode) -> None:
    res = db.execute(update(PromoCode)
                     .where(PromoCode.id == promo.id,
                            or_(PromoCode.max_redemptions.is_(None), PromoCode.redemptions < PromoCode.max_redemptions))
                     .values(redemptions=PromoCode.redemptions + 1))
    if res.rowcount != 1:
        raise ApiError(410, "promo_exhausted", "That code has been used up.")


def _checkout_promo(db: Session, user: User, code: str, kind: str) -> PromoCode:
    promo = _find_promo(db, code)
    if promo.kind != "percent_off" or not (1 <= promo.percent_off <= 100):
        raise ApiError(400, "promo_not_applicable", "That code adds credits; redeem it on the billing page instead.")
    if kind != "pack" or promo.applies_to not in ("any", "pack"):
        raise ApiError(400, "promo_not_applicable", "That code only applies to credit packs.")
    if promo.first_purchase_only and user.first_paid_at is not None:
        raise ApiError(400, "promo_not_applicable", "That code is for first purchases only.")
    existing = db.query(PromoRedemption).filter(PromoRedemption.promo_id == promo.id,
                                                PromoRedemption.user_id == user.id).one_or_none()
    if existing is not None and existing.status == "applied":
        raise ApiError(409, "promo_used", "You've already used that code.")
    return promo


def _hold_promo(db: Session, promo: PromoCode, user: User, payment: Payment) -> None:
    existing = db.query(PromoRedemption).filter(PromoRedemption.promo_id == promo.id,
                                                PromoRedemption.user_id == user.id).one_or_none()
    if existing is not None:  # an abandoned earlier checkout: reuse its slot
        if existing.status == "released":
            _claim_slot(db, promo)
        existing.status = "pending"
        existing.payment_id = payment.id
        existing.created_at = utcnow()
    else:
        _claim_slot(db, promo)
        db.add(PromoRedemption(promo_id=promo.id, user_id=user.id, payment_id=payment.id, status="pending"))
    db.flush()


def release_stale_promo_holds(db: Session) -> int:
    cutoff = utcnow() - timedelta(hours=settings.promo_pending_hours)
    stale = db.query(PromoRedemption).filter(PromoRedemption.status == "pending",
                                             PromoRedemption.created_at < cutoff).all()
    for r in stale:
        r.status = "released"
        db.execute(update(PromoCode).where(PromoCode.id == r.promo_id, PromoCode.redemptions > 0)
                   .values(redemptions=PromoCode.redemptions - 1))
    db.flush()
    return len(stale)


def redeem_credit_promo(db: Session, user: User, code: str) -> dict:
    promo = _find_promo(db, code)
    if promo.kind != "credits" or promo.credits <= 0:
        return {"ok": True, "percent_off": promo.percent_off, "applies_to": promo.applies_to,
                "message": f"{promo.percent_off}% off applies at checkout."}
    if promo.first_purchase_only and user.first_paid_at is not None:
        raise ApiError(400, "promo_not_applicable", "That code is for new customers only.")
    if db.query(PromoRedemption).filter(PromoRedemption.promo_id == promo.id,
                                        PromoRedemption.user_id == user.id).first() is not None:
        raise ApiError(409, "promo_used", "You've already used that code.")
    _claim_slot(db, promo)
    db.add(PromoRedemption(promo_id=promo.id, user_id=user.id, status="applied"))
    db.flush()  # unique (promo, user): a racing second redemption fails here
    ledger.grant(db, user.id, promo.credits, kind="promo", idempotency_key=f"promo:{promo.id}:{user.id}",
                 note=f"Promo code {promo.code}")
    return {"ok": True, "credits_granted": promo.credits, "balance": ledger.balance(db, user.id)}


# ---- fulfilment -----------------------------------------------------------------------------------

def _amount_ok(db: Session, payment: Payment, amount_minor, currency, *, provider_ref: str) -> bool:
    try:
        amount = int(amount_minor)
    except (TypeError, ValueError):
        amount = -1
    cur = (currency or "").upper()
    if amount == payment.amount_minor and cur == payment.currency.upper():
        return True
    user = db.get(User, payment.user_id)
    meta = dict(payment.meta or {})
    meta["mismatch"] = {"expected": [payment.amount_minor, payment.currency], "got": [amount, cur], "ref": provider_ref}
    payment.meta = meta
    if user is not None:
        _flag(user, "payment_mismatch", {"payment": payment.id, "at": utcnow().isoformat()})
    compat.log_event(db, "payment_amount_mismatch", "high", user_id=payment.user_id,
                     detail={"payment": payment.id, "expected": payment.amount_minor, "got": amount, "currency": cur})
    log.error("payment %s amount mismatch: expected %s %s, got %s %s", payment.id, payment.amount_minor,
              payment.currency, amount, cur)
    return False


def _mark_paid(db: Session, payment: Payment, provider_payment_id: Optional[str]) -> bool:
    now = utcnow()
    res = db.execute(update(Payment).where(Payment.id == payment.id, Payment.status.in_(OPEN_STATUSES))
                     .values(status="paid", paid_at=now, provider_payment_id=provider_payment_id, updated_at=now))
    if res.rowcount != 1:
        return False
    payment.status = "paid"
    payment.paid_at = now
    payment.provider_payment_id = provider_payment_id
    return True


def _after_first_payment(db: Session, user: User, payment: Payment) -> None:
    if payment.promo_code_id:
        db.execute(update(PromoRedemption).where(PromoRedemption.promo_id == payment.promo_code_id,
                                                 PromoRedemption.user_id == user.id)
                   .values(status="applied", payment_id=payment.id))
    first = user.first_paid_at is None
    if first:
        user.first_paid_at = utcnow()
    if first and payment.amount_minor > 0:
        reward_referral(db, user, payment)


def fulfil_one_time(db: Session, payment: Payment, *, amount_minor, currency, provider_payment_id: Optional[str]) -> str:
    """Pack purchase confirmed by the provider. Returns processed | duplicate | rejected."""
    if payment.kind != "pack":
        return "rejected"
    if payment.status not in OPEN_STATUSES:
        return "duplicate"
    if not _amount_ok(db, payment, amount_minor, currency, provider_ref=provider_payment_id or ""):
        return "rejected"
    if not _mark_paid(db, payment, provider_payment_id):
        return "duplicate"
    user = db.get(User, payment.user_id)
    ledger.grant(db, payment.user_id, payment.credits, kind="purchase", idempotency_key=f"purchase:{payment.id}",
                 payment_id=payment.id, note=f"Bought {payment.credits} credits")
    if user is not None:
        _after_first_payment(db, user, payment)
    db.flush()
    return "processed"


def _grant_subscription_month(db: Session, sub: Subscription, at: datetime, *, payment_id: Optional[str]) -> bool:
    plan = catalog.get_plan(db, sub.plan_slug)
    credits = sub.monthly_credits or (plan.monthly_credits if plan else 0)
    rollover = plan.rollover_months if plan else 1
    stamp = int(at.timestamp())
    ledger.expire_subscription_excess(db, sub.user_id, credits * max(0, rollover),
                                      idempotency_key=f"subexpire:{sub.id}:{stamp}")
    entry = None
    if credits > 0:
        entry = ledger.grant(db, sub.user_id, credits, kind="subscription_grant",
                             idempotency_key=f"subgrant:{sub.id}:{stamp}", payment_id=payment_id, bucket="sub",
                             note=f"{plan.name if plan else sub.plan_slug} monthly credits")
    sub.grants += 1 if entry is not None else 0
    return entry is not None


def record_subscription_payment(db: Session, sub: Subscription, *, provider_ref: str, provider_payment_id: Optional[str],
                                amount_minor, currency, period_start: Optional[datetime],
                                period_end: Optional[datetime], first: bool) -> str:
    """A paid subscription period (first or renewal). Returns processed | duplicate | rejected."""
    user = db.get(User, sub.user_id)
    if user is None:
        return "rejected"
    initial = (db.query(Payment).filter(Payment.subscription_id == sub.id).order_by(Payment.created_at).first())
    if first and initial is not None and initial.status in OPEN_STATUSES:
        payment = initial
    else:
        existing = db.query(Payment).filter(Payment.provider == sub.provider,
                                            Payment.provider_order_id == provider_ref).one_or_none()
        if existing is not None:
            return "duplicate"
        if initial is not None and initial.status in OPEN_STATUSES:
            payment = initial  # renewal event seen before the first one; treat as the first
        else:
            payment = Payment(user_id=sub.user_id, provider=sub.provider, kind="plan", item_slug=sub.plan_slug,
                              interval=sub.interval, currency=sub.currency, amount_minor=sub.amount_minor,
                              list_amount_minor=sub.amount_minor, status="pending", provider_order_id=provider_ref,
                              subscription_id=sub.id, tier=initial.tier if initial else "default")
            db.add(payment)
            db.flush()
    if not _amount_ok(db, payment, amount_minor, currency, provider_ref=provider_ref):
        return "rejected"
    if not _mark_paid(db, payment, provider_payment_id or provider_ref):
        return "duplicate"

    now = utcnow()
    start = period_start or now
    end = period_end or add_months(start, 12 if sub.interval == "year" else 1)
    sub.status = "active"
    sub.current_period_start = start
    sub.current_period_end = end
    user.plan_slug = sub.plan_slug
    if sub.interval == "year":
        # Annual plans pay once a year but get credits monthly (so rollover caps mean the same thing).
        _grant_subscription_month(db, sub, start, payment_id=payment.id)
        sub.next_grant_at = add_months(start, 1)
    else:
        _grant_subscription_month(db, sub, start, payment_id=payment.id)
        sub.next_grant_at = None
    plan = catalog.get_plan(db, sub.plan_slug)
    payment.credits = sub.monthly_credits or (plan.monthly_credits if plan else 0)
    _after_first_payment(db, user, payment)
    db.flush()
    return "processed"


def grant_due_subscription_credits(db: Session, now: Optional[datetime] = None) -> int:
    """Monthly grants inside an annual period. Safe to run often (idempotent per month)."""
    now = now or utcnow()
    due = (db.query(Subscription)
           .filter(Subscription.status == "active", Subscription.next_grant_at.is_not(None),
                   Subscription.next_grant_at <= now).limit(500).all())
    granted = 0
    for sub in due:
        while sub.next_grant_at is not None and sub.next_grant_at <= now:
            if sub.current_period_end is not None and sub.next_grant_at >= sub.current_period_end:
                sub.next_grant_at = None
                break
            if _grant_subscription_month(db, sub, sub.next_grant_at, payment_id=None):
                granted += 1
            sub.next_grant_at = add_months(sub.next_grant_at, 1)
    db.flush()
    return granted


def end_subscription(db: Session, sub: Subscription, *, status: str = "canceled") -> None:
    sub.status = status
    sub.canceled_at = sub.canceled_at or utcnow()
    sub.next_grant_at = None
    user = db.get(User, sub.user_id)
    other = (db.query(Subscription.id)
             .filter(Subscription.user_id == sub.user_id, Subscription.id != sub.id,
                     Subscription.status.in_(("active", "past_due"))).first())
    if user is not None and other is None and user.plan_slug == sub.plan_slug:
        user.plan_slug = "free"
    db.flush()


def cancel_subscription(db: Session, user: User) -> Subscription:
    sub = (db.query(Subscription).filter(Subscription.user_id == user.id, Subscription.status.in_(("active", "past_due")))
           .order_by(Subscription.created_at.desc()).first())
    if sub is None:
        raise ApiError(404, "not_found", "You don't have an active subscription.")
    if sub.provider_subscription_id and sub.provider != "dev":
        providers.get(sub.provider).cancel_subscription(sub.provider_subscription_id)
    sub.cancel_at_period_end = True
    if sub.provider == "dev":
        sub.canceled_at = utcnow()
    db.flush()
    return sub


# ---- refunds & chargebacks ------------------------------------------------------------------------

def reverse_payment(db: Session, payment: Payment, *, reason: str, refunded_minor: Optional[int] = None,
                    ref: str = "") -> dict:
    """Take back the credits a refunded or disputed payment granted.

    Rule (CONTRACT.md): reverse up to the current balance, never below zero; record any shortfall;
    flag the user; chargebacks (and refunds with a shortfall) set billing_hold, which blocks new jobs.
    """
    user = db.get(User, payment.user_id)
    granted = int(payment.credits or 0)
    if reason == "chargeback" or refunded_minor is None or payment.amount_minor <= 0:
        target = granted
    else:
        frac = min(1.0, max(0.0, int(refunded_minor) / payment.amount_minor))
        target = int(round(granted * frac))
    delta = target - int(payment.credits_reversed or 0)
    shortfall = 0
    if delta > 0:
        entry = ledger.debit(db, payment.user_id, delta, kind="refund", idempotency_key=f"reverse:{payment.id}:{target}",
                             payment_id=payment.id, clamp=True,
                             note="Chargeback: credits reversed" if reason == "chargeback" else "Refund: credits reversed")
        if entry is not None:
            shortfall = int((entry.meta or {}).get("shortfall", 0))
            payment.credits_reversed = target
    if refunded_minor is not None:
        payment.refunded_minor = max(int(payment.refunded_minor or 0), int(refunded_minor))
    if reason == "chargeback":
        payment.status = "disputed"
    else:
        payment.status = "refunded" if payment.refunded_minor >= payment.amount_minor else "partially_refunded"
    if user is not None:
        _flag(user, reason, {"payment": payment.id, "at": utcnow().isoformat(), "shortfall": shortfall, "ref": ref})
        if reason == "chargeback" or shortfall > 0:
            user.billing_hold = True
    compat.log_event(db, f"payment_{reason}", "high" if reason == "chargeback" else "medium",
                     user_id=payment.user_id, detail={"payment": payment.id, "reversed": delta, "shortfall": shortfall})
    db.flush()
    return {"reversed": max(0, delta), "shortfall": shortfall}


# ---- referrals ------------------------------------------------------------------------------------

def reward_referral(db: Session, referred: User, payment: Payment) -> bool:
    if not referred.referred_by or settings.referral_reward_credits <= 0:
        return False
    ref = db.query(Referral).filter(Referral.referred_id == referred.id).one_or_none()
    if ref is None or ref.status != "pending":
        return False
    referrer = db.get(User, ref.referrer_id)
    if referrer is None or referrer.status != "active":
        ref.status = "void"
        ref.reason = "referrer_unavailable"
        return False
    rewarded = db.query(Referral).filter(Referral.referrer_id == referrer.id, Referral.status == "rewarded").count()
    if rewarded >= settings.referral_max_rewards:
        ref.status = "void"
        ref.reason = "referrer_cap"
        return False
    res = db.execute(update(Referral).where(Referral.id == ref.id, Referral.status == "pending")
                     .values(status="rewarded", rewarded_at=utcnow(), payment_id=payment.id,
                             reward_credits=settings.referral_reward_credits))
    if res.rowcount != 1:
        return False
    ledger.grant(db, referrer.id, settings.referral_reward_credits, kind="referral",
                 idempotency_key=f"referral:{ref.id}", note="Referral reward")
    return True


def referral_summary(db: Session, user: User) -> dict:
    rows = db.query(Referral).filter(Referral.referrer_id == user.id).all()
    return {"code": user.referral_code, "url": f"{settings.public_url.rstrip('/')}/?ref={user.referral_code}",
            "reward_credits": settings.referral_reward_credits,
            "rewarded": sum(1 for r in rows if r.status == "rewarded"),
            "pending": sum(1 for r in rows if r.status == "pending"),
            "credits_earned": sum(r.reward_credits for r in rows if r.status == "rewarded")}


# ---- views ----------------------------------------------------------------------------------------

def public_subscription(sub: Optional[Subscription]) -> Optional[dict]:
    if sub is None:
        return None
    return {"id": sub.id, "plan": sub.plan_slug, "interval": sub.interval, "status": sub.status,
            "provider": sub.provider, "currency": sub.currency,
            "amount": catalog.format_amount(sub.amount_minor, sub.currency),
            "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "cancel_at_period_end": sub.cancel_at_period_end, "monthly_credits": sub.monthly_credits}


def public_payment(p: Payment) -> dict:
    return {"id": p.id, "kind": p.kind, "item": p.item_slug, "interval": p.interval, "status": p.status,
            "amount": catalog.format_amount(p.amount_minor, p.currency), "amount_minor": p.amount_minor,
            "currency": p.currency, "credits": p.credits, "provider": p.provider,
            "created_at": p.created_at.isoformat(), "paid_at": p.paid_at.isoformat() if p.paid_at else None}
