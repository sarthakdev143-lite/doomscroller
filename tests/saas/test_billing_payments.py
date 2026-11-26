"""Checkout, webhooks (signature, replay, tampering, idempotency), subscriptions, refunds, promos, referrals."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta

import httpx
import pytest

from core_fixtures import PASSWORD, core, csrf, form_fields, login, make_user, outbox_link  # noqa: F401

WHSEC = "whsec_test_secret"
RZP_SECRET = "rzp_webhook_secret"


@pytest.fixture()
def stripe(core, monkeypatch):
    """Stripe configured, its HTTP API mocked. Returns the list of captured requests."""
    from saas.billing import providers

    monkeypatch.setattr(core, "payment_provider_default", "stripe")  # dormant unless selected
    monkeypatch.setattr(core, "stripe_secret_key", "sk_test_123")
    monkeypatch.setattr(core, "stripe_webhook_secret", WHSEC)
    calls: list[httpx.Request] = []
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/checkout/sessions":
            counter["n"] += 1
            return httpx.Response(200, json={"id": f"cs_test_{counter['n']}", "url": f"https://checkout.stripe.test/{counter['n']}"})
        if request.url.path.startswith("/v1/subscriptions/"):
            return httpx.Response(200, json={"id": "sub_ext", "cancel_at_period_end": True})
        return httpx.Response(404, json={})

    monkeypatch.setattr(providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return calls


def stripe_post(client, event: dict, *, secret: str = WHSEC, ts: int | None = None, tamper: bool = False):
    body = json.dumps(event).encode()
    t = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    if tamper:
        body = body.replace(b"900", b"100")
    return client.post("/webhooks/stripe", content=body,
                       headers={"Stripe-Signature": f"t={t},v1={sig},v0=ignored", "Content-Type": "application/json"})


def _buy(client, h, kind="pack", slug="pack-60", **extra):
    r = client.post("/api/billing/checkout", json={"kind": kind, "slug": slug, **extra}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _session_event(payment, *, evt="evt_1", amount=900, currency="usd", mode="payment", status="paid", sub=None):
    return {"id": evt, "type": "checkout.session.completed",
            "data": {"object": {"id": payment.provider_order_id, "object": "checkout.session", "mode": mode,
                                "client_reference_id": payment.id, "payment_status": status, "amount_total": amount,
                                "currency": currency, "payment_intent": "pi_123", "subscription": sub,
                                "customer": "cus_1"}}}


# ---- catalog & tiers ------------------------------------------------------------------------------

def test_catalog_regional_display(client, core):
    us = client.get("/api/billing/catalog?country=US").json()
    india = client.get("/api/billing/catalog?country=IN").json()
    ppp = client.get("/api/billing/catalog?country=PK").json()
    assert us["currency"] == "USD" and india["currency"] == "INR"
    starter_us = next(p for p in us["plans"] if p["slug"] == "starter")
    starter_in = next(p for p in india["plans"] if p["slug"] == "starter")
    starter_pk = next(p for p in ppp["plans"] if p["slug"] == "starter")
    assert starter_us["prices"]["month"]["display"] == "$15"
    assert starter_in["prices"]["month"]["currency"] == "INR"
    assert starter_pk["prices"]["month"]["amount_minor"] < starter_us["prices"]["month"]["amount_minor"]
    assert all(p["slug"] != "free" for p in us["plans"])
    assert [k["credits"] for k in us["packs"]] == [60, 300, 1000]


def test_checkout_tier_comes_from_server_country_not_the_client(client, core, saas_db, stripe, monkeypatch):
    from saas.models.billing import Payment

    make_user(saas_db, "tier@example.com")
    h = login(client, "tier@example.com")
    out = _buy(client, h)  # no trusted country header: default USD tier even if the page showed INR
    assert saas_db.get(Payment, out["payment_id"]).currency == "USD"
    monkeypatch.setattr(core, "trust_proxy", "cloudflare")
    out = client.post("/api/billing/checkout?country=US", json={"kind": "pack", "slug": "pack-60"},
                      headers={**h, "CF-IPCountry": "PK"}).json()
    p = saas_db.get(Payment, out["payment_id"])
    assert (p.currency, p.tier, p.amount_minor) == ("USD", "ppp_low", 500)


def test_checkout_sends_server_price_to_stripe(client, core, saas_db, stripe):
    make_user(saas_db, "form@example.com")
    h = login(client, "form@example.com")
    out = _buy(client, h, slug="pack-300")
    assert out["redirect_url"].startswith("https://checkout.stripe.test/")
    req = stripe[-1]
    form = dict(httpx.QueryParams(req.content.decode()))
    assert form["mode"] == "payment" and form["client_reference_id"] == out["payment_id"]
    assert form["line_items[0][price_data][unit_amount]"] == "3500"
    assert form["line_items[0][price_data][currency]"] == "usd"
    assert form["payment_intent_data[metadata][payment_id]"] == out["payment_id"]
    assert req.headers["idempotency-key"] == f"checkout-{out['payment_id']}"
    assert req.headers["authorization"] == "Bearer sk_test_123"


def test_unverified_users_cannot_check_out(client, core, saas_db, stripe):
    make_user(saas_db, "unv@example.com", verified=False)
    h = login(client, "unv@example.com")
    r = client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-60"}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "email_unverified"


# ---- Stripe webhooks ------------------------------------------------------------------------------

def test_stripe_pack_purchase_is_credited_once(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment

    u = make_user(saas_db, "buyer@example.com")
    h = login(client, "buyer@example.com")
    pay = saas_db.get(Payment, _buy(client, h)["payment_id"])
    assert stripe_post(client, _session_event(pay)).json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 60
    assert stripe_post(client, _session_event(pay)).json()["duplicate"] is True  # same event replayed
    assert stripe_post(client, _session_event(pay, evt="evt_2")).json()["status"] == "duplicate"  # new id, same order
    assert ledger.balance(saas_db, u.id) == 60
    saas_db.refresh(pay)
    assert pay.status == "paid" and pay.provider_payment_id == "pi_123"


def test_stripe_signature_failures(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment

    u = make_user(saas_db, "sig@example.com")
    h = login(client, "sig@example.com")
    pay = saas_db.get(Payment, _buy(client, h)["payment_id"])
    ev = _session_event(pay)
    assert stripe_post(client, ev, secret="whsec_wrong").status_code == 400
    assert stripe_post(client, ev, ts=int(time.time()) - 3600).status_code == 400  # replayed outside tolerance
    assert stripe_post(client, ev, tamper=True).status_code == 400  # body changed after signing
    r = client.post("/webhooks/stripe", content=json.dumps(ev), headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert ledger.balance(saas_db, u.id) == 0


def test_stripe_amount_tampering_is_refused(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment
    from saas.models.core import User

    u = make_user(saas_db, "tamper@example.com")
    h = login(client, "tamper@example.com")
    pay = saas_db.get(Payment, _buy(client, h)["payment_id"])
    r = stripe_post(client, _session_event(pay, amount=1))
    assert r.json()["status"] == "rejected"
    r = stripe_post(client, _session_event(pay, evt="evt_cur", currency="inr"))
    assert r.json()["status"] == "rejected"
    assert ledger.balance(saas_db, u.id) == 0
    saas_db.expire_all()
    assert "payment_mismatch" in saas_db.get(User, u.id).flags
    # A real session id for someone else's payment is not accepted either.
    other = _session_event(pay, evt="evt_x")
    other["data"]["object"]["id"] = "cs_someone_else"
    assert stripe_post(client, other).json()["status"] == "ignored"


def test_unpaid_async_session_waits_for_async_success(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment

    u = make_user(saas_db, "async@example.com")
    h = login(client, "async@example.com")
    pay = saas_db.get(Payment, _buy(client, h)["payment_id"])
    assert stripe_post(client, _session_event(pay, status="unpaid")).json()["status"] == "ignored"
    ev = _session_event(pay, evt="evt_async")
    ev["type"] = "checkout.session.async_payment_succeeded"
    assert stripe_post(client, ev).json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 60


def _invoice(evt, inv, sub_ext, *, reason, amount, start, end, meta=None):
    return {"id": evt, "type": "invoice.paid",
            "data": {"object": {"id": inv, "object": "invoice", "billing_reason": reason, "amount_paid": amount,
                                "currency": "usd",
                                "parent": {"type": "subscription_details",
                                           "subscription_details": {"subscription": sub_ext, "metadata": meta or {}}},
                                "lines": {"data": [{"period": {"start": int(start), "end": int(end)}}]}}}}


def test_stripe_subscription_grants_monthly_with_rollover_cap(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment, Subscription
    from saas.models.core import User

    u = make_user(saas_db, "subber@example.com")
    h = login(client, "subber@example.com")
    out = _buy(client, h, kind="plan", slug="starter", interval="month")
    pay = saas_db.get(Payment, out["payment_id"])
    sub = saas_db.get(Subscription, pay.subscription_id)
    assert sub.status == "incomplete" and pay.amount_minor == 1500
    form = dict(httpx.QueryParams(stripe[-1].content.decode()))
    assert form["mode"] == "subscription" and form["line_items[0][price_data][recurring][interval]"] == "month"
    assert form["subscription_data[metadata][sub_id]"] == sub.id

    assert stripe_post(client, _session_event(pay, mode="subscription", sub="sub_ext_1")).json()["status"] == "processed"
    t0 = time.time()
    month = 30 * 86400
    r = stripe_post(client, _invoice("evt_i1", "in_1", "sub_ext_1", reason="subscription_create", amount=1500,
                                     start=t0, end=t0 + month))
    assert r.json()["status"] == "processed"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 150 and saas_db.get(User, u.id).plan_slug == "starter"
    r = stripe_post(client, _invoice("evt_i2", "in_2", "sub_ext_1", reason="subscription_cycle", amount=1500,
                                     start=t0 + month, end=t0 + 2 * month))
    assert r.json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 300  # 150 carried (cap = 1 month) + 150
    r = stripe_post(client, _invoice("evt_i3", "in_3", "sub_ext_1", reason="subscription_cycle", amount=1500,
                                     start=t0 + 2 * month, end=t0 + 3 * month))
    assert ledger.balance(saas_db, u.id) == 300  # 150 above the cap expired, then 150 granted
    assert stripe_post(client, _invoice("evt_i3b", "in_3", "sub_ext_1", reason="subscription_cycle", amount=1500,
                                        start=t0 + 2 * month, end=t0 + 3 * month)).json()["status"] == "duplicate"
    bad = stripe_post(client, _invoice("evt_i4", "in_4", "sub_ext_1", reason="subscription_cycle", amount=100,
                                       start=t0 + 3 * month, end=t0 + 4 * month))
    assert bad.json()["status"] == "rejected"
    assert ledger.balance(saas_db, u.id) == 300

    deleted = {"id": "evt_del", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_ext_1"}}}
    assert stripe_post(client, deleted).json()["status"] == "processed"
    saas_db.expire_all()
    assert saas_db.get(User, u.id).plan_slug == "free"
    assert ledger.balance(saas_db, u.id) == 300  # credits already granted stay


def test_invoice_before_link_uses_metadata_or_retries(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment

    u = make_user(saas_db, "early@example.com")
    h = login(client, "early@example.com")
    pay = saas_db.get(Payment, _buy(client, h, kind="plan", slug="starter", interval="month")["payment_id"])
    t0 = time.time()
    r = stripe_post(client, _invoice("evt_e1", "in_e1", "sub_unknown", reason="subscription_create", amount=1500,
                                     start=t0, end=t0 + 86400 * 30))
    assert r.status_code == 503  # Stripe will redeliver after checkout.session.completed links it
    r = stripe_post(client, _invoice("evt_e2", "in_e1", "sub_unknown", reason="subscription_create", amount=1500,
                                     start=t0, end=t0 + 86400 * 30,
                                     meta={"sub_id": pay.subscription_id, "payment_id": pay.id}))
    assert r.json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 150
    forged = stripe_post(client, _invoice("evt_e3", "in_e2", "sub_other", reason="subscription_create", amount=1500,
                                          start=t0, end=t0 + 86400 * 30,
                                          meta={"sub_id": pay.subscription_id, "payment_id": "pay_forged"}))
    assert forged.status_code == 503


def test_refund_after_spending_clamps_and_holds_account(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment
    from saas.models.core import User

    u = make_user(saas_db, "refund@example.com")
    h = login(client, "refund@example.com")
    pay = saas_db.get(Payment, _buy(client, h)["payment_id"])
    stripe_post(client, _session_event(pay))
    ledger.reserve(saas_db, u.id, "job_spent", 50)
    ledger.settle(saas_db, "job_spent", 50)
    saas_db.commit()
    ev = {"id": "evt_ref", "type": "charge.refunded",
          "data": {"object": {"id": "ch_1", "amount": 900, "amount_refunded": 900, "payment_intent": "pi_123",
                              "metadata": {}}}}
    assert stripe_post(client, ev).json()["status"] == "processed"
    saas_db.expire_all()
    user = saas_db.get(User, u.id)
    assert ledger.balance(saas_db, u.id) == 0  # never negative
    assert user.billing_hold is True and user.flags["refund"]["shortfall"] == 50
    assert saas_db.get(Payment, pay.id).status == "refunded"
    r = client.post("/api/jobs", json={"source_url": "https://youtube.com/watch?v=x"}, headers=csrf(client))
    assert r.json()["error"] == "billing_hold"


def test_partial_refund_and_chargeback(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment
    from saas.models.core import User

    u = make_user(saas_db, "partial@example.com")
    h = login(client, "partial@example.com")
    pay = saas_db.get(Payment, _buy(client, h)["payment_id"])
    stripe_post(client, _session_event(pay))
    ev = {"id": "evt_p1", "type": "charge.refunded",
          "data": {"object": {"id": "ch_1", "amount": 900, "amount_refunded": 450, "payment_intent": "pi_123"}}}
    stripe_post(client, ev)
    assert ledger.balance(saas_db, u.id) == 30
    saas_db.expire_all()
    assert saas_db.get(User, u.id).billing_hold is False
    dispute = {"id": "evt_d1", "type": "charge.dispute.created",
               "data": {"object": {"id": "dp_1", "charge": "ch_1", "payment_intent": "pi_123", "amount": 900}}}
    assert stripe_post(client, dispute).json()["status"] == "processed"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 0
    assert saas_db.get(User, u.id).billing_hold is True
    assert saas_db.get(Payment, pay.id).status == "disputed"


# ---- Razorpay -------------------------------------------------------------------------------------

@pytest.fixture()
def razorpay(core, monkeypatch):
    from saas.billing import providers

    monkeypatch.setattr(core, "payment_provider_inr", "razorpay")  # dormant unless selected
    monkeypatch.setattr(core, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(core, "razorpay_key_secret", "rzp_test_secret")
    monkeypatch.setattr(core, "razorpay_webhook_secret", RZP_SECRET)
    monkeypatch.setattr(core, "trust_proxy", "cloudflare")
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.test/x", "status": "created"})

    monkeypatch.setattr(providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return calls


def rzp_post(client, event, *, secret=RZP_SECRET, event_id="rzp_evt_1"):
    body = json.dumps(event).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/razorpay", content=body,
                       headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": event_id,
                                "Content-Type": "application/json"})


def test_razorpay_inr_pack(client, core, saas_db, razorpay):
    from saas.billing import ledger
    from saas.models.billing import Payment

    u = make_user(saas_db, "india@example.com")
    h = login(client, "india@example.com")
    out = client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-60"},
                      headers={**h, "CF-IPCountry": "IN"}).json()
    pay = saas_db.get(Payment, out["payment_id"])
    assert (pay.provider, pay.currency, pay.amount_minor) == ("razorpay", "INR", 49900)
    sent = json.loads(razorpay[-1].content)
    assert sent["amount"] == 49900 and sent["reference_id"] == pay.id and sent["currency"] == "INR"
    event = {"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"id": "plink_1", "amount": 49900, "amount_paid": 49900, "currency": "INR",
                                    "reference_id": pay.id, "status": "paid"}},
        "payment": {"entity": {"id": "pay_rzp_1", "amount": 49900, "currency": "INR", "status": "captured"}}}}
    assert rzp_post(client, event, secret="wrong").status_code == 400
    assert rzp_post(client, event).json()["status"] == "processed"
    assert rzp_post(client, event).json()["duplicate"] is True
    assert ledger.balance(saas_db, u.id) == 60
    refund = {"event": "refund.processed", "payload": {
        "refund": {"entity": {"id": "rfnd_1", "payment_id": "pay_rzp_1", "amount": 49900}},
        "payment": {"entity": {"id": "pay_rzp_1", "amount": 49900, "amount_refunded": 49900}}}}
    assert rzp_post(client, refund, event_id="rzp_evt_2").json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 0


def test_razorpay_amount_mismatch(client, core, saas_db, razorpay):
    from saas.billing import ledger
    from saas.models.billing import Payment

    u = make_user(saas_db, "india2@example.com")
    h = login(client, "india2@example.com")
    out = client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-60"},
                      headers={**h, "CF-IPCountry": "IN"}).json()
    pay = saas_db.get(Payment, out["payment_id"])
    event = {"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": {"id": "plink_1", "reference_id": pay.id, "status": "paid"}},
        "payment": {"entity": {"id": "pay_rzp_2", "amount": 100, "currency": "INR", "status": "captured"}}}}
    assert rzp_post(client, event).json()["status"] == "rejected"
    assert ledger.balance(saas_db, u.id) == 0


# ---- dev provider ---------------------------------------------------------------------------------

def test_dev_provider_credits_instantly_only_in_dev(client, core, saas_db, monkeypatch):
    from saas.billing import ledger, providers
    from saas.billing.routes import dev_complete  # noqa: F401
    from saas.models.billing import Payment

    u = make_user(saas_db, "dev@example.com")
    h = login(client, "dev@example.com")
    out = _buy(client, h)
    assert out["provider"] == "dev"
    url = out["redirect_url"]
    bad = url[:-4] + "AAAA"
    assert client.get(bad, follow_redirects=False).status_code == 404
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/billing?status=paid"
    client.get(url, follow_redirects=False)  # refresh of the page: no double credit
    assert ledger.balance(saas_db, u.id) == 60
    assert saas_db.get(Payment, out["payment_id"]).status == "paid"
    monkeypatch.setattr(core, "env", "prod")
    assert client.get(url, follow_redirects=False).status_code == 404
    with pytest.raises(Exception):
        providers.for_currency("USD")  # no keys in prod -> 503, never the dev provider


def test_annual_plan_grants_monthly(client, core, saas_db):
    from saas.billing import ledger, service
    from saas.common import utcnow
    from saas.models.billing import Subscription

    u = make_user(saas_db, "annual@example.com")
    h = login(client, "annual@example.com")
    out = _buy(client, h, kind="plan", slug="pro", interval="year")
    client.get(out["redirect_url"], follow_redirects=False)
    assert ledger.balance(saas_db, u.id) == 500
    sub = saas_db.query(Subscription).filter(Subscription.user_id == u.id).one()
    assert sub.interval == "year" and sub.status == "active"
    assert service.grant_due_subscription_credits(saas_db, utcnow() + timedelta(days=32)) == 1
    assert service.grant_due_subscription_credits(saas_db, utcnow() + timedelta(days=32)) == 0  # idempotent
    saas_db.commit()
    assert ledger.balance(saas_db, u.id) == 1000


# ---- promos & referrals ---------------------------------------------------------------------------

def _promo(db, code, **kw):
    from saas.models.billing import PromoCode

    p = PromoCode(code=code, **kw)
    db.add(p)
    db.commit()
    return p


def test_credit_promo_single_use_and_limited(client, core, saas_db):
    from saas.billing import ledger
    from saas.common import utcnow

    _promo(saas_db, "WELCOME10", kind="credits", credits=10, max_redemptions=2)
    _promo(saas_db, "OLD", kind="credits", credits=10, expires_at=utcnow() - timedelta(days=1))
    users = [make_user(saas_db, f"promo{i}@example.com") for i in range(3)]
    results = []
    for i, u in enumerate(users):
        h = login(client, f"promo{i}@example.com")
        results.append(client.post("/api/billing/promo", json={"code": "welcome10"}, headers=h).status_code)
        if i == 0:
            again = client.post("/api/billing/promo", json={"code": "WELCOME10"}, headers=h)
            assert again.status_code == 409
            assert client.post("/api/billing/promo", json={"code": "OLD"}, headers=h).status_code == 404
    assert results == [200, 200, 410]
    assert [ledger.balance(saas_db, u.id) for u in users] == [10, 10, 0]


def test_percent_promo_discounts_packs_only(client, core, saas_db, stripe):
    from saas.billing import ledger
    from saas.models.billing import Payment

    _promo(saas_db, "HALF", kind="percent_off", percent_off=50, applies_to="pack", max_redemptions=10)
    u = make_user(saas_db, "half@example.com")
    h = login(client, "half@example.com")
    out = _buy(client, h, promo="HALF")
    pay = saas_db.get(Payment, out["payment_id"])
    assert pay.amount_minor == 450 and pay.list_amount_minor == 900
    r = client.post("/api/billing/checkout", json={"kind": "plan", "slug": "starter", "promo": "HALF"}, headers=h)
    assert r.json()["error"] == "promo_not_applicable"
    assert stripe_post(client, _session_event(pay, amount=450)).json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 60


def test_referral_rewarded_after_first_paid_purchase_only(client, core, saas_db):
    from saas.billing import ledger
    from saas.models.billing import Referral
    from saas.models.core import User

    referrer = make_user(saas_db, "referrer@example.com")
    client.post("/auth/signup", json={"email": "friend@example.com", "password": PASSWORD, "ref": referrer.referral_code,
                                      **form_fields()})
    client.get(f"/auth/verify/{outbox_link('/auth/verify/')}", follow_redirects=False)
    friend = saas_db.query(User).filter(User.email == "friend@example.com").one()
    assert friend.referred_by == referrer.id
    assert saas_db.query(Referral).one().status == "pending"
    assert ledger.balance(saas_db, referrer.id) == 0  # signup alone earns nothing
    h = csrf(client)
    for _ in range(2):
        out = _buy(client, h)
        client.get(out["redirect_url"], follow_redirects=False)
    saas_db.expire_all()
    assert ledger.balance(saas_db, referrer.id) == core.referral_reward_credits  # once, not per purchase
    assert saas_db.query(Referral).one().status == "rewarded"


def test_self_referral_from_same_device_is_void(client, core, saas_db):
    from saas.models.billing import Referral

    fp = "c" * 32
    client.post("/auth/signup", json={"email": "me1@example.com", "password": PASSWORD, **form_fields(), "fp": fp})
    from saas.models.core import User

    me1 = saas_db.query(User).filter(User.email == "me1@example.com").one()
    client.cookies.clear()
    client.post("/auth/signup", json={"email": "me2@example.com", "password": PASSWORD, "ref": me1.referral_code,
                                      **form_fields(), "fp": fp})
    assert saas_db.query(Referral).one().status == "void"


def test_ledger_and_payments_endpoints_are_scoped(client, core, saas_db):
    a = make_user(saas_db, "la@example.com", credits=7)
    make_user(saas_db, "lb@example.com")
    h = login(client, "lb@example.com")
    body = client.get("/api/billing/ledger").json()
    assert body["balance"] == 0 and body["entries"] == []
    assert client.get("/api/billing/payments").json()["payments"] == []
    assert a.id not in json.dumps(client.get("/api/billing/referrals").json())
    assert h
