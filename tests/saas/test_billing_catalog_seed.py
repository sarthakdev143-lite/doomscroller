"""The product seed catalog: the memo's provisional prices, every paid item unpublished until the owner
approves, and unpublished items neither listed nor purchasable."""
from __future__ import annotations

import pytest

from core_fixtures import login, make_user  # noqa: F401

MEMO_USD = {("plan", "creator", "month"): 1000, ("plan", "creator", "year"): 10000,
            ("plan", "pro", "month"): 2700, ("plan", "pro", "year"): 27000,
            ("plan", "studio", "month"): 8900, ("plan", "studio", "year"): 89000,
            ("plan", "pro-byok", "month"): 1300, ("plan", "pro-byok", "year"): 13000,
            ("pack", "pack-s", "once"): 400, ("pack", "pack-m", "once"): 1200, ("pack", "pack-l", "once"): 4900}
MEMO_INR = {("plan", "india-lite", "month"): 29900, ("plan", "creator", "month"): 84900,
            ("plan", "pro", "month"): 249900, ("plan", "studio", "month"): 839900,
            ("plan", "studio", "year"): 8399000}
CREDITS = {"creator": 1000, "pro": 3000, "studio": 10000, "pro-byok": 3000, "india-lite": 300,
           "pack-s": 250, "pack-m": 1000, "pack-l": 5000}


@pytest.fixture()
def real_catalog(saas_db, monkeypatch):
    from saas.auth import compat
    from saas.billing import catalog
    from saas.config import settings

    monkeypatch.setattr(settings, "hibp_enabled", False)
    compat._local.reset()
    from saas.security import limits

    limits.reset_state()
    catalog.ensure_seeded(saas_db)
    return catalog


def test_seed_matches_the_memo_and_is_unpublished(saas_db, real_catalog):
    from saas.models.billing import Pack, Plan, Price

    prices = {(p.item_kind, p.item_slug, p.interval, p.currency): p.amount_minor for p in saas_db.query(Price)}
    for (kind, slug, interval), amount in MEMO_USD.items():
        assert prices[(kind, slug, interval, "USD")] == amount
    for (kind, slug, interval), amount in MEMO_INR.items():
        assert prices[(kind, slug, interval, "INR")] == amount
    plans = {p.slug: p for p in saas_db.query(Plan)}
    packs = {p.slug: p for p in saas_db.query(Pack)}
    for slug, credits in CREDITS.items():
        row = plans.get(slug) or packs[slug]
        assert (row.monthly_credits if slug in plans else row.credits) == credits
        assert row.published is False, f"{slug} must stay unpublished until the owner approves"
    assert plans["free"].features["free_grant_minutes"] == 10 and plans["free"].features["free_grant_shorts"] == 10
    assert plans["studio"].features["api"] and plans["pro-byok"].features["byok"]


def test_unpublished_items_are_hidden_and_unbuyable(client, saas_db, real_catalog):
    from saas.models.billing import Pack

    body = client.get("/api/billing/catalog?country=US").json()
    assert body["plans"] == [] and body["packs"] == []
    assert body["free"]["minutes"] == 10 and body["free"]["shorts"] == 10
    make_user(saas_db, "early@example.com")
    h = login(client, "early@example.com")
    for payload in ({"kind": "pack", "slug": "pack-s"}, {"kind": "plan", "slug": "pro", "interval": "month"}):
        r = client.post("/api/billing/checkout", json=payload, headers=h)
        assert r.status_code == 404, r.text
    # The owner publishes one pack: it shows and can be bought.
    saas_db.query(Pack).filter_by(slug="pack-s").update({"published": True})
    saas_db.commit()
    body = client.get("/api/billing/catalog?country=US").json()
    assert [k["slug"] for k in body["packs"]] == ["pack-s"] and body["packs"][0]["price"]["display"] == "$4"
    r = client.post("/api/billing/checkout", json={"kind": "pack", "slug": "pack-s"}, headers=h)
    assert r.status_code == 200 and r.json()["amount"] == "$4"
