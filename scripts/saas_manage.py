"""Operator commands for the SaaS database (never the owner's doomscroller.db).

    python scripts/saas_manage.py seed-catalog
    python scripts/saas_manage.py make-admin owner@example.com
    python scripts/saas_manage.py grant someone@example.com 60 --note "Support goodwill"
    python scripts/saas_manage.py set-plan someone@example.com studio
    python scripts/saas_manage.py promo LAUNCH50 --percent 50 --max 100 --days 30
    python scripts/saas_manage.py promo WELCOME20 --credits 20 --max 500
    python scripts/saas_manage.py bmc-map plan starter --level-id 123456 --url https://buymeacoffee.com/you/membership
    python scripts/saas_manage.py bmc-map pack pack-300 --extra-id 98765 --url https://buymeacoffee.com/you/e/98765
    python scripts/saas_manage.py claims                  # Buy Me a Coffee payments not yet matched to an account
    python scripts/saas_manage.py claim CLAIM_ID someone@example.com   # credit one to a verified account
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["DOOMSCROLLER_ENV_FILE"] = ""
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _user(db, email: str):
    from saas.auth import service
    from saas.models.core import User

    u = db.query(User).filter(User.email == service.normalise_email(email)).one_or_none()
    if u is None:
        raise SystemExit(f"no user {email}")
    return u


def main() -> int:
    ap = argparse.ArgumentParser(description="SaaS operator commands")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed-catalog")
    p = sub.add_parser("make-admin"); p.add_argument("email")  # noqa: E702
    p = sub.add_parser("grant"); p.add_argument("email"); p.add_argument("minutes", type=int)  # noqa: E702
    p.add_argument("--note", default="Admin grant")
    p = sub.add_parser("set-plan"); p.add_argument("email"); p.add_argument("slug")  # noqa: E702
    p = sub.add_parser("promo"); p.add_argument("code")  # noqa: E702
    p.add_argument("--credits", type=int, default=0); p.add_argument("--percent", type=int, default=0)  # noqa: E702
    p.add_argument("--max", type=int, default=None); p.add_argument("--days", type=int, default=None)  # noqa: E702
    p.add_argument("--first-purchase-only", action="store_true")
    p = sub.add_parser("bmc-map"); p.add_argument("kind", choices=["plan", "pack"]); p.add_argument("slug")  # noqa: E702
    p.add_argument("--interval", choices=["month", "year"], default="month")
    p.add_argument("--level-id"); p.add_argument("--level-name"); p.add_argument("--extra-id")  # noqa: E702
    p.add_argument("--url"); p.add_argument("--currency", default="USD")  # noqa: E702
    sub.add_parser("claims")
    p = sub.add_parser("claim"); p.add_argument("claim_id"); p.add_argument("email")  # noqa: E702
    args = ap.parse_args()

    from saas.billing import catalog, ledger
    from saas.common import utcnow
    from saas.db import init_db, session_scope

    init_db()
    with session_scope() as db:
        if args.cmd == "seed-catalog":
            print(catalog.seed_catalog(db))
        elif args.cmd == "make-admin":
            u = _user(db, args.email)
            u.role = "admin"
            print(f"{u.email} is now an admin; they must enable 2FA before the admin panel opens.")
        elif args.cmd == "grant":
            u = _user(db, args.email)
            if args.minutes > 0:
                ledger.grant(db, u.id, args.minutes, kind="admin_adjust", note=args.note,
                             idempotency_key=f"cli:{uuid.uuid4().hex}")
            elif args.minutes < 0:
                ledger.debit(db, u.id, -args.minutes, kind="admin_adjust", note=args.note,
                             idempotency_key=f"cli:{uuid.uuid4().hex}")
            print(f"{u.email}: balance {ledger.balance(db, u.id)}")
        elif args.cmd == "set-plan":
            u = _user(db, args.email)
            if catalog.get_plan(db, args.slug) is None:
                raise SystemExit(f"no plan {args.slug}")
            u.plan_slug = args.slug
            print(f"{u.email}: plan {args.slug}")
        elif args.cmd == "promo":
            from saas.models.billing import PromoCode

            if bool(args.credits) == bool(args.percent):
                raise SystemExit("give exactly one of --credits or --percent")
            db.add(PromoCode(code=args.code.upper(), kind="credits" if args.credits else "percent_off",
                             credits=args.credits, percent_off=args.percent, max_redemptions=args.max,
                             applies_to="any" if args.credits else "pack",
                             first_purchase_only=args.first_purchase_only,
                             expires_at=utcnow() + timedelta(days=args.days) if args.days else None))
            print(f"promo {args.code.upper()} created")
        elif args.cmd == "bmc-map":
            from saas.models.billing import Price

            interval = args.interval if args.kind == "plan" else "once"
            row = (db.query(Price).filter_by(item_kind=args.kind, item_slug=args.slug, interval=interval,
                                             currency=args.currency.upper(), tier="default").one_or_none())
            if row is None:
                raise SystemExit("no matching default-tier price; seed the catalog or add the price first")
            refs = dict(row.provider_refs or {})
            for k, v in (("bmc_level_id", args.level_id), ("bmc_level_name", args.level_name),
                         ("bmc_extra_id", args.extra_id), ("bmc_url", args.url)):
                if v:
                    refs[k] = v
            row.provider_refs = refs
            print(f"{args.kind} {args.slug}: {refs}")
        elif args.cmd == "claims":
            from saas.models.billing import PendingClaim

            for c in db.query(PendingClaim).filter(PendingClaim.status.in_(("pending", "unmatched"))) \
                    .order_by(PendingClaim.created_at):
                print(f"{c.id}  {c.status:9}  {c.event_type:24}  {c.email}  {c.reason}  {c.created_at:%Y-%m-%d}")
        elif args.cmd == "claim":
            from saas.billing import bmc
            from saas.models.billing import PendingClaim

            c = db.get(PendingClaim, args.claim_id)
            u = _user(db, args.email)
            if c is None or c.status not in ("pending", "unmatched") or u.email_verified_at is None:
                raise SystemExit("claim not open, or that account is not verified")
            result, detail = bmc.handle(db, c.event_type, dict(c.payload or {}), user=u)
            if result in ("processed", "duplicate"):
                c.status, c.claimed_by, c.claimed_at = "claimed", u.id, utcnow()
            print(result, detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
