"""The credit ledger. 1 credit = 1 minute of source video.

Invariants
* Append-only: `credit_ledger` rows are inserted, never changed. `credit_accounts.balance` is the
  running total, and a CHECK keeps it >= 0.
* Never negative under concurrency: every debit is one guarded statement,
  `UPDATE credit_accounts SET balance = balance - :n WHERE user_id = :u AND balance >= :n`, and the
  rowcount decides. That UPDATE row-locks on Postgres and takes SQLite's write lock, so the follow-up
  reads in the same transaction see a stable row.
* Idempotent: every entry has a unique `idempotency_key`. Applying a key twice returns None the second
  time; if two transactions race on the same key, the loser's INSERT raises IntegrityError and its
  whole transaction (balance change included) rolls back. Callers treat that as "already applied".
* One finalisation per reservation: settle and release share the key `job:<id>:final`, so a double
  settle, a settle after a release, or a recovery release after a settle are all no-ops.
* Functions flush, never commit. The caller commits, so a webhook's event row, its order transition
  and its grant are one transaction.

Buckets: `sub_balance` is the part of the balance that came from subscription grants and is subject to
the rollover cap; it is spent first. Everything else (packs, free, promo, referral) never expires.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from ..common import utcnow
from ..models.billing import CreditAccount, CreditEntry

GRANT_KINDS = {"grant", "purchase", "subscription_grant", "promo", "referral", "admin_adjust"}
DEBIT_KINDS = {"refund", "expire", "admin_adjust"}


class InsufficientCredits(Exception):
    def __init__(self, needed: int, available: int):
        super().__init__(f"needs {needed} credits, has {available}")
        self.needed = needed
        self.available = available


def ensure_account(db: Session, user_id: str) -> None:
    dialect = db.get_bind().dialect.name
    values = {"user_id": user_id, "balance": 0, "sub_balance": 0, "updated_at": utcnow()}
    if dialect == "postgresql":
        stmt = postgresql.insert(CreditAccount).values(**values).on_conflict_do_nothing(index_elements=["user_id"])
    elif dialect == "sqlite":
        stmt = sqlite.insert(CreditAccount).values(**values).on_conflict_do_nothing(index_elements=["user_id"])
    else:  # pragma: no cover - other backends
        if db.get(CreditAccount, user_id) is None:
            db.add(CreditAccount(**values))
            db.flush()
        return
    db.execute(stmt)


def balance(db: Session, user_id: str) -> int:
    row = db.execute(select(CreditAccount.balance).where(CreditAccount.user_id == user_id)).first()
    return int(row[0]) if row else 0


def _amounts(db: Session, user_id: str) -> tuple[int, int]:
    row = db.execute(select(CreditAccount.balance, CreditAccount.sub_balance)
                     .where(CreditAccount.user_id == user_id)).first()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def _applied(db: Session, key: str) -> Optional[CreditEntry]:
    return db.execute(select(CreditEntry).where(CreditEntry.idempotency_key == key)).scalar_one_or_none()


def _insert(db: Session, user_id: str, *, kind: str, amount: int, key: str, job_id: Optional[str] = None,
            payment_id: Optional[str] = None, actor_id: Optional[str] = None, note: str = "",
            meta: Optional[dict] = None) -> CreditEntry:
    bal, _sub = _amounts(db, user_id)
    entry = CreditEntry(user_id=user_id, kind=kind, amount=amount, balance_after=bal, idempotency_key=key,
                        job_id=job_id, payment_id=payment_id, actor_id=actor_id, note=note[:300], meta=meta or {},
                        created_at=utcnow())
    db.add(entry)
    db.flush()  # a duplicate key raises IntegrityError here; the caller's transaction rolls back
    return entry


def grant(db: Session, user_id: str, amount: int, *, kind: str, idempotency_key: str, note: str = "",
          actor_id: Optional[str] = None, payment_id: Optional[str] = None, bucket: str = "other",
          meta: Optional[dict] = None) -> Optional[CreditEntry]:
    """Add credits. Returns None if this key was already applied."""
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    if kind not in GRANT_KINDS:
        raise ValueError(f"not a grant kind: {kind}")
    if _applied(db, idempotency_key) is not None:
        return None
    ensure_account(db, user_id)
    sub_add = amount if bucket == "sub" else 0
    db.execute(update(CreditAccount).where(CreditAccount.user_id == user_id)
               .values(balance=CreditAccount.balance + amount, sub_balance=CreditAccount.sub_balance + sub_add,
                       updated_at=utcnow()))
    return _insert(db, user_id, kind=kind, amount=amount, key=idempotency_key, actor_id=actor_id,
                   payment_id=payment_id, note=note, meta={**(meta or {}), "bucket": bucket})


def _debit_rows(db: Session, user_id: str, amount: int, *, sub_first: bool) -> Optional[int]:
    """Guarded debit; returns how much came out of the subscription bucket, or None if short."""
    res = db.execute(update(CreditAccount)
                     .where(CreditAccount.user_id == user_id, CreditAccount.balance >= amount)
                     .values(balance=CreditAccount.balance - amount, updated_at=utcnow()))
    if res.rowcount != 1:
        return None
    # The row is now locked by this transaction; keep 0 <= sub_balance <= balance.
    bal, sub = _amounts(db, user_id)
    if sub_first:
        from_sub = min(sub, amount)
        new_sub = sub - from_sub
    else:
        new_sub = min(sub, bal)
        from_sub = sub - new_sub
    if new_sub > bal:
        new_sub = bal
    if new_sub != sub:
        db.execute(update(CreditAccount).where(CreditAccount.user_id == user_id).values(sub_balance=new_sub))
    return from_sub


def debit(db: Session, user_id: str, amount: int, *, kind: str, idempotency_key: str, note: str = "",
          actor_id: Optional[str] = None, payment_id: Optional[str] = None, clamp: bool = True,
          meta: Optional[dict] = None) -> Optional[CreditEntry]:
    """Take credits back (refund reversal, expiry, negative admin adjustment).

    With clamp=True the debit stops at the current balance and records the shortfall in meta; the
    balance never goes negative. Returns None if this key was already applied.
    """
    if amount <= 0:
        raise ValueError("debit amount must be positive")
    if kind not in DEBIT_KINDS:
        raise ValueError(f"not a debit kind: {kind}")
    if _applied(db, idempotency_key) is not None:
        return None
    ensure_account(db, user_id)
    # Lock the row first (no-op update) so the read below is stable.
    db.execute(update(CreditAccount).where(CreditAccount.user_id == user_id)
               .values(updated_at=utcnow()))
    bal, _ = _amounts(db, user_id)
    take = min(amount, bal) if clamp else amount
    if take > bal:
        raise InsufficientCredits(amount, bal)
    shortfall = amount - take
    from_sub = 0
    if take > 0:
        got = _debit_rows(db, user_id, take, sub_first=(kind == "expire"))
        if got is None:  # cannot happen while we hold the row lock
            raise InsufficientCredits(take, bal)
        from_sub = got
    return _insert(db, user_id, kind=kind, amount=-take, key=idempotency_key, actor_id=actor_id,
                   payment_id=payment_id, note=note,
                   meta={**(meta or {}), "requested": amount, "shortfall": shortfall, "sub": from_sub})


def reserve(db: Session, user_id: str, job_id: str, amount: int) -> CreditEntry:
    """Hold credits for a job. Raises InsufficientCredits. Idempotent per job."""
    if amount <= 0:
        raise ValueError("reserve amount must be positive")
    key = f"job:{job_id}:reserve"
    existing = _applied(db, key)
    if existing is not None:
        return existing
    ensure_account(db, user_id)
    from_sub = _debit_rows(db, user_id, amount, sub_first=True)
    if from_sub is None:
        raise InsufficientCredits(amount, balance(db, user_id))
    return _insert(db, user_id, kind="reserve", amount=-amount, key=key, job_id=job_id,
                   note="Reserved for a clip job", meta={"sub": from_sub})


def _finalise(db: Session, job_id: str, charged: Optional[int], *, note: str) -> Optional[CreditEntry]:
    key = f"job:{job_id}:final"
    if _applied(db, key) is not None:
        return None
    res = _applied(db, f"job:{job_id}:reserve")
    if res is None:
        return None
    reserved = -int(res.amount)
    from_sub = int((res.meta or {}).get("sub", 0))
    charge = 0 if charged is None else max(0, min(int(charged), reserved))
    refund = reserved - charge
    # Consumption takes subscription credits first, so the unused remainder is the non-sub part first.
    refund_other = min(refund, reserved - from_sub)
    refund_sub = refund - refund_other
    if refund > 0:
        db.execute(update(CreditAccount).where(CreditAccount.user_id == res.user_id)
                   .values(balance=CreditAccount.balance + refund,
                           sub_balance=CreditAccount.sub_balance + refund_sub, updated_at=utcnow()))
    else:
        db.execute(update(CreditAccount).where(CreditAccount.user_id == res.user_id).values(updated_at=utcnow()))
    return _insert(db, res.user_id, kind="settle" if charged is not None else "release", amount=refund, key=key,
                   job_id=job_id, note=note, meta={"reserved": reserved, "charged": charge})


def settle(db: Session, job_id: str, actual: int) -> Optional[CreditEntry]:
    """Charge `actual` (capped at the reservation) and return the rest. No-op once finalised."""
    return _finalise(db, job_id, actual, note="Job finished")


def release(db: Session, job_id: str, note: str = "Job did not run to completion") -> Optional[CreditEntry]:
    """Return the whole reservation. No-op once finalised."""
    return _finalise(db, job_id, None, note=note)


def is_finalised(db: Session, job_id: str) -> bool:
    return _applied(db, f"job:{job_id}:final") is not None


def expire_subscription_excess(db: Session, user_id: str, cap: int, *, idempotency_key: str) -> Optional[CreditEntry]:
    """Trim subscription credits above the rollover cap before a new period's grant."""
    _bal, sub = _amounts(db, user_id)
    excess = sub - max(0, cap)
    if excess <= 0:
        return None
    return debit(db, user_id, excess, kind="expire", idempotency_key=idempotency_key, clamp=True,
                 note="Unused subscription credits above the rollover cap expired")


def entries(db: Session, user_id: str, *, limit: int = 50, before: Optional[str] = None) -> list[CreditEntry]:
    q = select(CreditEntry).where(CreditEntry.user_id == user_id)
    if before:
        anchor = db.execute(select(CreditEntry.created_at)
                            .where(CreditEntry.id == before, CreditEntry.user_id == user_id)).first()
        if anchor:
            q = q.where(CreditEntry.created_at < anchor[0])
    q = q.order_by(CreditEntry.created_at.desc()).limit(max(1, min(limit, 200)))
    return list(db.execute(q).scalars())


def public_entry(e: CreditEntry) -> dict:
    return {"id": e.id, "kind": e.kind, "amount": e.amount, "balance_after": e.balance_after, "note": e.note,
            "job_id": e.job_id, "created_at": e.created_at.isoformat()}


def audit(db: Session, user_id: str) -> dict:
    """Ledger sum vs account balance (admin consistency check)."""
    total = db.execute(select(func.coalesce(func.sum(CreditEntry.amount), 0))
                       .where(CreditEntry.user_id == user_id)).scalar_one()
    return {"ledger_sum": int(total), "balance": balance(db, user_id)}

