"""The product seed catalog: the owner's rupee prices, all published, API + MCP on every paid plan, priority on
Studio; unpublished items (an admin can still hide one) are neither listed nor purchasable."""
from __future__ import annotations

import pytest

from core_fixtures import login, make_user  # noqa: F401

PRICES = {("pack", "pack-s", "once"): 9900, ("pack", "pack-m", "once"): 34900, ("pack", "pack-l", "once"): 149900,
          ("plan", "creator", "month"): 29900, ("plan", "creator", "year"): 299000,
          ("plan", "pro", "month"): 79900, ("plan", "pro", "year"): 799000,
          ("plan", "studio", "month"): 249900, ("plan", "studio", "year"): 2499000}
CREDITS = {"creator": 1000, "pro": 3000, "studio": 10000, "pack-s": 250, "pack-m": 1000, "pack-l": 5000}


@pytest.fixture()
def real_catalog(saas_db, monkeypatch):
    from saas.auth import compat
    from saas.billing import catalog
    from saas.config import settings
    from saas.security import limits

    monkeypatch.setattr(settings, "hibp_enabled", False)
    compat._local.reset()
    limits.reset_state()
    catalog.ensure_seeded(saas_db)
    return catalog


def test_seed_is_the_owners_rupee_table(saas_db, real_catalog):
    from saas.models.billing import Pack, Plan, Price, RegionTier

    prices = {(p.item_kind, p.item_slug, p.interval): (p.amount_minor, p.currency, p.tier) for p in saas_db.query(Price)}
    assert prices == {k: (v, "INR", "default") for k, v in PRICES.items()}  # INR only: no USD, PPP or BYOK rows
    plans = {p.slug: p for p in saas_db.query(Plan)}
    packs = {p.slug: p for p in saas_db.query(Pack)}
    assert set(plans) == {"free", "creator", "pro", "studio"} and set(packs) == {"pack-s", "pack-m", "pack-l"}
    for slug, credits in CREDITS.items():
        row = plans.get(slug) or packs[slug]
        assert (row.monthly_credits if slug in plans else row.credits) == credits and row.published is True
    for slug in ("creator", "pro", "studio"):
        assert plans[slug].features["api"] and plans[slug].features["mcp"] and plans[slug].rollover_months == 1
    assert plans["studio"].features["priority"] > plans["pro"].features["priority"] > plans["free"].features["priority"]
    assert plans["free"].features["free_grant_minutes"] == 10 and plans["free"].features["free_grant_shorts"] == 10
    assert saas_db.query(RegionTier).count() == 0


@pytest.mark.parametrize("default_currency", ["INR", "USD"])
def test_catalog_shows_rupees_whatever_the_default_currency(client, saas_db, real_catalog, monkeypatch,
                                                            default_currency):
    from saas.config import settings

    monkeypatch.setattr(settings, "default_currency", default_currency)
    body = client.get("/api/billing/catalog?country=US").json()
    assert body["currency"] == "INR"
    shown = {k["slug"]: k["price"]["display"] for k in body["packs"]}
    assert shown == {"pack-s": "₹99", "pack-m": "₹349", "pack-l": "₹1,499"}
    plans = {p["slug"]: (p["prices"]["month"]["display"], p["prices"]["year"]["display"]) for p in body["plans"]}
    assert plans == {"creator": ("₹299", "₹2,990"), "pro": ("₹799", "₹7,990"), "studio": ("₹2,499", "₹24,990")}
    assert "3 x" in body["charge_rule"]


def test_rupee_checkout_and_unpublished_items(client, saas_db, real_catalog):
    from saas.models.billing import Pack, Payment

    make_user(saas_db, "rupee@example.com")
    h = login(client, "rupee@example.com")
    out = client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-s"}, headers=h).json()
    p = saas_db.get(Payment, out["payment_id"])
    assert (p.currency, p.amount_minor, p.credits) == ("INR", 9900, 250) and out["amount"] == "₹99"
    saas_db.query(Pack).filter_by(slug="pack-l").update({"published": False})  # an admin hides one
    saas_db.commit()
    assert "pack-l" not in [k["slug"] for k in client.get("/api/billing/catalog").json()["packs"]]
    assert client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-l"}, headers=h).status_code == 404
