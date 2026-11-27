"""Bridge the live waitlist (Cloudflare D1, edge/) into the app's waitlist table and issue invites.

The public waitlist runs at the edge; app signup (launch_mode=waitlist) redeems invites against the
app's own `waitlist_entries` (saas.waitlist). The owner exports `/admin/export.csv` from the edge and runs

    python scripts/saas_manage.py import-waitlist waitlist-YYYYMMDDHHMM.csv [--invite N] [--dry-run]

* Input is exactly what edge/functions/_lib/admin.js `toCsv` writes: UTF-8 with a BOM, CRLF, RFC 4180
  quoting, and `safeCell`'s formula escaping (a leading apostrophe on text starting with = + - @ tab CR
  | %, or with optional spaces then = + @). `unescape_cell` undoes exactly that.
* Upsert by normalised email. Kept: join order (`seq`), position (`rank_key` rebuilt with the same
  formula both sides use: seq - waitlist_referral_bump x referrals_counted), referral code and counts,
  who referred whom, joined_at (`created_at`), country, role, channel, first-touch source, risk reasons.
* Flagged rows are skipped unless include_flagged; an already-imported entry that the edge later flags is
  marked flagged (held from invites). Edge `removed` removes, and voids an unused invite; an account
  already created from an invite (`joined`) is never touched, and re-imports never undo issued invites.
* Idempotent: re-importing the same file changes nothing. `issue_invites(n)` invites the top n
  un-invited, unflagged, waiting entries by position and returns rows to mail (no email is sent).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from ..config import settings

REQUIRED = ("seq", "email", "status", "created_at")
EDGE_COLUMNS = ("seq", "id", "email", "role", "channel_url", "status", "created_at", "referral_code",
                "referrals_counted", "referred_by_seq", "referral_status", "referral_reason", "country", "region",
                "city", "asn", "as_org", "referrer_host", "landing_path", "utm_source", "utm_medium", "utm_campaign",
                "utm_term", "utm_content", "turnstile", "flagged", "risk")
_FORMULA_A = re.compile(r"^[=+\-@\t\r|%]")
_FORMULA_B = re.compile(r"^\s*[=+@]")
_SOURCE_KEYS = ("referrer_host", "landing_path", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")


def unescape_cell(value: str) -> str:
    """Undo admin.js safeCell: it prefixes "'" only when the rest would trigger a formula."""
    if value.startswith("'") and (_FORMULA_A.match(value[1:]) or _FORMULA_B.match(value[1:])):
        return value[1:]
    return value


def read_export(path: str | Path) -> list[dict[str, str]]:
    text = Path(path).read_bytes().decode("utf-8-sig")  # -sig drops the BOM
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise ValueError(f"not a waitlist export: missing column(s) {', '.join(missing)}")
    reader.fieldnames = header
    return [{k: unescape_cell(v or "") for k, v in row.items() if k} for row in reader]


def _int(v: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _ts(v: str) -> Optional[datetime]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clip(v: str, n: int) -> Optional[str]:
    v = (v or "").strip()
    return v[:n] if v else None


def import_rows(db: Session, rows: Iterable[dict[str, str]], *, include_flagged: bool = False) -> dict:
    """Upsert edge rows into waitlist_entries. Flushes; the caller commits (or rolls back for a dry run)."""
    from ..models.waitlist import WaitlistEntry
    from ..security import disposable
    from ..waitlist import service as wl
    from . import service as auth_service
    from .errors import ApiError

    bump = int(settings.waitlist_referral_bump)
    stats = {"rows": 0, "created": 0, "updated": 0, "unchanged": 0, "skipped_flagged": 0, "skipped_invalid": 0}
    by_seq: dict[int, Any] = {}
    referrals: list[tuple[Any, int]] = []

    for row in rows:
        stats["rows"] += 1
        try:
            email = auth_service.normalise_email(row.get("email", ""))
        except ApiError:
            stats["skipped_invalid"] += 1
            continue
        seq = _int(row.get("seq"))
        if seq is None or seq < 1:
            stats["skipped_invalid"] += 1
            continue
        flagged = (row.get("flagged") or "").strip() in ("1", "true", "True")
        entry = db.query(WaitlistEntry).filter(WaitlistEntry.email == email).one_or_none()
        if entry is None and flagged and not include_flagged:
            stats["skipped_flagged"] += 1
            continue

        referrals_counted = max(0, _int(row.get("referrals_counted"), 0) or 0)
        created = _ts(row.get("created_at", ""))
        source = {k: row[k].strip() for k in _SOURCE_KEYS if (row.get(k) or "").strip()}
        risk = [r.strip() for r in (row.get("risk") or "").split(",") if r.strip()]
        edge_status = (row.get("status") or "joined").strip().lower()
        fields = {
            "email_canonical": disposable.canonical(email),
            "role": _clip(row.get("role", ""), 64),
            "channel_url": _clip(row.get("channel_url", ""), 500),
            "seq": seq,
            "rank_key": seq - bump * referrals_counted,
            "referrals_counted": referrals_counted,
            "referral_status": _clip(row.get("referral_status", ""), 16),
            "referral_reason": _clip(row.get("referral_reason", ""), 64),
            "country": (_clip(row.get("country", ""), 2) or "").upper() or None,
            "source": source or None,
            "risk_reasons": risk or None,
            "flagged": flagged,
        }
        if created is not None:
            fields["created_at"] = created
        if entry is None:
            code = (row.get("referral_code") or "").strip().upper()
            if not code or len(code) > 16 or db.query(WaitlistEntry.id).filter(WaitlistEntry.referral_code == code).first():
                code = wl._new_referral_code(db)
            edge_id = (row.get("id") or "").strip()
            kwargs: dict = {}
            if edge_id.startswith("wl_") and len(edge_id) <= 40 and db.get(WaitlistEntry, edge_id) is None:
                kwargs["id"] = edge_id
            entry = WaitlistEntry(email=email, referral_code=code, status="confirmed", **kwargs, **fields)
            entry.confirmed_at = fields.get("created_at")
            entry.notes = "imported from the edge waitlist"
            if edge_status == "removed":
                entry.status = "removed"
            db.add(entry)
            db.flush()
            stats["created"] += 1
        else:
            changed = False
            for k, v in fields.items():
                if getattr(entry, k) != v:
                    setattr(entry, k, v)
                    changed = True
            # App-side progress (an issued or redeemed invite) is never undone by a re-import, except that a
            # removal at the edge (the person asked to leave) voids an invite they haven't used yet.
            if entry.status == "invited" and edge_status == "removed":
                entry.status = "removed"
                changed = True
            elif entry.status not in ("invited", "joined"):
                want = "removed" if edge_status == "removed" else "confirmed"
                if entry.status != want:
                    entry.status = want
                    changed = True
                if want == "confirmed" and entry.confirmed_at is None:
                    entry.confirmed_at = entry.created_at
                    changed = True
            stats["updated" if changed else "unchanged"] += 1
        by_seq[seq] = entry
        ref_seq = _int(row.get("referred_by_seq"))
        if ref_seq:
            referrals.append((entry, ref_seq))

    for entry, ref_seq in referrals:
        referrer = by_seq.get(ref_seq) or db.query(WaitlistEntry).filter(WaitlistEntry.seq == ref_seq).first()
        if referrer is not None and referrer.id != entry.id and entry.referred_by_id != referrer.id:
            entry.referred_by_id = referrer.id
    db.flush()
    return stats


def signup_url(code: str) -> str:
    return f"{settings.public_url.rstrip('/')}/signup?invite={code}"


def issue_invites(db: Session, n: int) -> list[dict]:
    """Invite the top `n` un-invited, unflagged, waiting entries by position (no email is sent)."""
    from ..models.waitlist import WaitlistEntry
    from ..waitlist import service as wl

    if n <= 0:
        return []
    picked = (db.query(WaitlistEntry)
              .filter(WaitlistEntry.status.in_(wl.WAITING), WaitlistEntry.flagged.is_(False),
                      WaitlistEntry.invite_code.is_(None))
              .order_by(WaitlistEntry.rank_key, WaitlistEntry.seq).limit(int(n)).all())
    out = []
    for entry in picked:
        wl.invite(db, entry, send=False)
        out.append(invite_row(entry))
    return out


def outstanding_invites(db: Session) -> list[dict]:
    """Every issued, unredeemed, unexpired invite (to re-send a lost list)."""
    from ..common import utcnow
    from ..models.waitlist import WaitlistEntry

    now = utcnow()
    rows = (db.query(WaitlistEntry).filter(WaitlistEntry.status == "invited", WaitlistEntry.invite_code.is_not(None))
            .order_by(WaitlistEntry.rank_key, WaitlistEntry.seq).all())
    return [invite_row(e) for e in rows if e.invite_expires_at is None or e.invite_expires_at > now]


def invite_row(entry) -> dict:
    return {"email": entry.email, "invite_code": entry.invite_code, "signup_url": signup_url(entry.invite_code),
            "expires_at": entry.invite_expires_at.isoformat() if entry.invite_expires_at else ""}


def invites_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["email", "invite_code", "signup_url", "expires_at"])
    for r in rows:
        # Same formula guard as the edge export, since the owner will open this in a spreadsheet.
        w.writerow([("'" + str(v)) if str(v)[:1] in ("=", "+", "-", "@", "\t", "\r", "|", "%") else v
                    for v in (r["email"], r["invite_code"], r["signup_url"], r["expires_at"])])
    return buf.getvalue()
