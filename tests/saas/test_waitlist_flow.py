"""Waitlist: join, double opt-in, positions, fraud-resistant referrals, invites and redemption."""
from __future__ import annotations

import re
import time

import pytest
from sqlalchemy import func, select

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


@pytest.fixture()
def settings():
    from saas.config import settings as s

    return s


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    from saas.waitlist import emails

    monkeypatch.setattr(emails, "send", lambda to, subject, text, html, **kw: sent.append(
        {"to": to, "subject": subject, "text": text, "html": html, **kw}) or True)
    return sent


def test_emails_go_through_the_core_mailer_shape(monkeypatch):
    """The core mailer is send(to, subject, paragraphs, button); the link must reach it intact."""
    import types

    from saas.waitlist import emails

    got = {}
    fake = types.SimpleNamespace(send=lambda to, subject, paragraphs, button=None: got.update(
        to=to, subject=subject, paragraphs=paragraphs, button=button))
    import saas.auth as auth_pkg

    monkeypatch.setattr(auth_pkg, "mailer", fake, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "saas.auth.mailer", fake)
    assert emails.confirm_email("a@company.io", "TOKEN123", 4) is True
    assert got["to"] == "a@company.io" and got["button"][1].endswith("/waitlist/confirm/TOKEN123")
    assert all(isinstance(p, str) for p in got["paragraphs"])


@pytest.fixture()
def proxy(settings, monkeypatch):
    """Trust X-Forwarded-For so each test 'person' can have their own IP."""
    monkeypatch.setattr(settings, "trust_proxy", "forwarded")


def form(email, *, fp="ab" * 16, ts_ago_ms=6000, **extra):
    return {"email": email, "fp": fp, "ts": int(time.time() * 1000) - ts_ago_ms, "website": "", **extra}


def headers(ip="198.51.100.10"):
    return {"Origin": "http://testserver", "User-Agent": UA, "X-Forwarded-For": ip}


def join(client, email, ip="198.51.100.10", **kw):
    return client.post("/api/waitlist", json=form(email, **kw), headers=headers(ip))


def token_from(mail):
    return re.search(r"/waitlist/confirm/([A-Za-z0-9_\-]+)", mail["text"]).group(1)


def confirm(client, mail, ip="198.51.100.10", fp=None):
    tok = token_from(mail)
    data = {"fp": fp} if fp else {}
    return client.post(f"/waitlist/confirm/{tok}", data=data, headers=headers(ip), follow_redirects=False)


def entries(db):
    from saas.models.waitlist import WaitlistEntry

    db.expire_all()
    return db.execute(select(WaitlistEntry).order_by(WaitlistEntry.seq)).scalars().all()


# ---- join ----------------------------------------------------------------------------------------
def test_join_creates_pending_entry_and_sends_confirmation(client, saas_db, outbox, proxy):
    r = join(client, "Alice@Company.io", role="creator", channel_url="youtube.com/@alice")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and not body["already"] and body["confirm_required"]
    assert body["referral_url"].endswith("/?ref=" + body["referral_code"]) and body["position"] == 1
    e = entries(saas_db)[0]
    assert e.email == "alice@company.io" and e.status == "pending" and e.role == "creator"
    assert e.channel_url == "https://youtube.com/@alice" and e.fingerprint == "ab" * 16
    assert e.confirm_token_hash and token_from(outbox[0]) not in (e.confirm_token_hash or "")
    assert outbox[0]["to"] == "alice@company.io"


def test_form_encoded_join(client, saas_db, outbox, proxy):
    r = client.post("/api/waitlist", data=form("bob@company.io"), headers=headers())
    assert r.status_code == 200 and len(entries(saas_db)) == 1


def test_honeypot_silently_drops(client, saas_db, outbox, proxy):
    r = join(client, "bot@company.io", website="http://buy-pills.example")
    assert r.status_code == 200 and r.json()["ok"] is True  # the bot learns nothing
    assert entries(saas_db) == [] and outbox == []


def test_too_fast_rejected(client, saas_db, outbox, proxy):
    r = join(client, "fast@company.io", ts_ago_ms=100)
    assert r.status_code == 400 and r.json()["error"] == "too_fast"
    assert entries(saas_db) == []


def test_disposable_rejected(client, saas_db, outbox, proxy):
    r = join(client, "temp@mailinator.com")
    assert r.status_code == 400 and r.json()["error"] == "disposable_email"
    assert entries(saas_db) == []


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "x" * 400 + "@company.io"])
def test_invalid_email_rejected(client, saas_db, outbox, proxy, email):
    assert join(client, email).status_code == 400


def test_turnstile_enforced_when_configured(client, saas_db, outbox, proxy, settings, monkeypatch):
    from saas.security import turnstile

    monkeypatch.setattr(settings, "turnstile_secret_key", "s")
    monkeypatch.setattr(turnstile, "verify", lambda token, ip, **k: token == "good")
    assert join(client, "t@company.io").json()["error"] == "turnstile_failed"
    assert join(client, "t@company.io", cf_turnstile_response="good").status_code == 200


def test_automation_ua_blocked(client, saas_db, outbox, proxy):
    r = client.post("/api/waitlist", json=form("bot@company.io"),
                    headers={**headers(), "User-Agent": "python-requests/2.32"})
    assert r.status_code == 403 and entries(saas_db) == []


def test_alias_is_already_on_list_and_does_not_leak_referral(client, saas_db, outbox, proxy):
    join(client, "joe@gmail.com", ip="198.51.100.20", fp="11" * 16)
    r = join(client, "j.o.e+again@gmail.com", ip="198.51.100.99", fp="22" * 16)
    body = r.json()
    assert body["already"] is True and body["referral_url"] is None and body["position"] is None
    assert len(entries(saas_db)) == 1


def test_same_person_rejoining_sees_their_link(client, saas_db, outbox, proxy):
    first = join(client, "carol@company.io").json()
    again = join(client, "carol@company.io").json()
    assert again["already"] and again["referral_url"] == first["referral_url"]


def test_join_rate_limited_per_ip(client, saas_db, outbox, proxy):
    codes = [join(client, f"user{i}@company.io", ip="198.51.100.77").status_code for i in range(6)]
    assert 429 in codes


def test_velocity_from_one_ip_blocks(client, saas_db, outbox, proxy, settings, monkeypatch):
    monkeypatch.setattr(settings, "signup_ip_hourly_limit", 2)
    from saas.security import limits

    monkeypatch.setattr(limits, "enforce", lambda *a, **k: None)  # isolate the risk rule from route limits
    assert join(client, "a1@company.io", ip="198.51.100.5", fp="31" * 16).status_code == 200
    assert join(client, "a2@company.io", ip="198.51.100.5", fp="32" * 16).status_code == 200
    r = join(client, "a3@company.io", ip="198.51.100.5", fp="33" * 16)
    assert r.status_code == 429 and len(entries(saas_db)) == 2


# ---- confirm / position --------------------------------------------------------------------------
def test_confirm_via_interstitial_then_post(client, saas_db, outbox, proxy):
    join(client, "dave@company.io")
    tok = token_from(outbox[0])
    page = client.get(f"/waitlist/confirm/{tok}", headers=headers())
    assert page.status_code == 200 and 'method="post"' in page.text
    nonce = page.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in page.text  # the auto-submit script is allowed by the CSP
    assert entries(saas_db)[0].status == "pending"  # a GET (mail scanner) does not confirm
    r = confirm(client, outbox[0])
    assert r.status_code == 303 and r.headers["location"].startswith("/waitlist/thanks?code=")
    e = entries(saas_db)[0]
    assert e.status == "confirmed" and e.confirmed_at is not None
    # Following the link again goes straight to the thanks page.
    assert client.get(f"/waitlist/confirm/{tok}", follow_redirects=False).status_code == 303


def test_invalid_and_expired_tokens(client, saas_db, outbox, proxy):
    from datetime import timedelta

    from saas.common import utcnow

    assert client.get("/waitlist/confirm/nope").status_code == 404
    join(client, "erin@company.io")
    e = entries(saas_db)[0]
    e.confirm_expires_at = utcnow() - timedelta(hours=1)
    saas_db.commit()
    r = client.post(f"/waitlist/confirm/{token_from(outbox[0])}", headers={**headers(), "Accept": "application/json"})
    assert r.status_code == 410 and r.json()["error"] == "expired"


def test_positions_and_me_endpoint(client, saas_db, outbox, proxy):
    for i, ip in enumerate(["198.51.100.1", "198.51.100.2", "198.51.100.3"]):
        join(client, f"p{i}@company.io", ip=ip, fp=f"{i}{i}" * 16)
    for i, mail in enumerate(outbox):
        confirm(client, mail, ip=f"198.51.100.{i + 1}")
    es = entries(saas_db)
    me = client.get(f"/api/waitlist/me?code={es[2].referral_code}").json()
    assert me["position"] == 3 and me["total"] == 3 and "email" not in me
    assert client.get("/api/waitlist/me?code=zzzzzzzz").status_code == 404


# ---- referrals -----------------------------------------------------------------------------------
def _referrer(client, saas_db, outbox):
    join(client, "ref@company.io", ip="198.51.100.40", fp="40" * 16)
    confirm(client, outbox[-1], ip="198.51.100.40", fp="40" * 16)
    return entries(saas_db)[0]


def test_referral_counts_only_after_confirmation(client, saas_db, outbox, proxy):
    referrer = _referrer(client, saas_db, outbox)
    join(client, "friend@company.io", ip="203.0.114.1", fp="41" * 16, ref=referrer.referral_code)
    assert entries(saas_db)[0].referrals_counted == 0  # not yet: unconfirmed
    confirm(client, outbox[-1], ip="203.0.114.1", fp="41" * 16)
    es = entries(saas_db)
    assert es[0].referrals_counted == 1 and es[1].referral_status == "counted"
    assert es[0].rank_key < es[0].seq  # moved up


def test_referral_from_same_ip_not_counted(client, saas_db, outbox, proxy):
    referrer = _referrer(client, saas_db, outbox)
    join(client, "sock@company.io", ip="198.51.100.40", fp="42" * 16, ref=referrer.referral_code)
    confirm(client, outbox[-1], ip="198.51.100.40", fp="42" * 16)
    es = entries(saas_db)
    assert es[0].referrals_counted == 0
    assert es[1].referral_status == "rejected" and es[1].referral_reason == "same_ip"


def test_referral_from_same_fingerprint_not_counted(client, saas_db, outbox, proxy):
    referrer = _referrer(client, saas_db, outbox)
    join(client, "sock2@company.io", ip="203.0.114.9", fp="40" * 16, ref=referrer.referral_code)
    confirm(client, outbox[-1], ip="203.0.114.9", fp="40" * 16)
    es = entries(saas_db)
    assert es[0].referrals_counted == 0 and es[1].referral_reason == "same_fingerprint"


def test_referral_confirmed_from_referrers_machine_not_counted(client, saas_db, outbox, proxy):
    """Joined from elsewhere, but clicked the confirm link on the referrer's machine."""
    referrer = _referrer(client, saas_db, outbox)
    join(client, "sock3@company.io", ip="203.0.114.10", fp="43" * 16, ref=referrer.referral_code)
    confirm(client, outbox[-1], ip="198.51.100.40", fp="40" * 16)
    assert entries(saas_db)[0].referrals_counted == 0


def test_sibling_referrals_from_one_machine_count_once(client, saas_db, outbox, proxy):
    referrer = _referrer(client, saas_db, outbox)
    for i, email in enumerate(["s1@company.io", "s2@company.io", "s3@company.io"]):
        join(client, email, ip="203.0.114.50", fp="44" * 16 if i == 0 else f"4{i + 4}" * 16,
             ref=referrer.referral_code)
        confirm(client, outbox[-1], ip="203.0.114.50")
    es = entries(saas_db)
    assert es[0].referrals_counted == 1
    assert [e.referral_reason for e in es[2:]] == ["sibling_cluster", "sibling_cluster"]


def test_self_referral_via_alias_ignored(client, saas_db, outbox, proxy):
    join(client, "self@gmail.com", ip="198.51.100.60", fp="50" * 16)
    code = entries(saas_db)[0].referral_code
    r = join(client, "s.e.l.f+2@gmail.com", ip="203.0.114.60", fp="51" * 16, ref=code)
    assert r.json()["already"] is True and len(entries(saas_db)) == 1


# ---- invites -------------------------------------------------------------------------------------
def _confirmed(client, saas_db, outbox, n):
    for i in range(n):
        join(client, f"inv{i}@company.io", ip=f"198.51.100.{100 + i}", fp=f"{i + 60}" * 16)
        confirm(client, outbox[-1], ip=f"198.51.100.{100 + i}")
    return entries(saas_db)


def test_invite_batch_and_redeem(client, saas_db, outbox, proxy):
    from saas import waitlist
    from saas.waitlist import service

    es = _confirmed(client, saas_db, outbox, 3)
    batch = service.invite_batch(saas_db, 2, actor_id="usr_admin", note="first wave")
    assert batch.invited == 2
    es = entries(saas_db)
    invited = [e for e in es if e.status == "invited"]
    assert [e.email for e in invited] == ["inv0@company.io", "inv1@company.io"]
    mail = [m for m in outbox if "invite" in m["subject"].lower()]
    assert len(mail) == 2 and invited[0].invite_code in mail[0]["text"]

    code = invited[0].invite_code
    assert waitlist.redeem_invite(saas_db, code, "someone-else@company.io") is None  # bound to the email
    got = waitlist.redeem_invite(saas_db, code.lower(), "inv0@company.io")
    assert got is not None and got.status == "joined"
    assert waitlist.redeem_invite(saas_db, code, "inv0@company.io") is not None  # idempotent
    assert waitlist.redeem_invite(saas_db, "NOPE-NOPE-NOPE", "inv0@company.io") is None
    assert waitlist.redeem_invite(saas_db, es[2].invite_code or "", "inv2@company.io") is None  # not invited


def test_expired_invite_not_redeemable(client, saas_db, outbox, proxy):
    from datetime import timedelta

    from saas import waitlist
    from saas.common import utcnow
    from saas.waitlist import service

    _confirmed(client, saas_db, outbox, 1)
    service.invite_batch(saas_db, 1)
    e = entries(saas_db)[0]
    e.invite_expires_at = utcnow() - timedelta(days=1)
    saas_db.commit()
    assert waitlist.redeem_invite(saas_db, e.invite_code, "inv0@company.io") is None


def test_flagged_entries_skipped_by_batches(client, saas_db, outbox, proxy):
    from saas.waitlist import service

    es = _confirmed(client, saas_db, outbox, 2)
    es[0].flagged = True
    saas_db.commit()
    service.invite_batch(saas_db, 5)
    es = entries(saas_db)
    assert es[0].status == "confirmed" and es[1].status == "invited"


def test_resend(client, saas_db, outbox, proxy):
    from saas.waitlist import service

    join(client, "re@company.io")
    e = entries(saas_db)[0]
    assert service.resend(saas_db, e) == "confirm" and len(outbox) == 2
    assert token_from(outbox[0]) != token_from(outbox[1])
    r = confirm(client, outbox[0])  # the superseded link no longer works
    assert r.status_code == 404
    assert confirm(client, outbox[1]).status_code == 303


def test_join_links_visitor_and_scores(client, saas_db, outbox, proxy, settings, monkeypatch):
    from saas.models.tracking import Visitor

    monkeypatch.setattr(settings, "consent_mode", "notice_only")
    client.get("/api/t/config", headers=headers())
    client.post("/api/t", json={"v": 1, "pv": "p_" + "e" * 24, "now": time.time() * 1000, "url": "/",
                                "e": [{"t": "pageview"}]}, headers=headers())
    join(client, "linked@company.io")
    v = saas_db.execute(select(Visitor)).scalar_one()
    saas_db.refresh(v)
    assert v.waitlist_id == entries(saas_db)[0].id and v.lead_score >= 20
    assert entries(saas_db)[0].source["landing"] == "/"


def test_contract_redeem_import_is_lazy():
    import importlib

    mod = importlib.import_module("saas.waitlist")
    assert callable(mod.redeem_invite)


def test_counts(saas_db):
    from saas.models.waitlist import WaitlistEntry

    assert saas_db.execute(select(func.count()).select_from(WaitlistEntry)).scalar_one() == 0
