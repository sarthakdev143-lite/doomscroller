"""Read (and a few admin writes) over the core agent's tables, every access guarded.

The admin panel must keep working while the core agent's models change under it, so nothing here
imports them at module load, and every function returns None / [] when a model, column or helper
is missing rather than raising. Columns used are the ones in docs/saas/CONTRACT.md.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

log = logging.getLogger("saas.admin.core")

# Estimated unit costs, from docs/saas/costs-and-pricing.md (Nov 2026). Override with
# SAAS_COST_RATES_JSON, e.g. {"stt_per_min": {"deepgram": 0.0043}, "render_per_s": 0.000084}.
DEFAULT_RATES = {
    "stt_per_min": {"deepgram": 0.0043, "groq": 0.00067, "sarvam": 0.0052, "default": 0.0043},
    "llm_per_mtok": {"haiku": [0.10, 0.50], "sonnet": [3.0, 15.0], "opus": [15.0, 75.0], "default": [0.10, 0.50]},
    "image_tokens": 1400,
    "render_per_s": 0.000084,
}


def _model(module: str, name: str):
    try:
        mod = __import__(f"saas.models.{module}", fromlist=[name])
        m = getattr(mod, name, None)
        return m if m is not None and hasattr(m, "__table__") else None
    except Exception:
        return None


def User():
    return _model("core", "User")


def Job():
    return _model("jobs", "Job")


def Payment():
    return _model("billing", "Payment")


def CreditEntry():
    return _model("billing", "CreditEntry")


def _has(model, *cols: str) -> bool:
    return model is not None and all(hasattr(model, c) for c in cols)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as exc:
        log.debug("admin core query failed: %s", exc)
        return default


# ---- users -------------------------------------------------------------------------------------
def count_users(db: Session, since: datetime | None = None) -> int | None:
    U = User()
    if not _has(U, "created_at"):
        return None
    q = select(func.count()).select_from(U)
    if since is not None:
        q = q.where(U.created_at >= since)
    return _safe(lambda: int(db.execute(q).scalar_one()))


def count_paid(db: Session, since: datetime | None = None) -> int | None:
    U = User()
    if not _has(U, "first_paid_at"):
        return None
    q = select(func.count()).select_from(U).where(U.first_paid_at.is_not(None))
    if since is not None:
        q = q.where(U.first_paid_at >= since)
    return _safe(lambda: int(db.execute(q).scalar_one()))


def list_users(db: Session, *, q: str = "", status: str = "", plan: str = "", limit: int = 50,
               offset: int = 0) -> list | None:
    U = User()
    if not _has(U, "id", "email"):
        return None
    stmt = select(U)
    if q:
        like = f"%{q.lower()[:100]}%"
        stmt = stmt.where(or_(func.lower(U.email).like(like), U.id == q))
    if status and hasattr(U, "status"):
        stmt = stmt.where(U.status == status)
    if plan and hasattr(U, "plan_slug"):
        stmt = stmt.where(U.plan_slug == plan)
    if hasattr(U, "created_at"):
        stmt = stmt.order_by(U.created_at.desc())
    return _safe(lambda: db.execute(stmt.limit(limit).offset(offset)).scalars().all(), [])


def get_user(db: Session, user_id: str):
    U = User()
    if U is None:
        return None
    return _safe(lambda: db.get(U, user_id))


def users_by_ids(db: Session, ids: list[str]) -> dict:
    U = User()
    if U is None or not ids:
        return {}
    return _safe(lambda: {u.id: u for u in db.execute(select(U).where(U.id.in_(ids))).scalars()}, {})


def balance(db: Session, user_id: str) -> int | None:
    try:
        from ..billing import ledger

        return int(ledger.balance(db, user_id))
    except Exception:
        return None


def balances(db: Session, ids: list[str]) -> dict[str, int]:
    return {i: b for i in ids if (b := balance(db, i)) is not None}


def adjust_credits(db: Session, user_id: str, amount: int, *, note: str, actor_id: str | None,
                   key: str) -> bool:
    """Admin +/- credits through the core ledger (idempotent on `key`)."""
    try:
        from ..billing import ledger
    except Exception:
        return False
    if amount > 0:
        ledger.grant(db, user_id, amount, kind="admin_adjust", idempotency_key=key, note=note, actor_id=actor_id)
    elif amount < 0:
        try:
            ledger.debit(db, user_id, -amount, kind="admin_adjust", idempotency_key=key, note=note,
                         actor_id=actor_id, clamp=True)
        except TypeError:
            ledger.debit(db, user_id, -amount, kind="admin_adjust", idempotency_key=key, note=note)
    return True


def revoke_sessions(db: Session, user_id: str, reason: str) -> int | None:
    try:
        from ..auth import sessions

        return sessions.revoke_all(db, user_id, reason=reason)
    except Exception:
        return None


def user_ledger(db: Session, user_id: str, limit: int = 25) -> list:
    E = CreditEntry()
    if not _has(E, "user_id", "created_at"):
        return []
    return _safe(lambda: db.execute(select(E).where(E.user_id == user_id).order_by(E.created_at.desc())
                                    .limit(limit)).scalars().all(), [])


def user_jobs(db: Session, user_id: str, limit: int = 20) -> list:
    J = Job()
    if not _has(J, "user_id", "created_at"):
        return []
    return _safe(lambda: db.execute(select(J).where(J.user_id == user_id).order_by(J.created_at.desc())
                                    .limit(limit)).scalars().all(), [])


def user_payments(db: Session, user_id: str, limit: int = 20) -> list:
    P = Payment()
    if not _has(P, "user_id", "created_at"):
        return []
    return _safe(lambda: db.execute(select(P).where(P.user_id == user_id).order_by(P.created_at.desc())
                                    .limit(limit)).scalars().all(), [])


def users_sharing(db: Session, *, ip: str | None = None, fingerprint: str | None = None) -> list:
    U = User()
    if U is None:
        return []
    conds = []
    if ip and hasattr(U, "signup_ip"):
        conds.append(U.signup_ip == ip)
    if fingerprint and hasattr(U, "signup_fingerprint"):
        conds.append(U.signup_fingerprint == fingerprint)
    if not conds:
        return []
    return _safe(lambda: db.execute(select(U).where(or_(*conds)).limit(200)).scalars().all(), [])


def signup_clusters(db: Session, limit: int = 40) -> list[dict]:
    """Signup IPs / fingerprints shared by two or more accounts (multi-account rings)."""
    U = User()
    out = []
    for col, kind in (("signup_fingerprint", "fp"), ("signup_ip", "ip")):
        if not _has(U, col, "id"):
            continue
        c = getattr(U, col)
        rows = _safe(lambda c=c: db.execute(select(c, func.count(U.id)).where(c.is_not(None)).group_by(c)
                                            .having(func.count(U.id) > 1).order_by(func.count(U.id).desc())
                                            .limit(limit)).all(), [])
        for value, n in rows:
            out.append({"kind": kind, "value": value, "accounts": int(n)})
    return out


# ---- money & jobs ------------------------------------------------------------------------------
def revenue(db: Session, since: datetime) -> dict[str, int] | None:
    P = Payment()
    if not _has(P, "paid_at", "amount_minor", "currency"):
        return None

    def q():
        rows = db.execute(select(P.currency, func.sum(P.amount_minor), func.count(P.id))
                          .where(P.paid_at.is_not(None), P.paid_at >= since).group_by(P.currency)).all()
        return {cur: {"minor": int(total or 0), "count": int(n)} for cur, total, n in rows}

    return _safe(q)


def credits_flow(db: Session, since: datetime) -> dict | None:
    """Credits sold (purchases + subscription grants), given free, and burned by jobs."""
    E = CreditEntry()
    J = Job()
    out: dict[str, int] = {}
    if _has(E, "kind", "amount", "created_at"):
        rows = _safe(lambda: db.execute(select(E.kind, func.sum(E.amount)).where(E.created_at >= since)
                                        .group_by(E.kind)).all(), [])
        by = {k: int(v or 0) for k, v in rows}
        out["sold"] = by.get("purchase", 0) + by.get("subscription_grant", 0)
        out["free"] = by.get("grant", 0) + by.get("promo", 0) + by.get("referral", 0)
    if _has(J, "credits_charged", "created_at"):
        out["burned"] = _safe(lambda: int(db.execute(select(func.coalesce(func.sum(J.credits_charged), 0))
                                                     .where(J.created_at >= since)).scalar_one()), 0)
    return out or None


def _job_cost(meter: dict | None, rates: dict) -> float | None:
    if not isinstance(meter, dict) or not meter:
        return None
    cost = 0.0
    for key, secs in (meter.get("stt_seconds") or {}).items():
        provider = str(key).split(":", 1)[0].lower()
        per_min = rates["stt_per_min"].get(provider, rates["stt_per_min"]["default"])
        cost += float(secs or 0) / 60 * per_min
    for key, b in (meter.get("llm") or {}).items():
        name = str(key).lower()
        tier = next((t for t in ("opus", "sonnet", "haiku") if t in name), "default")
        pin, pout = rates["llm_per_mtok"].get(tier, rates["llm_per_mtok"]["default"])
        tokens_in = float(b.get("input_tokens") or 0) + float(b.get("images") or 0) * rates["image_tokens"]
        cost += tokens_in / 1e6 * pin + float(b.get("output_tokens") or 0) / 1e6 * pout
    cost += float((meter.get("render") or {}).get("wall_seconds") or 0) * rates["render_per_s"]
    return cost


def rates() -> dict:
    import json

    from ..config import settings

    raw = getattr(settings, "cost_rates_json", "") or ""
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_RATES.items()}
    if raw:
        try:
            for k, v in json.loads(raw).items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
        except (ValueError, AttributeError):
            pass
    return merged


def jobs_summary(db: Session, since: datetime) -> dict | None:
    J = Job()
    if not _has(J, "status", "created_at"):
        return None

    def q():
        rows = db.execute(select(J.status, func.count(J.id)).where(J.created_at >= since).group_by(J.status)).all()
        by = {s: int(n) for s, n in rows}
        done, failed = by.get("done", 0), by.get("failed", 0)
        out: dict[str, Any] = {"by_status": by, "total": sum(by.values()),
                               "success_rate": (done / (done + failed)) if (done + failed) else None}
        if hasattr(J, "meter"):
            r = rates()
            costs, minutes = [], []
            stt = defaultdict(float)
            for meter, dur in db.execute(select(J.meter, getattr(J, "source_duration_s", J.meter))
                                         .where(J.created_at >= since, J.status == "done").limit(5000)).all():
                c = _job_cost(meter, r)
                if c is not None:
                    costs.append(c)
                    if isinstance(dur, (int, float)) and dur:
                        minutes.append(float(dur) / 60)
                for k, v in ((meter or {}).get("stt_seconds") or {}).items():
                    stt[k] += float(v or 0)
            out["avg_cost"] = sum(costs) / len(costs) if costs else None
            out["cost_jobs"] = len(costs)
            out["cost_per_source_min"] = (sum(costs) / sum(minutes)) if minutes and sum(minutes) else None
            out["stt_minutes"] = {k: round(v / 60, 1) for k, v in stt.items()}
        return out

    return _safe(q)
