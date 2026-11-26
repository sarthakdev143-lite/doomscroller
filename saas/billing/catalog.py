"""Plans, packs and prices: DB rows seeded once from seed_catalog.json, editable afterwards (admin).

Prices resolve per (currency, tier). A country maps to a (currency, tier) through
`billing_region_tiers` (IN -> INR/in, PPP countries -> USD/ppp_low); every other country pays the
default currency at the default tier. A missing regional price falls back to the default tier.

The tier a customer is *charged* comes from the server-side country (Cloudflare header / GeoIP),
never from a query parameter: `?country=` on the public catalog only changes what is displayed.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models.billing import Pack, Plan, Price, RegionTier

log = logging.getLogger("saas.billing.catalog")

SEED_PATH = Path(__file__).with_name("seed_catalog.json")
_SYMBOLS = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}
_ZERO_DECIMAL = {"JPY", "KRW", "VND"}
_seed_lock = threading.Lock()
DEFAULT_FEATURES: dict[str, Any] = {"api": False, "mcp": False, "priority": 0, "watermark": True,
                                    "max_source_minutes": 30, "concurrency": 1, "daily_jobs": 3, "max_clips": 3,
                                    "max_upload_mb": 500}


def seed_catalog(db: Session, path: Path = SEED_PATH) -> dict:
    """Insert seed rows that are missing (by natural key). Never overwrites an existing row."""
    data = json.loads(path.read_text(encoding="utf-8"))
    added = {"plans": 0, "packs": 0, "prices": 0, "region_tiers": 0}
    have_plans = {p.slug for p in db.query(Plan.slug)}
    for p in data.get("plans", []):
        if p["slug"] not in have_plans:
            db.add(Plan(slug=p["slug"], name=p["name"], description=p.get("description", ""),
                        monthly_credits=int(p.get("monthly_credits", 0)), rollover_months=int(p.get("rollover_months", 1)),
                        features=p.get("features", {}), sort=int(p.get("sort", 0)), public=bool(p.get("public", True))))
            added["plans"] += 1
    have_packs = {p.slug for p in db.query(Pack.slug)}
    for p in data.get("packs", []):
        if p["slug"] not in have_packs:
            db.add(Pack(slug=p["slug"], name=p["name"], credits=int(p["credits"]), sort=int(p.get("sort", 0)),
                        description=p.get("description", "")))
            added["packs"] += 1
    have_prices = {(r.item_kind, r.item_slug, r.interval, r.currency, r.tier) for r in
                   db.query(Price.item_kind, Price.item_slug, Price.interval, Price.currency, Price.tier)}
    for p in data.get("prices", []):
        k = (p["item_kind"], p["item_slug"], p["interval"], p["currency"].upper(), p.get("tier", "default"))
        if k not in have_prices:
            db.add(Price(item_kind=k[0], item_slug=k[1], interval=k[2], currency=k[3], tier=k[4],
                         amount_minor=int(p["amount_minor"]), provider_refs=p.get("provider_refs", {})))
            added["prices"] += 1
    have_regions = {r.country for r in db.query(RegionTier.country)}
    for r in data.get("region_tiers", []):
        if r["country"].upper() not in have_regions:
            db.add(RegionTier(country=r["country"].upper(), currency=r["currency"].upper(), tier=r.get("tier", "default"),
                              note=r.get("note", "")))
            added["region_tiers"] += 1
    db.flush()
    return added


def ensure_seeded(db: Session) -> None:
    if db.query(Plan.id).first() is not None:
        return
    with _seed_lock:
        if db.query(Plan.id).first() is None:
            seed_catalog(db)
            db.commit()


def region_for(db: Session, country: Optional[str]) -> tuple[str, str]:
    cc = (country or "").strip().upper()
    if len(cc) == 2:
        row = db.get(RegionTier, cc)
        if row is not None:
            return row.currency.upper(), row.tier
    return settings.default_currency.upper(), "default"


def price_for(db: Session, kind: str, slug: str, interval: str, country: Optional[str]) -> Optional[Price]:
    currency, tier = region_for(db, country)
    base = db.query(Price).filter(Price.item_kind == kind, Price.item_slug == slug, Price.interval == interval,
                                  Price.active.is_(True))
    for cur, tr in ((currency, tier), (currency, "default"), (settings.default_currency.upper(), "default")):
        row = base.filter(Price.currency == cur, Price.tier == tr).first()
        if row is not None:
            return row
    return None


def format_amount(amount_minor: int, currency: str) -> str:
    cur = currency.upper()
    symbol = _SYMBOLS.get(cur, cur + " ")
    if cur in _ZERO_DECIMAL:
        return f"{symbol}{amount_minor:,}"
    major, minor = divmod(int(amount_minor), 100)
    return f"{symbol}{major:,}" if minor == 0 else f"{symbol}{major:,}.{minor:02d}"


def _price_view(p: Optional[Price]) -> Optional[dict]:
    if p is None:
        return None
    return {"amount_minor": p.amount_minor, "currency": p.currency, "display": format_amount(p.amount_minor, p.currency),
            "tier": p.tier}


def get_plan(db: Session, slug: str) -> Optional[Plan]:
    return db.query(Plan).filter(Plan.slug == slug).one_or_none()


def get_pack(db: Session, slug: str) -> Optional[Pack]:
    return db.query(Pack).filter(Pack.slug == slug).one_or_none()


def plan_for_user(db: Session, user) -> Optional[Plan]:
    ensure_seeded(db)
    plan = get_plan(db, user.plan_slug or "free")
    return plan if plan is not None else get_plan(db, "free")


def features_for(db: Session, user) -> dict:
    plan = plan_for_user(db, user)
    feats = {**DEFAULT_FEATURES, **((plan.features or {}) if plan else {})}
    # A customer who has paid for credits is not on the free tier, even without a subscription.
    if user.first_paid_at is not None and (plan is None or plan.slug == "free"):
        feats["watermark"] = False
        feats["max_source_minutes"] = max(int(feats.get("max_source_minutes", 30)), 90)
        feats["daily_jobs"] = max(int(feats.get("daily_jobs", 3)), 20)
        feats["max_clips"] = max(int(feats.get("max_clips", 3)), 10)
        feats["max_upload_mb"] = max(int(feats.get("max_upload_mb", 500)), 2048)
    return feats


def public_catalog(db: Session, country: Optional[str]) -> dict:
    ensure_seeded(db)
    currency, tier = region_for(db, country)
    plans = []
    for p in db.query(Plan).filter(Plan.active.is_(True)).order_by(Plan.sort, Plan.slug):
        if not p.public:
            continue
        month = price_for(db, "plan", p.slug, "month", country)
        year = price_for(db, "plan", p.slug, "year", country)
        if month is None and year is None:
            continue
        plans.append({"slug": p.slug, "name": p.name, "description": p.description,
                      "monthly_credits": p.monthly_credits, "rollover_months": p.rollover_months,
                      "features": {**DEFAULT_FEATURES, **(p.features or {})},
                      "prices": {"month": _price_view(month), "year": _price_view(year)}})
    packs = []
    for k in db.query(Pack).filter(Pack.active.is_(True)).order_by(Pack.sort, Pack.slug):
        pr = price_for(db, "pack", k.slug, "once", country)
        if pr is None:
            continue
        packs.append({"slug": k.slug, "name": k.name, "description": k.description, "credits": k.credits,
                      "price": _price_view(pr),
                      "per_credit_minor": round(pr.amount_minor / max(1, k.credits), 2)})
    free = get_plan(db, "free")
    return {"currency": currency, "tier": tier, "country": (country or "").upper() or None,
            "credit_unit": "1 credit = 1 minute of source video (rounded up per job)",
            "free": {"credits": settings.signup_free_credits,
                     "features": {**DEFAULT_FEATURES, **((free.features or {}) if free else {})}},
            "plans": plans, "packs": packs}
