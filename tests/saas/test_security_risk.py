"""Disposable emails, canonical aliases, honeypot/timing, Turnstile, Tor/datacenter and assess_signup."""
from __future__ import annotations

import time

import httpx
import pytest
from starlette.requests import Request

from saas.security import disposable, forms, netintel, risk, turnstile


def _request(ip: str = "203.0.113.50", ua: str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0 Safari/537.36", cookies: str = "") -> Request:
    headers = [(b"user-agent", ua.encode())]
    if cookies:
        headers.append((b"cookie", cookies.encode()))
    return Request({"type": "http", "method": "POST", "path": "/auth/signup", "headers": headers,
                    "client": (ip, 1234), "query_string": b"", "scheme": "http", "server": ("testserver", 80)})


# ---- disposable / canonical ---------------------------------------------------------------------
@pytest.mark.parametrize("email", ["a@mailinator.com", "x@10minutemail.com", "y@sub.mailinator.com",
                                   "z@guerrillamail.com", "t@yopmail.com"])
def test_disposable_rejected(email):
    assert disposable.is_disposable(email)


@pytest.mark.parametrize("email", ["a@gmail.com", "b@outlook.com", "c@company.io", "d@proton.me", "e@icloud.com"])
def test_real_providers_allowed(email):
    assert not disposable.is_disposable(email)


def test_disposable_overrides(monkeypatch):
    from saas.config import settings

    monkeypatch.setattr(settings, "disposable_extra_domains", "burner.example")
    monkeypatch.setattr(settings, "disposable_allow_domains", "mailinator.com")
    assert disposable.is_disposable("a@burner.example")
    assert not disposable.is_disposable("a@mailinator.com")


def test_vendored_list_is_loaded():
    assert len(disposable._domains()) > 5000


def test_canonical_collapses_aliases():
    assert disposable.canonical("J.O.E+promo@GoogleMail.com") == "joe@gmail.com"
    assert disposable.canonical("joe+x@outlook.com") == "joe@outlook.com"
    assert disposable.canonical("a.b@company.io") == "a.b@company.io"  # dots only collapse at Gmail


# ---- forms ---------------------------------------------------------------------------------------
def test_honeypot_and_timing():
    now = int(time.time() * 1000)
    assert forms.check(honeypot="http://spam", ts=now - 10_000, now_ms=now).honeypot
    fast = forms.check(honeypot="", ts=now - 200, now_ms=now)
    assert fast.too_fast and fast.is_bot
    ok = forms.check(honeypot="", ts=now - 8_000, now_ms=now)
    assert not ok.is_bot and not ok.reasons
    assert "form_ts_missing" in forms.check(ts=None, now_ms=now).reasons
    assert "form_stale" in forms.check(ts=now - 3 * 86_400_000, now_ms=now).reasons
    skew = forms.check(ts=now + 5_000, now_ms=now)  # client clock ahead: noted, not a bot
    assert not skew.is_bot and "form_ts_future" in skew.reasons
    assert not forms.check(ts=(now - 8_000) // 1000, now_ms=now).too_fast  # seconds tolerated


def test_fingerprint_shape():
    assert forms.clean_fingerprint("AB" * 16) == "ab" * 16
    assert forms.clean_fingerprint("<script>") is None
    assert forms.clean_fingerprint("abc") is None
    assert forms.clean_fingerprint(None) is None


# ---- turnstile -----------------------------------------------------------------------------------
def test_turnstile_passes_without_secret(monkeypatch):
    from saas.config import settings

    monkeypatch.setattr(settings, "turnstile_secret_key", "")
    assert turnstile.verify(None, "1.2.3.4") is True


def _client(body=None, exc=None):
    def handler(request: httpx.Request):
        if exc:
            raise exc
        assert b"secret=s3cr3t" in request.content
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_turnstile_with_secret(monkeypatch):
    from saas.config import settings

    monkeypatch.setattr(settings, "turnstile_secret_key", "s3cr3t")
    assert turnstile.verify(None, "1.2.3.4") is False  # missing token
    assert turnstile.verify("tok", "1.2.3.4", client=_client({"success": True, "hostname": "x"})) is True
    assert turnstile.verify("tok", "1.2.3.4", client=_client({"success": False})) is False
    assert turnstile.verify("tok", "1.2.3.4", client=_client(exc=httpx.ConnectError("down"))) is False
    assert turnstile.verify("tok", "1.2.3.4", action="waitlist",
                            client=_client({"success": True, "action": "login"})) is False


# ---- network intel -------------------------------------------------------------------------------
def test_tor_list_and_asn_classification():
    netintel.reset_state()
    netintel.load_tor_list("# comment\n185.220.101.1\n2a0b:f4c2::1\njunk\n")
    assert netintel.is_tor("185.220.101.1")
    assert netintel.is_tor("::ffff:185.220.101.1")  # mapped v4 is the same client
    assert not netintel.is_tor("8.8.4.4")
    assert netintel.classify_asn(14061, "DIGITALOCEAN-ASN") == (True, False)
    assert netintel.classify_asn(9009, "M247 Europe SRL") == (True, True)
    assert netintel.classify_asn(7922, "Comcast Cable Communications") == (False, False)
    netintel.reset_state()


def test_non_ip_clients_never_crash():
    assert netintel.inspect("testclient").flags() == []
    assert netintel.is_tor("testclient") is False


# ---- assess_signup -------------------------------------------------------------------------------
def test_clean_signup_gets_credits(saas_db):
    v = risk.assess_signup(saas_db, _request(), "alice@company.io", "ab" * 16)
    assert v.block is False and v.grant_free_credits is True and v.score < 40


def test_disposable_signup_blocked(saas_db):
    v = risk.assess_signup(saas_db, _request(), "x@mailinator.com", "ab" * 16)
    assert v.block and "disposable_email" in v.reasons and not v.grant_free_credits


def test_honeypot_and_fast_signup_blocked(saas_db):
    now = int(time.time() * 1000)
    assert risk.assess_signup(saas_db, _request(), "a@company.io", "ab" * 16, honeypot="x", form_ts=now - 9000).block
    assert risk.assess_signup(saas_db, _request(), "a@company.io", "ab" * 16, honeypot="", form_ts=now - 50).block


def test_automation_ua_blocked(saas_db):
    v = risk.assess_signup(saas_db, _request(ua="python-requests/2.32"), "a@company.io", "ab" * 16)
    assert v.block and "automation" in v.reasons


def test_missing_fingerprint_withholds_credits_but_does_not_block(saas_db):
    v = risk.assess_signup(saas_db, _request(), "a@company.io", None)
    assert not v.block and not v.grant_free_credits and "no_fingerprint" in v.reasons


def test_fingerprint_reuse_withholds_credits(saas_db):
    risk.record_signup(saas_db, "usr_1", "first@company.io", "198.51.100.1", "ab" * 16)
    saas_db.commit()
    v = risk.assess_signup(saas_db, _request(), "second@company.io", "ab" * 16)
    assert "fingerprint_reuse" in v.reasons and v.grant_free_credits is False


def test_email_alias_withholds_credits(saas_db):
    risk.record_signup(saas_db, "usr_1", "joe@gmail.com", "198.51.100.1", "cd" * 16)
    saas_db.commit()
    v = risk.assess_signup(saas_db, _request(), "j.o.e+free@gmail.com", "ab" * 16)
    assert "email_alias" in v.reasons and v.grant_free_credits is False


def test_tor_signup_withholds_credits(saas_db):
    # Network intel only judges public addresses, so this uses a real (Tor-operator) range.
    netintel.load_tor_list("185.220.101.7\n")
    try:
        v = risk.assess_signup(saas_db, _request(ip="185.220.101.7"), "a@company.io", "ab" * 16)
        assert "tor_exit" in v.reasons and v.grant_free_credits is False
    finally:
        netintel.reset_state()


def test_banned_email_domain_blocks(saas_db):
    from saas.security import bans

    bans.add(saas_db, "email_domain", "spammy.example", "test")
    saas_db.commit()
    v = risk.assess_signup(saas_db, _request(), "a@spammy.example", "ab" * 16)
    assert v.block and "banned" in v.reasons


def test_ip_velocity_blocks(saas_db, monkeypatch):
    from saas.common import utcnow
    from saas.config import settings
    from saas.models.waitlist import WaitlistEntry

    monkeypatch.setattr(settings, "signup_ip_hourly_limit", 3)
    for i in range(3):
        saas_db.add(WaitlistEntry(email=f"u{i}@company.io", email_canonical=f"u{i}@company.io", seq=i + 1,
                                  rank_key=i + 1, referral_code=f"code{i:04d}", ip="198.51.100.9",
                                  created_at=utcnow()))
    saas_db.commit()
    v = risk.assess_signup(saas_db, _request(ip="198.51.100.9"), "new@company.io", "ab" * 16)
    assert v.block and "ip_velocity" in v.reasons


def test_high_risk_signup_is_logged(saas_db):
    from sqlalchemy import select

    from saas.models.security import SecurityEvent

    risk.assess_signup(saas_db, _request(), "x@mailinator.com", None)
    ev = saas_db.execute(select(SecurityEvent).where(SecurityEvent.kind == "signup_risk")).scalar_one()
    assert ev.severity == "high" and "disposable_email" in ev.detail["reasons"]
