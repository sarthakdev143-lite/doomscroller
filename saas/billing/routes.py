"""Billing endpoints (docs/saas/CONTRACT.md, "Billing"). Webhooks live in webhooks.py and are included here."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import compat
from ..auth.crypto import verify_sig
from ..auth.deps import optional_user, require_user, require_verified_user
from ..auth.errors import ApiError, ApiRoute
from ..common import utcnow
from ..config import settings
from ..db import get_db
from ..models.billing import Payment, Subscription
from ..models.core import User
from . import catalog, ledger, service
from .bmc import router as bmc_router
from .webhooks import router as webhooks_router

router = APIRouter(route_class=ApiRoute)
router.include_router(webhooks_router)
router.include_router(bmc_router)


class CheckoutIn(BaseModel):
    kind: Literal["plan", "pack"]
    slug: str = Field(max_length=40)
    interval: Optional[Literal["month", "year", "once"]] = None
    promo: Optional[str] = Field(default=None, max_length=40)


class PromoIn(BaseModel):
    code: str = Field(max_length=40)


@router.get("/api/billing/catalog")
def get_catalog(request: Request, country: Optional[str] = None, db: Session = Depends(get_db)):
    shown = country if country and len(country) == 2 and country.isalpha() else service.server_country(request)
    return catalog.public_catalog(db, shown)


@router.post("/api/billing/checkout")
def checkout(body: CheckoutIn, request: Request, user: User = Depends(require_verified_user),
             db: Session = Depends(get_db)):
    compat.rate_limit(request, "checkout", 20, 3600, key=f"user:{user.id}")
    return service.start_checkout(db, user, request, kind=body.kind, slug=body.slug,
                                  interval=body.interval if body.interval != "once" else None, promo_code=body.promo)


@router.get("/api/billing/ledger")
def get_ledger(limit: int = 50, before: Optional[str] = None, user: User = Depends(require_user),
               db: Session = Depends(get_db)):
    rows = ledger.entries(db, user.id, limit=limit, before=before)
    return {"balance": ledger.balance(db, user.id), "entries": [ledger.public_entry(e) for e in rows],
            "next": rows[-1].id if len(rows) == max(1, min(limit, 200)) else None}


@router.get("/api/billing/subscription")
def get_subscription(user: User = Depends(require_user), db: Session = Depends(get_db)):
    sub = (db.query(Subscription).filter(Subscription.user_id == user.id,
                                         Subscription.status.in_(("active", "past_due")))
           .order_by(Subscription.created_at.desc()).first())
    return {"subscription": service.public_subscription(sub), "plan": user.plan_slug}


@router.post("/api/billing/subscription/cancel")
def cancel(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    compat.rate_limit(request, "sub_cancel", 10, 3600, key=f"user:{user.id}")
    sub = service.cancel_subscription(db, user)
    db.commit()
    return {"ok": True, "subscription": service.public_subscription(sub)}


@router.post("/api/billing/promo")
def promo(body: PromoIn, request: Request, user: User = Depends(require_verified_user), db: Session = Depends(get_db)):
    compat.rate_limit(request, "promo", 10, 3600, key=f"user:{user.id}")
    try:
        out = service.redeem_credit_promo(db, user, body.code)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "promo_used", "You've already used that code.")
    return out


@router.get("/api/billing/payments")
def payments(user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = (db.query(Payment).filter(Payment.user_id == user.id, Payment.status != "created")
            .order_by(Payment.created_at.desc()).limit(100).all())
    return {"payments": [service.public_payment(p) for p in rows]}


@router.get("/api/billing/referrals")
def referrals(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return service.referral_summary(db, user)


@router.get("/billing/return")
def billing_return(payment_id: Optional[str] = None, user: Optional[User] = Depends(optional_user),
                   db: Session = Depends(get_db)):
    status = "pending"
    if user is not None and payment_id:
        p = db.query(Payment).filter(Payment.id == payment_id, Payment.user_id == user.id).one_or_none()
        if p is not None:
            status = "paid" if p.status == "paid" else ("failed" if p.status in ("failed", "canceled") else "pending")
    return RedirectResponse(f"/app/billing?status={status}", status_code=303)


@router.get("/billing/dev/complete/{payment_id}")
def dev_complete(payment_id: str, sig: str = "", db: Session = Depends(get_db)):
    """The dev provider's 'checkout page': pays at once. 404 outside SAAS_ENV=dev."""
    if settings.is_prod or settings.env.lower() != "dev":
        raise ApiError(404, "not_found", "Not found.")
    if not verify_sig("dev-checkout", payment_id, sig):
        raise ApiError(404, "not_found", "Not found.")
    p = db.get(Payment, payment_id)
    if p is None or p.provider != "dev":
        raise ApiError(404, "not_found", "Not found.")
    if p.kind == "pack":
        service.fulfil_one_time(db, p, amount_minor=p.amount_minor, currency=p.currency,
                                provider_payment_id=f"dev_pay_{p.id}")
    else:
        sub = db.get(Subscription, p.subscription_id) if p.subscription_id else None
        if sub is not None:
            start = utcnow()
            end = service.add_months(start, 12 if sub.interval == "year" else 1)
            service.record_subscription_payment(db, sub, provider_ref=f"dev_inv_{p.id}",
                                                provider_payment_id=f"dev_pay_{p.id}", amount_minor=p.amount_minor,
                                                currency=p.currency, period_start=start, period_end=end, first=True)
    db.commit()
    return RedirectResponse("/app/billing?status=paid", status_code=303)
