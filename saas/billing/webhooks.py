"""Stripe and Razorpay webhooks: signature-verified over the raw body, idempotent on the event id.

Flow per delivery: verify the signature (Stripe also checks its timestamp, a 5-minute replay window)
-> refuse if this (provider, event id) was already processed -> run the handler, record the event
and commit in ONE transaction. A handler that needs a row which has not arrived yet raises RetryLater
and we answer 503 so the provider redelivers. Payloads are parsed defensively: Stripe moved invoice
fields under `parent.subscription_details` in recent API versions, so both shapes are read.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import compat
from ..auth.errors import ApiRoute
from ..common import client_ip
from ..config import settings
from ..db import SessionLocal
from ..models.billing import Payment, Subscription, WebhookEvent
from . import providers, service
from .service import RetryLater, from_unix

log = logging.getLogger("saas.billing.webhooks")
router = APIRouter(route_class=ApiRoute)

Handler = Callable[[Session, dict], tuple[str, str]]


def _get(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def process_event(provider: str, event_id: str, event_type: str, handler: Optional[Handler], payload: dict) -> JSONResponse:
    db = SessionLocal()
    try:
        if db.query(WebhookEvent.id).filter(WebhookEvent.provider == provider, WebhookEvent.event_id == event_id).first():
            return JSONResponse({"ok": True, "duplicate": True})
        if handler is None:
            status, detail = "ignored", "unhandled event type"
        else:
            status, detail = handler(db, payload)
        db.add(WebhookEvent(provider=provider, event_id=event_id[:160], event_type=event_type[:80], status=status,
                            detail=detail[:1000] if detail else None))
        db.commit()
        return JSONResponse({"ok": True, "status": status})
    except IntegrityError:
        db.rollback()  # a concurrent delivery of the same event won
        return JSONResponse({"ok": True, "duplicate": True})
    except RetryLater as exc:
        db.rollback()
        log.info("%s %s deferred: %s", provider, event_type, exc)
        return JSONResponse({"error": "retry_later", "message": str(exc)}, status_code=503)
    except Exception:
        db.rollback()
        log.exception("%s webhook %s (%s) failed", provider, event_id, event_type)
        return JSONResponse({"error": "internal_error", "message": "Processing failed."}, status_code=500)
    finally:
        db.close()


def _reject(provider: str, request: Request, reason: str) -> JSONResponse:
    db = SessionLocal()
    try:
        compat.log_event(db, "webhook_rejected", "medium", ip=client_ip(request), detail={"provider": provider,
                                                                                          "reason": reason})
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    return JSONResponse({"error": "invalid_signature", "message": "Signature verification failed."}, status_code=400)


# ---- Stripe ---------------------------------------------------------------------------------------

def _stripe_payment_for_session(db: Session, obj: dict) -> Optional[Payment]:
    pid = obj.get("client_reference_id") or _get(obj, "metadata", "payment_id")
    if not pid:
        return None
    payment = db.get(Payment, str(pid))
    if payment is None or payment.provider != "stripe" or payment.provider_order_id != obj.get("id"):
        return None
    return payment


def _stripe_checkout_completed(db: Session, obj: dict) -> tuple[str, str]:
    payment = _stripe_payment_for_session(db, obj)
    if payment is None:
        return "ignored", "no matching payment"
    if obj.get("mode") == "subscription":
        sub = db.get(Subscription, payment.subscription_id) if payment.subscription_id else None
        if sub is None:
            return "ignored", "no subscription row"
        if obj.get("subscription"):
            sub.provider_subscription_id = str(obj["subscription"])
        if obj.get("customer"):
            sub.provider_customer_id = str(obj["customer"])
        return "processed", "subscription linked"
    if obj.get("payment_status") != "paid":
        return "ignored", "not paid yet"
    result = service.fulfil_one_time(db, payment, amount_minor=obj.get("amount_total"), currency=obj.get("currency"),
                                     provider_payment_id=obj.get("payment_intent"))
    return result, payment.id


def _stripe_invoice_subscription(obj: dict) -> tuple[Optional[str], dict]:
    sub_id = obj.get("subscription") or _get(obj, "parent", "subscription_details", "subscription")
    meta = (_get(obj, "parent", "subscription_details", "metadata") or _get(obj, "subscription_details", "metadata")
            or {})
    if isinstance(sub_id, dict):
        sub_id = sub_id.get("id")
    return (str(sub_id) if sub_id else None), (meta if isinstance(meta, dict) else {})


def _stripe_invoice_period(obj: dict):
    for line in _get(obj, "lines", "data") or []:
        period = line.get("period") if isinstance(line, dict) else None
        if isinstance(period, dict) and period.get("start") and period.get("end"):
            return from_unix(period["start"]), from_unix(period["end"])
    return from_unix(obj.get("period_start")), from_unix(obj.get("period_end"))


def _stripe_invoice_paid(db: Session, obj: dict) -> tuple[str, str]:
    reason = obj.get("billing_reason")
    if reason not in ("subscription_create", "subscription_cycle"):
        return "ignored", f"billing_reason {reason}"
    ext_id, meta = _stripe_invoice_subscription(obj)
    sub = None
    if ext_id:
        sub = db.query(Subscription).filter(Subscription.provider == "stripe",
                                            Subscription.provider_subscription_id == ext_id).one_or_none()
    if sub is None and meta.get("sub_id"):
        cand = db.get(Subscription, str(meta["sub_id"]))
        if cand is not None and cand.provider == "stripe" and (cand.provider_subscription_id in (None, ext_id)):
            initial = db.query(Payment).filter(Payment.subscription_id == cand.id).order_by(Payment.created_at).first()
            if initial is not None and str(meta.get("payment_id") or "") == initial.id:
                sub = cand
                if ext_id:
                    sub.provider_subscription_id = ext_id
    if sub is None:
        raise RetryLater("subscription not linked yet")
    start, end = _stripe_invoice_period(obj)
    result = service.record_subscription_payment(
        db, sub, provider_ref=str(obj.get("id")), provider_payment_id=str(obj.get("id")),
        amount_minor=obj.get("amount_paid"), currency=obj.get("currency"), period_start=start, period_end=end,
        first=(reason == "subscription_create"))
    return result, sub.id


_STRIPE_SUB_STATUS = {"active": "active", "trialing": "active", "past_due": "past_due", "unpaid": "past_due",
                      "canceled": "canceled", "incomplete_expired": "canceled", "incomplete": "incomplete"}


def _stripe_subscription_updated(db: Session, obj: dict) -> tuple[str, str]:
    sub = db.query(Subscription).filter(Subscription.provider == "stripe",
                                        Subscription.provider_subscription_id == obj.get("id")).one_or_none()
    if sub is None:
        return "ignored", "unknown subscription"
    sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    status = _STRIPE_SUB_STATUS.get(str(obj.get("status")), sub.status)
    if status == "canceled":
        service.end_subscription(db, sub)
    elif sub.status != "incomplete":  # only a paid invoice activates an incomplete subscription
        sub.status = status
    return "processed", sub.id


def _stripe_subscription_deleted(db: Session, obj: dict) -> tuple[str, str]:
    sub = db.query(Subscription).filter(Subscription.provider == "stripe",
                                        Subscription.provider_subscription_id == obj.get("id")).one_or_none()
    if sub is None:
        return "ignored", "unknown subscription"
    service.end_subscription(db, sub)
    return "processed", sub.id


def _stripe_find_payment_for_charge(db: Session, charge: dict) -> Optional[Payment]:
    pi = charge.get("payment_intent")
    if isinstance(pi, dict):
        pi = pi.get("id")
    if pi:
        p = db.query(Payment).filter(Payment.provider == "stripe", Payment.provider_payment_id == str(pi)).first()
        if p is not None:
            return p
    pid = _get(charge, "metadata", "payment_id")
    if pid:
        p = db.get(Payment, str(pid))
        if p is not None and p.provider == "stripe":
            return p
    inv = charge.get("invoice")
    if isinstance(inv, dict):
        inv = inv.get("id")
    if inv:
        return db.query(Payment).filter(Payment.provider == "stripe", Payment.provider_order_id == str(inv)).first()
    return None


def _stripe_charge_refunded(db: Session, obj: dict) -> tuple[str, str]:
    payment = _stripe_find_payment_for_charge(db, obj)
    if payment is None:
        compat.log_event(db, "refund_unmatched", "high", detail={"provider": "stripe", "charge": obj.get("id")})
        return "ignored", "no matching payment"
    out = service.reverse_payment(db, payment, reason="refund", refunded_minor=int(obj.get("amount_refunded") or 0),
                                  ref=str(obj.get("id")))
    return "processed", f"{payment.id} reversed {out['reversed']}"


def _stripe_dispute_created(db: Session, obj: dict) -> tuple[str, str]:
    charge: dict = {"payment_intent": obj.get("payment_intent"), "id": obj.get("charge")}
    payment = _stripe_find_payment_for_charge(db, charge) if charge.get("payment_intent") else None
    if payment is None and obj.get("charge"):
        fetched = providers.StripeProvider().fetch_charge(str(obj["charge"]))
        if fetched:
            payment = _stripe_find_payment_for_charge(db, fetched)
    if payment is None:
        compat.log_event(db, "chargeback_unmatched", "critical", detail={"provider": "stripe", "dispute": obj.get("id")})
        return "ignored", "no matching payment"
    out = service.reverse_payment(db, payment, reason="chargeback", ref=str(obj.get("id")))
    return "processed", f"{payment.id} reversed {out['reversed']}"


_STRIPE: dict[str, Handler] = {
    "checkout.session.completed": _stripe_checkout_completed,
    "checkout.session.async_payment_succeeded": _stripe_checkout_completed,
    "invoice.paid": _stripe_invoice_paid,
    "customer.subscription.updated": _stripe_subscription_updated,
    "customer.subscription.deleted": _stripe_subscription_deleted,
    "charge.refunded": _stripe_charge_refunded,
    "charge.dispute.created": _stripe_dispute_created,
}


def _stripe_handler(event: dict) -> Optional[Handler]:
    fn = _STRIPE.get(str(event.get("type")))
    if fn is None:
        return None
    return lambda db, payload: fn(db, _get(payload, "data", "object") or {})


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    raw = await request.body()
    if not settings.stripe_webhook_secret:
        return JSONResponse({"error": "not_configured", "message": "Stripe webhooks are not configured."}, status_code=503)
    if not providers.verify_stripe_signature(raw, request.headers.get("stripe-signature"),
                                             settings.stripe_webhook_secret, tolerance=settings.webhook_tolerance_s):
        return await run_in_threadpool(_reject, "stripe", request, "signature")
    try:
        event = json.loads(raw)
        event_id = str(event["id"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "invalid_payload", "message": "Unreadable event."}, status_code=400)
    return await run_in_threadpool(process_event, "stripe", event_id, str(event.get("type", "")),
                                   _stripe_handler(event), event)


# ---- Razorpay -------------------------------------------------------------------------------------

def _rp_entity(payload: dict, name: str) -> dict:
    return _get(payload, "payload", name, "entity") or {}


def _rp_payment_link_paid(db: Session, payload: dict) -> tuple[str, str]:
    link = _rp_entity(payload, "payment_link")
    pay = _rp_entity(payload, "payment")
    pid = link.get("reference_id") or _get(link, "notes", "payment_id")
    payment = db.get(Payment, str(pid)) if pid else None
    if payment is None or payment.provider != "razorpay" or payment.provider_order_id != link.get("id"):
        return "ignored", "no matching payment"
    if link.get("status") not in (None, "paid") or pay.get("status") not in (None, "captured"):
        return "ignored", "not captured"
    result = service.fulfil_one_time(db, payment, amount_minor=pay.get("amount", link.get("amount_paid")),
                                     currency=pay.get("currency") or link.get("currency"),
                                     provider_payment_id=pay.get("id"))
    return result, payment.id


def _rp_subscription(db: Session, entity: dict) -> Optional[Subscription]:
    sub = db.query(Subscription).filter(Subscription.provider == "razorpay",
                                        Subscription.provider_subscription_id == entity.get("id")).one_or_none()
    if sub is None and _get(entity, "notes", "sub_id"):
        cand = db.get(Subscription, str(entity["notes"]["sub_id"]))
        if cand is not None and cand.provider == "razorpay" and cand.provider_subscription_id in (None, entity.get("id")):
            sub = cand
            sub.provider_subscription_id = entity.get("id")
    return sub


def _rp_subscription_charged(db: Session, payload: dict) -> tuple[str, str]:
    ent = _rp_entity(payload, "subscription")
    pay = _rp_entity(payload, "payment")
    sub = _rp_subscription(db, ent)
    if sub is None:
        raise RetryLater("subscription not known yet")
    if not pay.get("id"):
        return "ignored", "no payment in event"
    first = sub.status == "incomplete"
    result = service.record_subscription_payment(
        db, sub, provider_ref=str(pay["id"]), provider_payment_id=str(pay["id"]), amount_minor=pay.get("amount"),
        currency=pay.get("currency"), period_start=from_unix(ent.get("current_start")),
        period_end=from_unix(ent.get("current_end")), first=first)
    return result, sub.id


def _rp_subscription_ended(db: Session, payload: dict) -> tuple[str, str]:
    sub = _rp_subscription(db, _rp_entity(payload, "subscription"))
    if sub is None:
        return "ignored", "unknown subscription"
    if payload.get("event") == "subscription.halted":
        sub.status = "past_due"
    else:
        service.end_subscription(db, sub)
    return "processed", sub.id


def _rp_payment_for(db: Session, razorpay_payment_id: Optional[str]) -> Optional[Payment]:
    if not razorpay_payment_id:
        return None
    return db.query(Payment).filter(Payment.provider == "razorpay",
                                    Payment.provider_payment_id == str(razorpay_payment_id)).first()


def _rp_refund(db: Session, payload: dict) -> tuple[str, str]:
    refund = _rp_entity(payload, "refund")
    pay = _rp_entity(payload, "payment")
    payment = _rp_payment_for(db, refund.get("payment_id") or pay.get("id"))
    if payment is None:
        compat.log_event(db, "refund_unmatched", "high", detail={"provider": "razorpay", "refund": refund.get("id")})
        return "ignored", "no matching payment"
    if pay.get("amount_refunded") is not None:
        total = int(pay["amount_refunded"])
    else:
        seen = list((payment.meta or {}).get("refund_ids", []))
        if refund.get("id") in seen:
            return "duplicate", "refund already counted"
        seen.append(refund.get("id"))
        payment.meta = {**(payment.meta or {}), "refund_ids": seen}
        total = int(payment.refunded_minor or 0) + int(refund.get("amount") or 0)
    out = service.reverse_payment(db, payment, reason="refund", refunded_minor=total, ref=str(refund.get("id")))
    return "processed", f"{payment.id} reversed {out['reversed']}"


def _rp_dispute(db: Session, payload: dict) -> tuple[str, str]:
    dispute = _rp_entity(payload, "dispute")
    payment = _rp_payment_for(db, dispute.get("payment_id") or _rp_entity(payload, "payment").get("id"))
    if payment is None:
        compat.log_event(db, "chargeback_unmatched", "critical", detail={"provider": "razorpay", "dispute": dispute.get("id")})
        return "ignored", "no matching payment"
    out = service.reverse_payment(db, payment, reason="chargeback", ref=str(dispute.get("id")))
    return "processed", f"{payment.id} reversed {out['reversed']}"


_RAZORPAY: dict[str, Handler] = {
    "payment_link.paid": _rp_payment_link_paid,
    "subscription.charged": _rp_subscription_charged,
    "subscription.cancelled": _rp_subscription_ended,
    "subscription.completed": _rp_subscription_ended,
    "subscription.halted": _rp_subscription_ended,
    "refund.processed": _rp_refund,
    "payment.refunded": _rp_refund,
    "payment.dispute.created": _rp_dispute,
}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    raw = await request.body()
    if not settings.razorpay_webhook_secret:
        return JSONResponse({"error": "not_configured", "message": "Razorpay webhooks are not configured."},
                            status_code=503)
    if not providers.verify_razorpay_signature(raw, request.headers.get("x-razorpay-signature"),
                                               settings.razorpay_webhook_secret):
        return await run_in_threadpool(_reject, "razorpay", request, "signature")
    try:
        event = json.loads(raw)
        etype = str(event.get("event", ""))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "invalid_payload", "message": "Unreadable event."}, status_code=400)
    event_id = request.headers.get("x-razorpay-event-id") or ("sha256:" + hashlib.sha256(raw).hexdigest())
    return await run_in_threadpool(process_event, "razorpay", event_id, etype, _RAZORPAY.get(etype), event)
