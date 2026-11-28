"""Operator commands for the SaaS database (never the owner's doomscroller.db).

    python scripts/saas_manage.py seed-catalog
    python scripts/saas_manage.py grant-admin owner@gmail.com   # before or after their first Google sign-in
    python scripts/saas_manage.py invite friend@gmail.com --add # print an invite link (no email is sent)
    python scripts/saas_manage.py grant someone@example.com 60 --note "Support goodwill"
    python scripts/saas_manage.py set-plan someone@example.com studio
    python scripts/saas_manage.py promo LAUNCH50 --percent 50 --max 100 --days 30
    python scripts/saas_manage.py promo WELCOME20 --credits 20 --max 500
    python scripts/saas_manage.py bmc-map plan creator --level-id 123456 --bmc-amount 3.25 --bmc-currency USD --url https://buymeacoffee.com/you/membership
    python scripts/saas_manage.py bmc-map pack pack-m --extra-id 98765 --url https://buymeacoffee.com/you/e/98765
    python scripts/saas_manage.py claims                  # Buy Me a Coffee payments not yet matched to an account
    python scripts/saas_manage.py claim CLAIM_ID someone@example.com   # credit one to a verified account
    python scripts/saas_manage.py import-waitlist waitlist-202611270130.csv --dry-run   # the edge /admin/export.csv
    python scripts/saas_manage.py import-waitlist waitlist-202611270130.csv --invite 50 --out invites.csv
    python scripts/saas_manage.py import-waitlist --outstanding --out invites.csv     # re-export unredeemed invites
    python scripts/saas_manage.py init-prod               # persistent SAAS_SECRET_KEY into .env.saas (never printed)
    python scripts/saas_manage.py backup [--keep 14] [--dir D:\\backups]
    python scripts/saas_manage.py mail-test you@example.com   # e.g. Gmail: SAAS_SMTP_HOST=smtp.gmail.com, port 587,
                                                              # SAAS_SMTP_USER/FROM = the address, PASSWORD = an App Password
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


def env_file_path() -> Path | None:
    """The SaaS env file (SAAS_ENV_FILE, default .env.saas), absolute; relative names are repo-relative."""
    name = os.environ.get("SAAS_ENV_FILE", ".env.saas")
    if not name:
        return None
    p = Path(name).expanduser()
    return p if p.is_absolute() else ROOT / p


# saas.config reads SAAS_ENV_FILE relative to the working directory; pin it so `cd` doesn't matter.
if env_file_path() is not None:
    os.environ["SAAS_ENV_FILE"] = str(env_file_path())


def init_prod(path: Path) -> None:
    """Write a new persistent SAAS_SECRET_KEY into the SaaS env file. Never prints it; never overwrites one."""
    import re
    import secrets

    if path.resolve() == (ROOT / ".env").resolve():
        raise SystemExit("refusing to write the owner's .env; point SAAS_ENV_FILE at .env.saas")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if any(re.match(r"\s*SAAS_SECRET_KEY\s*=\s*\S", line) for line in lines):
        raise SystemExit(f"SAAS_SECRET_KEY is already set in {path}; not overwriting it (a new key signs everyone "
                         "out, breaks signed links and makes stored 2FA secrets unreadable).")
    lines = [line for line in lines if not re.match(r"\s*SAAS_SECRET_KEY\s*=", line)]
    lines.append(f"SAAS_SECRET_KEY={secrets.token_urlsafe(48)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(f"Wrote a new SAAS_SECRET_KEY to {path} (not shown). Keep a private copy of this file: losing the key "
          "signs everyone out and makes stored 2FA secrets unreadable. Set SAAS_ENV=prod there too.")


def _user(db, email: str):
    from saas.auth import service
    from saas.models.core import User

    u = db.query(User).filter(User.email == service.normalise_email(email)).one_or_none()
    if u is None:
        raise SystemExit(f"no user {email}")
    return u


def settings_url(path: str) -> str:
    from saas.config import settings

    return settings.public_url.rstrip("/") + path


def _invite_link(db, email: str, *, add: bool) -> str:
    """Issue (or reuse) a waitlist invite for `email` and return the link to send. Never emails anything."""
    from sqlalchemy import or_

    from saas.auth import service
    from saas.common import utcnow
    from saas.config import settings
    from saas.models.waitlist import WaitlistEntry
    from saas.security import disposable
    from saas.waitlist import service as wl

    if settings.launch_mode != "waitlist":
        return f"launch_mode={settings.launch_mode}: no invite needed, anyone can sign up at {settings_url('/signup')}"
    email = service.normalise_email(email)
    canon = disposable.canonical(email)
    entry = (db.query(WaitlistEntry).filter(or_(WaitlistEntry.email == email, WaitlistEntry.email_canonical == canon))
             .order_by(WaitlistEntry.seq).first())
    if entry is None:
        if not add:
            raise SystemExit(f"{email} is not on the waitlist; pass --add to add and invite them")
        now = utcnow()
        entry = WaitlistEntry(email=email, email_canonical=canon, status="confirmed", seq=wl._next_seq(db),
                              referral_code=wl._new_referral_code(db), confirmed_at=now, created_at=now,
                              updated_at=now, notes="added by saas_manage.py invite")
        entry.rank_key = wl._rank_key(entry)
        db.add(entry)
        db.flush()
    if entry.status == "removed":
        raise SystemExit(f"{email} was removed from the waitlist; restore them in the admin panel first")
    if entry.status == "joined":
        return f"{email} already has an account: they sign in at {settings_url('/login')}"
    if entry.status != "invited" or (entry.invite_expires_at and entry.invite_expires_at < utcnow()):
        wl.invite(db, entry, send=False)
    return (f"{wl.invite_url(entry)}\n(for {entry.email}; it works only with the Google account for that address, "
            f"until {entry.invite_expires_at:%Y-%m-%d})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SaaS operator commands")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed-catalog")
    for name in ("grant-admin", "make-admin"):  # make-admin: the old name, same command
        p = sub.add_parser(name, help="make this (Google) email an admin, even before its first sign-in")
        p.add_argument("email")
    p = sub.add_parser("invite", help="print a waitlist invite link for this email (nothing is emailed)")
    p.add_argument("email")
    p.add_argument("--add", action="store_true", help="put them on the waitlist first if they aren't on it")
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
    p.add_argument("--url"); p.add_argument("--currency", default=None)  # noqa: E702  (default: SAAS_DEFAULT_CURRENCY)
    p.add_argument("--bmc-amount", help="what the BMC page charges, in major units, e.g. 3.25")
    p.add_argument("--bmc-currency", help="the BMC page's currency, e.g. USD (BMC accounts default to USD)")
    sub.add_parser("claims")
    p = sub.add_parser("claim"); p.add_argument("claim_id"); p.add_argument("email")  # noqa: E702
    p = sub.add_parser("import-waitlist", help="upsert the edge waitlist export; optionally issue invites")
    p.add_argument("csv", nargs="?", help="the CSV from the edge /admin/export.csv")
    p.add_argument("--invite", type=int, default=0, help="invite the top N un-invited people by position")
    p.add_argument("--include-flagged", action="store_true", help="also import entries the edge flagged")
    p.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    p.add_argument("--outstanding", action="store_true", help="export every issued, unredeemed invite")
    p.add_argument("--out", help="write the invites CSV here instead of printing it")
    sub.add_parser("init-prod", help="generate a persistent SAAS_SECRET_KEY into the SaaS env file")
    p = sub.add_parser("backup", help="SQLite online backup of the SaaS database")
    p.add_argument("--keep", type=int, default=None); p.add_argument("--dir")  # noqa: E702
    p = sub.add_parser("mail-test", help="send one test email with the configured SMTP settings")
    p.add_argument("to")
    args = ap.parse_args(argv)

    if args.cmd == "init-prod":
        path = env_file_path()
        if path is None:
            raise SystemExit("SAAS_ENV_FILE is empty; set it to the env file to write (default .env.saas)")
        init_prod(path)
        return 0
    if args.cmd == "backup":
        from saas.jobs import backup

        try:
            out = backup.run_backup(dest=Path(args.dir) if args.dir else None, keep=args.keep)
        except backup.BackupError as exc:
            raise SystemExit(str(exc))
        print(f"backup written: {out}")
        return 0
    if args.cmd == "mail-test":
        from saas.auth import mailer
        from saas.config import settings

        ok, detail = mailer.send_test(args.to)
        print(f"{'OK' if ok else 'FAILED'}: {detail} (from {settings.smtp_from or settings.support_email})")
        return 0 if ok else 1

    from saas.billing import catalog, ledger
    from saas.common import utcnow
    from saas.db import init_db, session_scope

    init_db()
    with session_scope() as db:
        if args.cmd == "seed-catalog":
            print(catalog.seed_catalog(db))
        elif args.cmd in ("grant-admin", "make-admin"):
            from saas.auth import policy, service

            try:
                u, created = service.provision_admin(db, args.email)
            except ValueError as exc:
                raise SystemExit(str(exc))
            how = "Continue with Google" if policy.google_only() else "sign in (Google, or email and password)"
            print(f"{u.email} is an admin{' (no account yet: it is claimed at their first Google sign-in)' if created else ''}.")
            print(f"To open the admin panel: {how} as {u.email} at {settings_url('/login')}, turn on two-factor in "
                  f"Settings ({settings_url('/app/settings')}), then open {settings_url('/admin')}. Each later "
                  "sign-in asks for the 2FA code.")
        elif args.cmd == "invite":
            print(_invite_link(db, args.email, add=args.add))
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
            from saas.config import settings
            from saas.models.billing import Price

            interval = args.interval if args.kind == "plan" else "once"
            row = (db.query(Price).filter_by(item_kind=args.kind, item_slug=args.slug, interval=interval,
                                             currency=(args.currency or settings.default_currency).upper(),
                                             tier="default").one_or_none())
            if row is None:
                raise SystemExit("no matching default-tier price; seed the catalog or add the price first")
            refs = dict(row.provider_refs or {})
            if bool(args.bmc_amount) != bool(args.bmc_currency):
                raise SystemExit("give --bmc-amount and --bmc-currency together")
            if args.bmc_amount:
                from saas.billing.bmc import to_minor

                refs["bmc_amount_minor"] = to_minor(args.bmc_amount, args.bmc_currency)
                refs["bmc_currency"] = args.bmc_currency.upper()
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
        elif args.cmd == "import-waitlist":
            from saas.auth import waitlist_bridge as bridge

            if not args.csv and not args.outstanding:
                raise SystemExit("give the export CSV, or --outstanding")
            if args.csv:
                stats = bridge.import_rows(db, bridge.read_export(args.csv), include_flagged=args.include_flagged)
                print("import:", stats, file=sys.stderr)
            invites = bridge.issue_invites(db, args.invite) if args.invite else []
            if args.outstanding:
                invites = bridge.outstanding_invites(db)
            if args.dry_run:
                db.rollback()
                print(f"dry run: nothing written; would invite {len(invites)}: "
                      + ", ".join(r["email"] for r in invites), file=sys.stderr)
            elif invites or args.out:
                text = bridge.invites_csv(invites)
                if args.out:
                    Path(args.out).write_text(text, encoding="utf-8", newline="")
                    print(f"{len(invites)} invites written to {args.out}", file=sys.stderr)
                else:
                    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
