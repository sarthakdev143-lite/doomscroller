"""Credit ledger: idempotency, one finalisation per job, buckets, never negative under concurrency.

Concurrency tests use a temp-file SQLite engine: the shared in-memory engine is a single connection,
so threads on it would prove nothing.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from core_fixtures import core, make_user  # noqa: F401


def test_grant_reserve_settle_release_and_idempotency(core, saas_db):
    from saas.billing import ledger

    u = make_user(saas_db)
    assert ledger.grant(saas_db, u.id, 100, kind="purchase", idempotency_key="p1") is not None
    assert ledger.grant(saas_db, u.id, 100, kind="purchase", idempotency_key="p1") is None  # replay
    ledger.reserve(saas_db, u.id, "job_a", 40)
    ledger.reserve(saas_db, u.id, "job_a", 40)  # idempotent per job
    assert ledger.balance(saas_db, u.id) == 60
    assert ledger.settle(saas_db, "job_a", 25) is not None
    assert ledger.balance(saas_db, u.id) == 75  # 15 unused credits returned
    assert ledger.settle(saas_db, "job_a", 0) is None  # double settle: no-op
    assert ledger.release(saas_db, "job_a") is None  # release after settle: no-op
    ledger.reserve(saas_db, u.id, "job_b", 30)
    assert ledger.release(saas_db, "job_b") is not None
    assert ledger.settle(saas_db, "job_b", 30) is None  # settle after release: no-op
    assert ledger.balance(saas_db, u.id) == 75
    saas_db.commit()
    audit = ledger.audit(saas_db, u.id)
    assert audit["ledger_sum"] == audit["balance"] == 75


def test_settle_never_charges_more_than_reserved(core, saas_db):
    from saas.billing import ledger

    u = make_user(saas_db, credits=10)
    ledger.reserve(saas_db, u.id, "job_c", 10)
    ledger.settle(saas_db, "job_c", 999)
    assert ledger.balance(saas_db, u.id) == 0
    e = ledger.entries(saas_db, u.id, limit=1)[0]
    assert e.kind == "settle" and e.meta["charged"] == 10


def test_reserve_refuses_to_go_negative(core, saas_db):
    from saas.billing import ledger

    u = make_user(saas_db, credits=5)
    with pytest.raises(ledger.InsufficientCredits):
        ledger.reserve(saas_db, u.id, "job_d", 6)
    assert ledger.balance(saas_db, u.id) == 5


def test_debit_clamps_and_records_shortfall(core, saas_db):
    from saas.billing import ledger

    u = make_user(saas_db, credits=10)
    e = ledger.debit(saas_db, u.id, 25, kind="refund", idempotency_key="r1")
    assert e.amount == -10 and e.meta["shortfall"] == 15
    assert ledger.balance(saas_db, u.id) == 0


def test_subscription_bucket_spent_first_and_rollover_cap(core, saas_db):
    from saas.billing import ledger
    from saas.models.billing import CreditAccount

    u = make_user(saas_db)
    ledger.grant(saas_db, u.id, 50, kind="purchase", idempotency_key="pack")
    ledger.grant(saas_db, u.id, 100, kind="subscription_grant", idempotency_key="sub1", bucket="sub")
    ledger.reserve(saas_db, u.id, "job_e", 30)
    acct = saas_db.get(CreditAccount, u.id)
    saas_db.refresh(acct)
    assert (acct.balance, acct.sub_balance) == (120, 70)
    ledger.release(saas_db, "job_e")  # returned to the bucket it came from
    saas_db.refresh(acct)
    assert (acct.balance, acct.sub_balance) == (150, 100)
    ledger.expire_subscription_excess(saas_db, u.id, 40, idempotency_key="exp1")
    saas_db.refresh(acct)
    assert (acct.balance, acct.sub_balance) == (90, 40)  # pack credits never expire


def test_balance_check_constraint_backstops_the_code(core, saas_db):
    from sqlalchemy import update

    from saas.models.billing import CreditAccount

    u = make_user(saas_db, credits=1)
    with pytest.raises(IntegrityError):
        saas_db.execute(update(CreditAccount).where(CreditAccount.user_id == u.id).values(balance=-5))
        saas_db.flush()
    saas_db.rollback()


# ---- concurrency on a real file database ---------------------------------------------------------

@pytest.fixture()
def file_db(tmp_path):
    from saas import models  # noqa: F401
    from saas.db import Base, _make_engine

    eng = _make_engine(f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}")
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    yield factory
    eng.dispose()


def _user_in(factory, credits: int) -> str:
    from saas.billing import ledger
    from saas.models.core import User

    db = factory()
    u = User(email=f"{uuid.uuid4().hex}@x.com", referral_code=uuid.uuid4().hex[:8], flags={}, signup_risk={})
    db.add(u)
    db.flush()
    ledger.ensure_account(db, u.id)
    if credits:
        ledger.grant(db, u.id, credits, kind="admin_adjust", idempotency_key=f"seed:{u.id}")
    db.commit()
    db.close()
    return u.id


def _run_threads(n, fn):
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = fn(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def test_concurrent_reservations_never_overdraw(file_db):
    from saas.billing import ledger

    uid = _user_in(file_db, 50)

    def attempt(i):
        db = file_db()
        try:
            ledger.reserve(db, uid, f"job_{i}", 10)
            db.commit()
            return "ok"
        except ledger.InsufficientCredits:
            db.rollback()
            return "short"
        except OperationalError:
            db.rollback()
            return "busy"
        finally:
            db.close()

    results = _run_threads(12, attempt)
    db = file_db()
    try:
        bal = ledger.balance(db, uid)
        audit = ledger.audit(db, uid)
    finally:
        db.close()
    assert results.count("ok") == 5
    assert bal == 0 and audit["ledger_sum"] == 0


def test_concurrent_double_settle_charges_once(file_db):
    from saas.billing import ledger

    uid = _user_in(file_db, 30)
    db = file_db()
    ledger.reserve(db, uid, "job_x", 30)
    db.commit()
    db.close()

    def attempt(i):
        db = file_db()
        try:
            out = ledger.settle(db, "job_x", 10) if i % 2 == 0 else ledger.release(db, "job_x")
            db.commit()
            return "applied" if out is not None else "noop"
        except IntegrityError:
            db.rollback()
            return "lost_race"
        finally:
            db.close()

    results = _run_threads(6, attempt)
    db = file_db()
    try:
        bal = ledger.balance(db, uid)
        audit = ledger.audit(db, uid)
    finally:
        db.close()
    assert results.count("applied") == 1
    assert bal in (20, 30)  # settled (charged 10) or released (charged 0), never both
    assert audit["ledger_sum"] == bal


def test_concurrent_duplicate_webhooks_grant_once(file_db, core, monkeypatch, saas_db):
    from saas.billing import ledger, service, webhooks
    from saas.models.billing import Payment

    uid = _user_in(file_db, 0)
    db = file_db()
    p = Payment(user_id=uid, provider="stripe", kind="pack", item_slug="pack-60", currency="USD", amount_minor=900,
                credits=60, status="pending", provider_order_id="cs_race")
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    monkeypatch.setattr(webhooks, "SessionLocal", file_db)

    def handler(dbs, payload):
        pay = dbs.get(Payment, pid)
        return service.fulfil_one_time(dbs, pay, amount_minor=900, currency="usd", provider_payment_id="pi_1"), pid

    results = _run_threads(6, lambda i: webhooks.process_event("stripe", "evt_same", "checkout.session.completed",
                                                                handler, {}).status_code)
    db = file_db()
    try:
        assert ledger.balance(db, uid) == 60
    finally:
        db.close()
    assert all(code == 200 for code in results), results
