"""Buy Me a Coffee webhooks: the primary payment path (memberships = plans, shop extras = credit packs).

Per BMC's webhook docs (help article "How to setup and use Buy Me a Coffee webhooks" and its OpenAPI
spec, bmc-webhooks-openapi.json v1.0.0):
* `x-signature-sha256` is the hex HMAC-SHA256 of the raw body keyed with the webhook's signing secret.
* Envelope: {event_id, type, live_mode, created, attempt, data}. `event_id` identifies a *delivery
  attempt*, not the business event, so idempotency keys on stable ids instead: the payment id
  (`data.id` on donation/extra events) and the membership id plus its `current_period_start`.
* Amounts are JSON numbers in major units (15.0 = $15; inferred from the documented example, where
  3 coffees x 5.0 = 15.0). Membership timestamps are Unix seconds (milliseconds are detected).
* Dashboard test events have live_mode=false and event_id=1; they are ignored unless
  SAAS_BMC_ACCEPT_TEST_EVENTS is on (never in prod).
* There is no replay header; the signed `created` must be within SAAS_BMC_MAX_EVENT_AGE_HOURS.
* BMC documents no renewal event. A membership period is credited the first time any membership.*
  event shows an active membership with a new `current_period_start`; that is our renewal signal.

Matching: the supporter's email must belong to a *verified* account (a squatter can't register an
unverified address and catch someone else's purchase). Otherwise the payment is stored as a
PendingClaim and credited when that email is verified (see `claim_pending`).
Mapping is catalog data: Price.provider_refs `bmc_level_id` / `bmc_level_name` (memberships -> plans)
and `bmc_extra_id` (extras -> packs), with an exact amount+currency match as the fallback.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..auth.errors import ApiRoute
from ..common import utcnow
from ..config import settings
from ..models.billing import Pack, Payment, PendingClaim, Plan, Price, Subscription
from ..models.core import User
from . import catalog, ledger, providers, service
from .webhooks import _reject, process_event

log = logging.getLogger("saas.billing.bmc")
router = APIRouter(route_class=ApiRoute)

ONE_OFF = ("donation.created", "extra_purchase.created")
REFUNDS = ("donation.refunded", "extra_purchase.refunded")
MEMBERSHIP = ("membership.started", "membership.updated", "membership.cancelled", "membership.paused")


# ---- parsing helpers ------------------------------------------------------------------------------

def to_minor(amount: Any, currency: str) -> Optional[int]:
    try:
        value = Decimal(str(amount))
    except Exception:
        return None
    factor = 1 if currency.upper() in catalog._ZERO_DECIMAL else 100
    return int((value * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_dt(ts: Any):
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return None
    if n > 10**12:  # milliseconds
        n //= 1000
    return service.from_unix(n)


def truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def stable_key(etype: str, data: dict) -> str:
    """What makes this business event unique (BMC's event_id is per delivery attempt)."""
    parts = [etype, str(data.get("id", "")), str(data.get("current_period_start", "")), str(data.get("status", "")),
             str(data.get("refunded", "")), str(data.get("cancel_at_period_end", ""))]
    raw = "|".join(parts)
    return raw if len(raw) <= 150 else "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def find_verified_user(db: Session, email: Any) -> Optional[User]:
    from ..auth import service as auth_service
    from ..auth.errors import ApiError

    try:
        norm = auth_service.normalise_email(email)
    except ApiError:
        return None
    user = db.query(User).filter(User.email == norm).one_or_none()
    if user is None:
        canon = auth_service.canonical_email(norm)
        matches = db.query(User).filter(User.email_canonical == canon, User.status == "active").all()
        user = matches[0] if len(matches) == 1 else None
    if user is None or user.status != "active" or user.email_verified_at is None:
        return None
    return user


# ---- catalog mapping ------------------------------------------------------------------------------

def _prices(db: Session, kind: str) -> list[Price]:
    """Default-tier prices only: BMC charges one fixed price, so a cheaper regional (PPP) price must never
    be what a BMC payment is matched against."""
    catalog.ensure_seeded(db)
    return db.query(Price).filter(Price.item_kind == kind, Price.active.is_(True), Price.tier == "default").all()


def map_membership(db: Session, data: dict) -> Optional[tuple[Plan, Price]]:
    level_id, level_name = data.get("membership_level_id"), str(data.get("membership_level_name") or "").strip().lower()
    interval = "year" if str(data.get("duration_type", "month")).lower().startswith("year") else "month"
    currency = str(data.get("currency") or "").upper()
    paid = to_minor(data.get("amount"), currency or "USD")
    candidates = [p for p in _prices(db, "plan") if p.interval == interval]
    match = None
    for p in candidates:
        refs = p.provider_refs or {}
        if level_id is not None and str(refs.get("bmc_level_id", "")) == str(level_id):
            match = p
            break
        if level_name and str(refs.get("bmc_level_name", "")).strip().lower() == level_name:
            match = p
            break
    if match is None:
        exact = [p for p in candidates if p.currency == currency and p.amount_minor == paid]
        match = exact[0] if len(exact) == 1 else None
    if match is None:
        return None
    plan = catalog.get_plan(db, match.item_slug)
    return (plan, match) if plan is not None and plan.active else None


def map_one_off(db: Session, data: dict) -> Optional[tuple[Pack, Price, int]]:
    """(pack, price, quantity) for an extras purchase or a plain support payment."""
    currency = str(data.get("currency") or "").upper()
    prices = _prices(db, "pack")
    for extra in data.get("extras") or []:
        if not isinstance(extra, dict):
            continue
        for p in prices:
            if str((p.provider_refs or {}).get("bmc_extra_id", "")) == str(extra.get("id")):
                pack = catalog.get_pack(db, p.item_slug)
                qty = max(1, int(extra.get("quantity") or 1))
                if pack is not None and pack.active:
                    return pack, p, qty
    paid = to_minor(data.get("amount"), currency or "USD")
    exact = [p for p in prices if p.currency == currency and p.amount_minor == paid]
    if len(exact) == 1:
        pack = catalog.get_pack(db, exact[0].item_slug)
        if pack is not None and pack.active:
            return pack, exact[0], 1
    return None


# ---- fulfilment -----------------------------------------------------------------------------------

def _defer(db: Session, etype: str, external_id: str, data: dict, reason: str) -> tuple[str, str]:
    email = str(data.get("supporter_email") or "").strip().lower()[:320]
    existing = db.query(PendingClaim).filter(PendingClaim.provider == "bmc",
                                             PendingClaim.external_id == external_id).one_or_none()
    if existing is None:
        db.add(PendingClaim(provider="bmc", external_id=external_id[:160], email=email, event_type=etype,
                            payload=data, status="pending" if reason == "no_verified_account" else "unmatched",
                            reason=reason))
    return "deferred", reason


def one_off(db: Session, etype: str, data: dict, user: Optional[User] = None) -> tuple[str, str]:
    external = f"pay:{data.get('id')}"
    if data.get("id") is None or str(data.get("status", "succeeded")) != "succeeded" or truthy(data.get("refunded")):
        return "ignored", "not a succeeded payment"
    if db.query(Payment.id).filter(Payment.provider == "bmc", Payment.provider_order_id == external).first():
        return "duplicate", external
    mapped = map_one_off(db, data)
    if mapped is None:
        return _defer(db, etype, external, data, "no_matching_pack")
    user = user or find_verified_user(db, data.get("supporter_email"))
    if user is None:
        return _defer(db, etype, external, data, "no_verified_account")
    pack, price, qty = mapped
    currency = str(data.get("currency") or "").upper()
    paid = to_minor(data.get("amount"), currency) or 0
    expected = price.amount_minor * qty
    pending = (db.query(Payment).filter(Payment.user_id == user.id, Payment.provider == "bmc", Payment.kind == "pack",
                                        Payment.item_slug == pack.slug, Payment.status.in_(service.OPEN_STATUSES))
               .order_by(Payment.created_at).first())
    payment = pending or Payment(user_id=user.id, provider="bmc", kind="pack", item_slug=pack.slug, interval="once",
                                 currency=price.currency, tier=price.tier, status="pending")
    payment.provider_order_id = external
    payment.currency = price.currency
    payment.amount_minor = expected
    payment.list_amount_minor = expected
    payment.credits = pack.credits * qty
    if pending is None:
        db.add(payment)
    db.flush()
    if paid < expected or currency != price.currency:
        service._amount_ok(db, payment, paid, currency, provider_ref=external)  # records the mismatch + flag
        payment.status = "failed"
        return "rejected", payment.id
    if not service._mark_paid(db, payment, str(data.get("transaction_id") or external)):
        return "duplicate", payment.id
    ledger.grant(db, user.id, payment.credits, kind="purchase", idempotency_key=f"purchase:{payment.id}",
                 payment_id=payment.id, note=f"Bought {payment.credits} credits on Buy Me a Coffee")
    service._after_first_payment(db, user, payment)
    return "processed", payment.id


def membership(db: Session, etype: str, data: dict, user: Optional[User] = None) -> tuple[str, str]:
    if data.get("id") is None:
        return "ignored", "no membership id"
    ext_sub = str(data["id"])
    sub = db.query(Subscription).filter(Subscription.provider == "bmc",
                                        Subscription.provider_subscription_id == ext_sub).one_or_none()
    status = str(data.get("status") or "").lower()
    start, end = to_dt(data.get("current_period_start")), to_dt(data.get("current_period_end"))

    if etype == "membership.cancelled" or status == "canceled":
        if sub is None:
            return "ignored", "unknown membership"
        sub.cancel_at_period_end = True
        sub.canceled_at = sub.canceled_at or utcnow()
        if end is None or end <= utcnow() or not truthy(data.get("cancel_at_period_end", "true")):
            service.end_subscription(db, sub)
        return "processed", sub.id
    if etype == "membership.paused" or status == "paused":
        if sub is None:
            return "ignored", "unknown membership"
        sub.status = "past_due"
        return "processed", sub.id
    if status not in ("", "active"):
        return "ignored", f"status {status}"

    external = f"mem:{ext_sub}:{data.get('current_period_start', '')}"
    mapped = map_membership(db, data)
    if mapped is None:
        return _defer(db, etype, external, data, "no_matching_plan")
    plan, price = mapped
    if sub is None:
        user = user or find_verified_user(db, data.get("supporter_email"))
        if user is None:
            return _defer(db, etype, external, data, "no_verified_account")
        sub = (db.query(Subscription).filter(Subscription.user_id == user.id, Subscription.provider == "bmc",
                                             Subscription.status == "incomplete", Subscription.plan_slug == plan.slug,
                                             Subscription.provider_subscription_id.is_(None))
               .order_by(Subscription.created_at.desc()).first())
        if sub is None:
            sub = Subscription(user_id=user.id, plan_slug=plan.slug, interval=price.interval, status="incomplete",
                               provider="bmc", currency=price.currency, amount_minor=price.amount_minor,
                               monthly_credits=plan.monthly_credits)
            db.add(sub)
        sub.provider_subscription_id = ext_sub
        db.flush()
    sub.amount_minor = price.amount_minor
    sub.currency = price.currency
    sub.cancel_at_period_end = truthy(data.get("cancel_at_period_end"))
    currency = str(data.get("currency") or "").upper()
    result = service.record_subscription_payment(
        db, sub, provider_ref=external, provider_payment_id=external,
        amount_minor=to_minor(data.get("amount"), currency or price.currency), currency=currency,
        period_start=start, period_end=end, first=sub.status == "incomplete")
    return result, sub.id


def refund(db: Session, etype: str, data: dict) -> tuple[str, str]:
    payment = db.query(Payment).filter(Payment.provider == "bmc",
                                       Payment.provider_order_id == f"pay:{data.get('id')}").one_or_none()
    if payment is None:
        claim = db.query(PendingClaim).filter(PendingClaim.provider == "bmc",
                                              PendingClaim.external_id == f"pay:{data.get('id')}").one_or_none()
        if claim is not None and claim.status in ("pending", "unmatched"):
            claim.status, claim.reason = "void", "refunded before it was claimed"
            return "processed", claim.id
        return "ignored", "no matching payment"
    out = service.reverse_payment(db, payment, reason="refund", refunded_minor=payment.amount_minor,
                                  ref=f"bmc:{data.get('id')}")
    return "processed", f"{payment.id} reversed {out['reversed']}"


def handle(db: Session, etype: str, data: dict, user: Optional[User] = None) -> tuple[str, str]:
    if etype in ONE_OFF:
        return one_off(db, etype, data, user)
    if etype in MEMBERSHIP:
        return membership(db, etype, data, user)
    if etype in REFUNDS:
        return refund(db, etype, data)
    return "ignored", f"unhandled {etype}"


def claim_pending(db: Session, user: User) -> int:
    """Credit purchases made on BMC with this (now verified) email before the account could match."""
    if user.email_verified_at is None:
        return 0
    claimed = 0
    rows = (db.query(PendingClaim).filter(PendingClaim.provider == "bmc", PendingClaim.email == user.email,
                                          PendingClaim.status == "pending").order_by(PendingClaim.created_at).all())
    for claim in rows:
        result, _detail = handle(db, claim.event_type, dict(claim.payload or {}), user=user)
        if result in ("processed", "duplicate"):
            claim.status, claim.claimed_by, claim.claimed_at = "claimed", user.id, utcnow()
            claimed += 1
    db.flush()
    return claimed


# ---- route ----------------------------------------------------------------------------------------

def _handler(etype: str, data: dict):
    return lambda db, _payload: handle(db, etype, data)


@router.post("/webhooks/bmc")
async def bmc_webhook(request: Request):
    raw = await request.body()
    if not settings.bmc_webhook_secret:
        return JSONResponse({"error": "not_configured", "message": "Buy Me a Coffee webhooks are not configured."},
                            status_code=503)
    if not providers.verify_bmc_signature(raw, request.headers.get("x-signature-sha256"), settings.bmc_webhook_secret):
        return await run_in_threadpool(_reject, "bmc", request, "signature")
    try:
        event = json.loads(raw)
        etype = str(event["type"])
        data = event.get("data") or {}
        if not isinstance(data, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "invalid_payload", "message": "Unreadable event."}, status_code=400)
    if event.get("live_mode") is False and (settings.is_prod or not settings.bmc_accept_test_events):
        return JSONResponse({"ok": True, "status": "ignored", "detail": "test event"})
    try:
        age = time.time() - int(event.get("created") or 0)
    except (TypeError, ValueError):
        age = 10**9
    if age > settings.bmc_max_event_age_hours * 3600:
        return JSONResponse({"ok": True, "status": "ignored", "detail": "stale event"})
    return await run_in_threadpool(process_event, "bmc", stable_key(etype, data), etype, _handler(etype, data), event)

