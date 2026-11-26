"""Signup in launch_mode=waitlist against the real saas.waitlist module (not a patched shim), and
enumeration safety when the invite gate is on."""
from __future__ import annotations

import pytest

from core_fixtures import PASSWORD, core, form_fields, make_user, outbox_link  # noqa: F401


@pytest.fixture()
def waitlist(core, monkeypatch, saas_db):
    from saas.security import disposable
    from saas.waitlist import service as wl

    monkeypatch.setattr(core, "launch_mode", "waitlist")
    entries = {}

    def invited(email: str):
        from saas.models.waitlist import WaitlistEntry

        e = WaitlistEntry(email=email, email_canonical=disposable.canonical(email), status="confirmed",
                          seq=len(entries) + 1, rank_key=len(entries) + 1, referral_code=f"REF{len(entries)}X")
        saas_db.add(e)
        saas_db.flush()
        wl.invite(saas_db, e, send=False)
        saas_db.commit()
        entries[email] = e
        return e.invite_code

    return invited


def _signup(client, email, **extra):
    client.cookies.clear()
    return client.post("/auth/signup", json={"email": email, "password": PASSWORD, **form_fields(), **extra})


def test_real_invite_redemption_links_the_account(client, core, saas_db, waitlist):
    from saas.models.core import User
    from saas.models.waitlist import WaitlistEntry

    code = waitlist("invited@example.com")
    assert _signup(client, "invited@example.com").json()["error"] == "invite_required"
    assert _signup(client, "invited@example.com", invite="WRONG-CODE-0000").json()["error"] == "invite_required"
    r = _signup(client, "invited@example.com", invite=code)
    assert r.status_code == 201, r.text
    user = saas_db.query(User).filter_by(email="invited@example.com").one()
    entry = saas_db.query(WaitlistEntry).filter_by(email="invited@example.com").one()
    saas_db.refresh(entry)
    assert entry.status == "joined" and entry.redeemed_user_id == user.id
    # The invite is bound to its address: someone else can't use it.
    assert _signup(client, "someone.else@example.com", invite=code).json()["error"] == "invite_required"
    from saas.billing import ledger

    client.get(f"/auth/verify/{outbox_link('/auth/verify/')}", follow_redirects=False)
    assert ledger.free_balance(saas_db, user.id) == (10, 10)


def test_waitlist_mode_does_not_reveal_existing_accounts(client, core, saas_db, waitlist):
    from saas.models.waitlist import WaitlistEntry

    make_user(saas_db, "exists@example.com")
    # Without an invite, an existing and a new address get the same refusal.
    a = _signup(client, "exists@example.com")
    b = _signup(client, "brand.new@example.com")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (403, a.json())
    assert a.json()["error"] == "invite_required"
    # With a valid invite for an address that already has an account: same 201 as a new signup, and the
    # invite is not consumed.
    code = waitlist("exists@example.com")
    r = _signup(client, "exists@example.com", invite=code)
    assert r.status_code == 201 and r.json() == {"ok": True, "verify_required": True}
    saas_db.expire_all()
    assert saas_db.query(WaitlistEntry).filter_by(email="exists@example.com").one().status == "invited"
