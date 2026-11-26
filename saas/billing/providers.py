"""Payment provider adapters over raw HTTP (httpx, no SDKs): Stripe, Razorpay and a dev provider.

A provider only creates hosted checkouts, cancels subscriptions and verifies webhook signatures.
What a payment is worth is never taken from the provider's redirect or the client: fulfilment
(saas.billing.service) compares every webhook's amount and currency with our own Payment row.

The `dev` provider credits instantly and exists only when SAAS_ENV=dev. It is never a fallback for a
missing key in prod: there, an unconfigured provider is a 503.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx

from ..auth.crypto import sign
from ..auth.errors import ApiError
from ..config import settings

log = logging.getLogger("saas.billing.providers")


@dataclass
class CheckoutRequest:
    payment_id: str
    kind: str  # plan | pack
    item_name: str
    interval: str  # month | year | once
    currency: str
    amount_minor: int
    customer_email: str
    user_id: str
    subscription_id: Optional[str] = None
    provider_refs: Optional[dict] = None


@dataclass
class CheckoutSession:
    redirect_url: str
    provider_order_id: str
    provider_subscription_id: Optional[str] = None


class Provider(Protocol):
    name: str

    def configured(self) -> bool: ...
    def create_checkout(self, req: CheckoutRequest) -> CheckoutSession: ...
    def cancel_subscription(self, provider_subscription_id: str) -> None: ...


def _return_url(payment_id: str) -> str:
    return f"{settings.public_url.rstrip('/')}/billing/return?payment_id={payment_id}"


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))


def _upstream_error(provider: str, r: httpx.Response) -> ApiError:
    log.error("%s API error %s: %s", provider, r.status_code, r.text[:300])
    return ApiError(502, "payment_provider_error", "The payment provider refused the request. Try again shortly.")


# ---- Stripe ---------------------------------------------------------------------------------------

def verify_stripe_signature(payload: bytes, header: Optional[str], secret: str, *, tolerance: int,
                            now: Optional[float] = None) -> bool:
    """`Stripe-Signature: t=<ts>,v1=<hex>[,v1=...][,v0=...][,other=...]`; HMAC-SHA256 of `<t>.<raw body>`.

    Unknown entries are ignored, every v1 is tried, and the timestamp must be within `tolerance`
    seconds (replay window)."""
    if not header or not secret:
        return False
    ts: Optional[int] = None
    sigs: list[str] = []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            try:
                ts = int(v)
            except ValueError:
                return False
        elif k == "v1" and v:
            sigs.append(v.strip())
    if ts is None or not sigs:
        return False
    now = time.time() if now is None else now
    if tolerance > 0 and abs(now - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("ascii") + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


class StripeProvider:
    name = "stripe"

    def configured(self) -> bool:
        return bool(settings.stripe_secret_key)

    def _headers(self, idem: Optional[str] = None) -> dict:
        h = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
        if idem:
            h["Idempotency-Key"] = idem
        return h

    def create_checkout(self, req: CheckoutRequest) -> CheckoutSession:
        base = settings.public_url.rstrip("/")
        data: dict[str, Any] = {
            "mode": "subscription" if req.kind == "plan" else "payment",
            "success_url": _return_url(req.payment_id),
            "cancel_url": f"{base}/app/billing?status=canceled",
            "client_reference_id": req.payment_id,
            "customer_email": req.customer_email,
            "metadata[payment_id]": req.payment_id,
            "metadata[user_id]": req.user_id,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": req.currency.lower(),
            "line_items[0][price_data][unit_amount]": str(req.amount_minor),
            "line_items[0][price_data][product_data][name]": req.item_name[:200],
        }
        if req.kind == "plan":
            data["line_items[0][price_data][recurring][interval]"] = req.interval
            data["subscription_data[metadata][payment_id]"] = req.payment_id
            data["subscription_data[metadata][sub_id]"] = req.subscription_id or ""
            data["subscription_data[metadata][user_id]"] = req.user_id
        else:
            data["payment_intent_data[metadata][payment_id]"] = req.payment_id
            data["payment_intent_data[metadata][user_id]"] = req.user_id
        with _client() as c:
            try:
                r = c.post(f"{settings.stripe_api_base}/v1/checkout/sessions", data=data,
                           headers=self._headers(idem=f"checkout-{req.payment_id}"))
            except httpx.HTTPError:
                raise ApiError(502, "payment_provider_unreachable", "Couldn't reach the payment provider. Try again.")
        if r.status_code != 200:
            raise _upstream_error("stripe", r)
        body = r.json()
        if not body.get("url") or not body.get("id"):
            raise _upstream_error("stripe", r)
        return CheckoutSession(redirect_url=body["url"], provider_order_id=body["id"])

    def cancel_subscription(self, provider_subscription_id: str) -> None:
        with _client() as c:
            r = c.post(f"{settings.stripe_api_base}/v1/subscriptions/{provider_subscription_id}",
                       data={"cancel_at_period_end": "true"}, headers=self._headers())
        if r.status_code != 200:
            raise _upstream_error("stripe", r)

    def fetch_charge(self, charge_id: str) -> Optional[dict]:
        if not charge_id.startswith("ch_") and not charge_id.startswith("py_"):
            return None
        try:
            with _client() as c:
                r = c.get(f"{settings.stripe_api_base}/v1/charges/{charge_id}", headers=self._headers())
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None


# ---- Razorpay -------------------------------------------------------------------------------------

def verify_razorpay_signature(payload: bytes, header: Optional[str], secret: str) -> bool:
    """`X-Razorpay-Signature`: hex HMAC-SHA256 of the raw body with the webhook secret."""
    if not header or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


class RazorpayProvider:
    name = "razorpay"

    def configured(self) -> bool:
        return bool(settings.razorpay_key_id and settings.razorpay_key_secret)

    def _auth(self) -> tuple[str, str]:
        return settings.razorpay_key_id, settings.razorpay_key_secret

    def create_checkout(self, req: CheckoutRequest) -> CheckoutSession:
        notes = {"payment_id": req.payment_id, "user_id": req.user_id}
        if req.kind == "plan":
            plan_id = (req.provider_refs or {}).get("razorpay_plan_id")
            if not plan_id:
                raise ApiError(503, "plan_not_configured",
                               "This plan can't be bought in this currency yet. Contact support.")
            body = {"plan_id": plan_id, "total_count": 120 if req.interval == "month" else 10, "quantity": 1,
                    "customer_notify": 1, "notes": {**notes, "sub_id": req.subscription_id or ""}}
            url = f"{settings.razorpay_api_base}/v1/subscriptions"
        else:
            body = {"amount": req.amount_minor, "currency": req.currency.upper(), "description": req.item_name[:200],
                    "reference_id": req.payment_id[:40], "customer": {"email": req.customer_email},
                    "notify": {"sms": False, "email": False}, "reminder_enable": False, "notes": notes,
                    "callback_url": _return_url(req.payment_id), "callback_method": "get",
                    "expire_by": int(time.time()) + 24 * 3600}
            url = f"{settings.razorpay_api_base}/v1/payment_links"
        with _client() as c:
            try:
                r = c.post(url, json=body, auth=self._auth())
            except httpx.HTTPError:
                raise ApiError(502, "payment_provider_unreachable", "Couldn't reach the payment provider. Try again.")
        if r.status_code not in (200, 201):
            raise _upstream_error("razorpay", r)
        data = r.json()
        if not data.get("short_url") or not data.get("id"):
            raise _upstream_error("razorpay", r)
        if req.kind == "plan":
            return CheckoutSession(redirect_url=data["short_url"], provider_order_id=data["id"],
                                   provider_subscription_id=data["id"])
        return CheckoutSession(redirect_url=data["short_url"], provider_order_id=data["id"])

    def cancel_subscription(self, provider_subscription_id: str) -> None:
        with _client() as c:
            r = c.post(f"{settings.razorpay_api_base}/v1/subscriptions/{provider_subscription_id}/cancel",
                       json={"cancel_at_cycle_end": 1}, auth=self._auth())
        if r.status_code not in (200, 201):
            raise _upstream_error("razorpay", r)


# ---- dev ------------------------------------------------------------------------------------------

def dev_signature(payment_id: str) -> str:
    return sign("dev-checkout", payment_id)


class DevProvider:
    name = "dev"

    def configured(self) -> bool:
        return not settings.is_prod

    def create_checkout(self, req: CheckoutRequest) -> CheckoutSession:
        if settings.is_prod:
            raise ApiError(503, "payments_unavailable", "Payments are not available right now.")
        return CheckoutSession(redirect_url=f"/billing/dev/complete/{req.payment_id}?sig={dev_signature(req.payment_id)}",
                               provider_order_id=f"dev_{req.payment_id}",
                               provider_subscription_id=f"dev_sub_{req.subscription_id}" if req.subscription_id else None)

    def cancel_subscription(self, provider_subscription_id: str) -> None:
        return None


_PROVIDERS: dict[str, Provider] = {"stripe": StripeProvider(), "razorpay": RazorpayProvider(), "dev": DevProvider()}


def get(name: str) -> Provider:
    if name == "dev" and settings.is_prod:
        raise ApiError(503, "payments_unavailable", "Payments are not available right now.")
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise ApiError(503, "payments_unavailable", "Payments are not available right now.")


def for_currency(currency: str) -> Provider:
    name = settings.payment_provider_inr if currency.upper() == "INR" else settings.payment_provider_default
    provider = get(name)
    if provider.configured():
        return provider
    if not settings.is_prod:
        return _PROVIDERS["dev"]  # dev only: no keys configured locally
    raise ApiError(503, "payments_unavailable", "Payments are not available right now. Try again later.")
