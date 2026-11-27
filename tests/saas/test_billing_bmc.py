"""Buy Me a Coffee: signature, stable-id idempotency (event_id is per delivery), email matching with
pending claims, catalog mapping (levels/extras/amounts), memberships over periods, refunds, test events."""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import time

import pytest

from core_fixtures import PASSWORD, core, csrf, form_fields, login, make_user, outbox_link  # noqa: F401

SECRET = "bmc_signing_secret"
_delivery = itertools.count(100)


@pytest.fixture()
def bmc(core, monkeypatch, saas_db):
    from saas.models.billing import Price

    monkeypatch.setattr(core, "bmc_webhook_secret", SECRET)
    monkeypatch.setattr(core, "bmc_page_url", "https://buymeacoffee.com/clipper")
    starter = saas_db.query(Price).filter_by(item_kind="plan", item_slug="starter", interval="month",
                                             currency="USD", tier="default").one()
    starter.provider_refs = {"bmc_level_id": 501, "bmc_url": "https://buymeacoffee.com/clipper/membership"}
    pack = saas_db.query(Price).filter_by(item_kind="pack", item_slug="pack-300", currency="USD", tier="default").one()
    pack.provider_refs = {"bmc_extra_id": 77}
    saas_db.commit()


def send(client, etype, data, *, secret=SECRET, live=True, created=None, event_id=None):
    body = json.dumps({"event_id": event_id or next(_delivery), "type": etype, "live_mode": live,
                       "created": int(time.time()) if created is None else created, "attempt": 1,
                       "data": data}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/bmc", content=body,
                       headers={"x-signature-sha256": sig, "Content-Type": "application/json"})


def extra(pay_id, email, *, amount=35.0, extra_id=77, qty=1):
    return {"id": pay_id, "object": "payment", "transaction_id": f"pi_{pay_id}", "status": "succeeded",
            "refunded": "false", "amount": amount, "currency": "USD", "supporter_email": email,
            "support_type": "Extra", "extras": [{"id": extra_id, "object": "extra", "title": "300 minutes",
                                                 "amount": amount, "quantity": qty, "currency": "USD"}]}


def membership(mem_id, email, *, start, level=501, amount=15.0, status="active", cancel_at_end="false"):
    return {"id": mem_id, "object": "membership", "psp_id": f"sub_{mem_id}", "duration_type": "month",
            "status": status, "canceled": "false", "cancel_at_period_end": cancel_at_end, "paused": "false",
            "supporter_email": email, "membership_level_id": level, "membership_level_name": "Starter",
            "amount": amount, "currency": "USD", "current_period_start": int(start),
            "current_period_end": int(start + 30 * 86400)}


def test_signature_and_configuration(client, core, saas_db, bmc, monkeypatch):
    make_user(saas_db, "sig@example.com")
    assert send(client, "extra_purchase.created", extra(1, "sig@example.com"), secret="wrong").status_code == 400
    r = client.post("/webhooks/bmc", content=b"{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    monkeypatch.setattr(core, "bmc_webhook_secret", "")
    assert send(client, "extra_purchase.created", extra(1, "sig@example.com")).status_code == 503


def test_extra_purchase_credits_once_across_redeliveries(client, core, saas_db, bmc):
    from saas.billing import ledger

    u = make_user(saas_db, "buyer@example.com")
    r = send(client, "extra_purchase.created", extra(9001, "Buyer@Example.com"))
    assert r.json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 300
    # BMC's event_id is per delivery attempt, so a retry arrives with a new one: still credited once.
    assert send(client, "extra_purchase.created", extra(9001, "buyer@example.com")).json()["duplicate"] is True
    assert ledger.balance(saas_db, u.id) == 300


def test_support_payment_matches_pack_by_exact_amount(client, core, saas_db, bmc):
    from saas.billing import ledger

    u = make_user(saas_db, "coffee@example.com")
    data = {"id": 42, "status": "succeeded", "refunded": "false", "amount": 9.0, "currency": "USD",
            "supporter_email": "coffee@example.com", "support_type": "Supporter", "coffee_count": 3, "coffee_price": 3.0}
    assert send(client, "donation.created", data).json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 60  # $9 is the 60-minute pack
    odd = {**data, "id": 43, "amount": 7.0}
    assert send(client, "donation.created", odd).json()["status"] == "deferred"  # no pack costs $7
    assert ledger.balance(saas_db, u.id) == 60


def test_underpayment_is_rejected(client, core, saas_db, bmc):
    from saas.billing import ledger

    u = make_user(saas_db, "cheap@example.com")
    r = send(client, "extra_purchase.created", extra(55, "cheap@example.com", amount=1.0))
    assert r.json()["status"] == "rejected"
    assert ledger.balance(saas_db, u.id) == 0


def test_purchase_before_signup_is_claimed_on_verification(client, core, saas_db, bmc):
    from saas.billing import ledger
    from saas.models.billing import PendingClaim
    from saas.models.core import User

    assert send(client, "extra_purchase.created", extra(7, "later@example.com")).json()["status"] == "deferred"
    claim = saas_db.query(PendingClaim).one()
    assert claim.status == "pending" and claim.email == "later@example.com"
    client.post("/auth/signup", json={"email": "later@example.com", "password": PASSWORD, **form_fields()})
    u = saas_db.query(User).filter_by(email="later@example.com").one()
    assert ledger.balance(saas_db, u.id) == 0  # unverified: a squatter can't catch someone's purchase
    client.get(f"/auth/verify/{outbox_link('/auth/verify/')}", follow_redirects=False)
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 300
    assert saas_db.query(PendingClaim).one().status == "claimed"


def test_membership_periods_renewals_and_cancellation(client, core, saas_db, bmc):
    from saas.billing import ledger, service
    from saas.common import utcnow
    from saas.models.billing import Subscription
    from saas.models.core import User

    u = make_user(saas_db, "member@example.com")
    t0 = time.time() - 3600
    month = 30 * 86400
    assert send(client, "membership.started", membership(300, "member@example.com", start=t0)).json()["status"] == "processed"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 150 and saas_db.get(User, u.id).plan_slug == "starter"
    # Same period seen again (an update that changed nothing billable): no second grant.
    r = send(client, "membership.updated", membership(300, "member@example.com", start=t0, cancel_at_end="true"))
    assert r.json()["status"] == "duplicate"
    assert ledger.balance(saas_db, u.id) == 150
    # A new current_period_start is the renewal signal.
    send(client, "membership.updated", membership(300, "member@example.com", start=t0 + month))
    assert ledger.balance(saas_db, u.id) == 300
    bad = send(client, "membership.updated", membership(300, "member@example.com", start=t0 + 2 * month, amount=1.0))
    assert bad.json()["status"] == "rejected"
    r = send(client, "membership.cancelled", membership(300, "member@example.com", start=t0 + month,
                                                         status="canceled", cancel_at_end="true"))
    assert r.json()["status"] == "processed"
    saas_db.expire_all()
    sub = saas_db.query(Subscription).one()
    assert sub.cancel_at_period_end is True and saas_db.get(User, u.id).plan_slug == "starter"  # paid-up period
    from datetime import timedelta

    service.expire_ended_subscriptions(saas_db, utcnow() + timedelta(days=61))
    saas_db.commit()
    saas_db.expire_all()
    assert saas_db.get(User, u.id).plan_slug == "free"


def test_refund_reverses_credits(client, core, saas_db, bmc):
    from saas.billing import ledger

    u = make_user(saas_db, "refund-bmc@example.com")
    send(client, "extra_purchase.created", extra(808, "refund-bmc@example.com"))
    assert ledger.balance(saas_db, u.id) == 300
    data = {**extra(808, "refund-bmc@example.com"), "status": "refunded", "refunded": "true"}
    assert send(client, "extra_purchase.refunded", data).json()["status"] == "processed"
    assert ledger.balance(saas_db, u.id) == 0


def test_test_events_and_stale_events_are_ignored(client, core, saas_db, bmc, monkeypatch):
    from saas.billing import ledger

    u = make_user(saas_db, "test-evt@example.com")
    r = send(client, "extra_purchase.created", extra(1, "test-evt@example.com"), live=False, event_id=1)
    assert r.json()["status"] == "ignored"
    old = int(time.time()) - (core.bmc_max_event_age_hours + 1) * 3600
    assert send(client, "extra_purchase.created", extra(2, "test-evt@example.com"), created=old).json()["status"] == "ignored"
    assert ledger.balance(saas_db, u.id) == 0
    monkeypatch.setattr(core, "bmc_accept_test_events", True)
    assert send(client, "extra_purchase.created", extra(3, "test-evt@example.com"), live=False).json()["status"] == "processed"


def test_checkout_redirects_to_bmc_and_webhook_fills_that_order(client, core, saas_db, bmc):
    from saas.models.billing import Payment

    make_user(saas_db, "flow@example.com")
    h = login(client, "flow@example.com")
    out = client.post("/api/billing/checkout", json={"kind": "plan", "slug": "starter"}, headers=h).json()
    assert out["provider"] == "bmc" and out["redirect_url"] == "https://buymeacoffee.com/clipper/membership"
    out2 = client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-300"}, headers=h).json()
    assert out2["redirect_url"] == "https://buymeacoffee.com/clipper"
    send(client, "extra_purchase.created", extra(1234, "flow@example.com"))
    p = saas_db.get(Payment, out2["payment_id"])
    saas_db.refresh(p)
    assert p.status == "paid" and p.provider_order_id == "pay:1234"
    r = client.post("/api/billing/subscription/cancel", headers=csrf(client))
    assert r.status_code == 404  # the membership hasn't started yet


def test_bmc_page_in_usd_for_a_rupee_catalog(client, core, saas_db, bmc):
    """BMC accounts default to USD; the item records what BMC really charges and payments are checked against it."""
    from saas.billing import ledger
    from saas.models.billing import Price

    pack = saas_db.query(Price).filter_by(item_kind="pack", item_slug="pack-300", currency="USD", tier="default").one()
    pack.currency, pack.amount_minor = "INR", 34900  # the catalog sells it for ₹349 ...
    pack.provider_refs = {"bmc_extra_id": 77, "bmc_amount_minor": 399, "bmc_currency": "USD"}  # ... BMC asks $3.99
    saas_db.commit()
    u = make_user(saas_db, "usd@example.com")
    assert send(client, "extra_purchase.created", extra(4001, "usd@example.com", amount=3.99)).json()["status"] == \
        "processed"
    assert ledger.balance(saas_db, u.id) == 300
    under = send(client, "extra_purchase.created", extra(4002, "usd@example.com", amount=1.0))
    assert under.json()["status"] == "rejected" and ledger.balance(saas_db, u.id) == 300
    rupees = send(client, "extra_purchase.created", extra(4003, "usd@example.com", amount=349.0) | {"currency": "INR"})
    assert rupees.json()["status"] == "rejected"  # not what the BMC page charges


def test_bmc_charges_one_price_everywhere(client, core, saas_db, bmc, monkeypatch):
    """BMC's page has one fixed price, so regional/PPP tiers and percent codes must not be offered."""
    from saas.billing import ledger
    from saas.models.billing import Payment, PromoCode

    monkeypatch.setattr(core, "trust_proxy", "cloudflare")
    india = client.get("/api/billing/catalog?country=IN").json()
    assert india["currency"] == "USD" and india["provider"] == "bmc" and india["promo_codes_at_checkout"] is False
    make_user(saas_db, "ppp@example.com")
    h = login(client, "ppp@example.com")
    out = client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-60"},
                      headers={**h, "CF-IPCountry": "PK"}).json()
    p = saas_db.get(Payment, out["payment_id"])
    assert (p.currency, p.tier, p.amount_minor) == ("USD", "default", 900)
    saas_db.add(PromoCode(code="HALFOFF", kind="percent_off", percent_off=50, applies_to="pack"))
    saas_db.commit()
    r = client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-60", "promo": "HALFOFF"}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "promo_not_applicable"
    # A $5 support must not match the cheaper PPP price of the 60-minute pack.
    u = make_user(saas_db, "five@example.com")
    data = {"id": 5005, "status": "succeeded", "refunded": "false", "amount": 5.0, "currency": "USD",
            "supporter_email": "five@example.com"}
    assert send(client, "donation.created", data).json()["status"] == "deferred"
    assert ledger.balance(saas_db, u.id) == 0
