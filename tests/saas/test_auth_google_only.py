"""Google-only sign-in (SAAS_AUTH_GOOGLE_ONLY): the flag, retired routes, no mail, account linking, the admin
path, invite links and the error pages. Google itself is faked at the token exchange, exactly where the real
round trip would hand us an ID token."""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import time
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest

from core_fixtures import PASSWORD, core, csrf, form_fields, make_user  # noqa: F401

CID = "cid.apps.googleusercontent.com"


# ---- fixtures and helpers ---------------------------------------------------------------------------

@pytest.fixture()
def gonly(core, monkeypatch):
    """Google configured, no SMTP, flag unset: the default resolves to Google-only."""
    monkeypatch.setattr(core, "google_client_id", CID)
    monkeypatch.setattr(core, "google_client_secret", "secret")
    monkeypatch.setattr(core, "smtp_host", "")
    monkeypatch.setattr(core, "auth_google_only", None)
    from saas.auth import policy

    assert policy.google_only()
    return core


@pytest.fixture()
def mail_spy(monkeypatch):
    """Records every way mail could leave: the core mailer, its delivery, the waitlist sender and SMTP itself."""
    import smtplib

    from saas.auth import mailer
    from saas.waitlist import emails

    calls: list = []
    monkeypatch.setattr(mailer, "send", lambda *a, **k: calls.append(("mailer.send", a[:2])))
    monkeypatch.setattr(mailer, "_deliver", lambda mail: calls.append(("deliver", mail.subject)) or (True, ""))
    monkeypatch.setattr(emails, "send", lambda *a, **k: calls.append(("waitlist.send", a[:2])) or True)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: calls.append(("smtp", a)))
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: calls.append(("smtp_ssl", a)))
    yield calls
    assert not mailer.outbox


def waitlist_mode(core, monkeypatch):
    from saas.templating import templates

    monkeypatch.setattr(core, "launch_mode", "waitlist")
    monkeypatch.setitem(templates.env.globals, "launch_mode", "waitlist")  # templates read a frozen global


def _id_token(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def start(client, query: str = ""):
    """GET /auth/google/start -> (state, the query sent to Google)."""
    from saas.auth import google

    r = client.get(f"/auth/google/start{query}", follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith(google.AUTH_URL), r.headers["location"]
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q["state"][0], q


def round_trip(client, monkeypatch, *, email, sub=None, verified=True, query="", name="G User"):
    """Start, let 'Google' answer for `email`, and return the callback's response."""
    from saas.auth import google

    state, q = start(client, query)
    claims = {"iss": "https://accounts.google.com", "aud": CID, "sub": sub or f"sub-{email}", "email": email,
              "email_verified": verified, "exp": time.time() + 600, "iat": time.time(), "nonce": q["nonce"][0],
              "name": name}
    monkeypatch.setattr(google, "exchange_code", lambda code, verifier, client=None: {"id_token": _id_token(claims)})
    r = client.get(f"/auth/google/callback?state={state}&code=abc", follow_redirects=False)
    r.google_query = q
    return r


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.untracked: list = []
        self.inline_scripts = 0
        self.style_attrs = 0
        self.hrefs: list[str] = []
        self.names: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "style" in a:
            self.style_attrs += 1
        if tag in ("a", "button") and not a.get("data-track"):
            self.untracked.append((tag, a))
        if tag == "script" and not a.get("src"):
            self.inline_scripts += 1
        if tag == "a":
            self.hrefs.append(a.get("href", ""))
        if tag in ("input", "textarea") and a.get("name"):
            self.names.append(a["name"])


def parse(html: str) -> Page:
    p = Page()
    p.feed(html)
    assert not p.untracked and p.inline_scripts == 0 and p.style_attrs == 0, (p.untracked[:2], p.style_attrs)
    return p


def manage():
    path = Path(__file__).resolve().parents[2] / "scripts" / "saas_manage.py"
    spec = importlib.util.spec_from_file_location("saas_manage_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the flag ---------------------------------------------------------------------------------------

def test_flag_defaults_to_google_only_when_google_is_set_up_and_smtp_is_not(core, monkeypatch):
    from saas.auth import policy
    from saas.config import SaasSettings

    monkeypatch.setattr(core, "auth_google_only", None)
    monkeypatch.setattr(core, "smtp_host", "")
    monkeypatch.setattr(core, "google_client_id", "")
    assert not policy.google_only()  # no Google: passwords stay
    monkeypatch.setattr(core, "google_client_id", CID)
    monkeypatch.setattr(core, "google_client_secret", "secret")
    assert policy.google_only() and not policy.mail_allowed()
    monkeypatch.setattr(core, "smtp_host", "smtp.gmail.com")
    assert not policy.google_only() and policy.mail_allowed()  # a mailer: both ways in
    monkeypatch.setattr(core, "auth_google_only", True)
    assert policy.google_only() and not policy.mail_allowed()  # explicit wins, and still no mail
    monkeypatch.setattr(core, "auth_google_only", False)
    monkeypatch.setattr(core, "smtp_host", "")
    assert not policy.google_only()
    for raw, want in (("auto", None), ("", None), ("true", True), ("0", False)):
        assert SaasSettings(_env_file=None, auth_google_only=raw).auth_google_only is want


def test_password_routes_are_gone_and_their_pages_redirect(client, gonly, saas_db, monkeypatch):
    for path, body in (("/auth/signup", {"email": "a@example.com", "password": PASSWORD, **form_fields()}),
                       ("/auth/login", {"email": "a@example.com", "password": PASSWORD, **form_fields()}),
                       ("/auth/magic", {"email": "a@example.com", **form_fields()}),
                       ("/auth/forgot", {"email": "a@example.com", **form_fields()}),
                       ("/auth/reset", {"token": "t" * 40, "password": PASSWORD})):
        r = client.post(path, json=body)
        assert r.status_code == 404 and r.json()["error"] == "google_only", (path, r.text)
    for path in ("/forgot", "/reset/sometoken", "/verify-email", "/auth/verify/sometoken", "/auth/magic/sometoken"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?notice=google_only", path
    assert "We now sign everyone in with Google" in client.get("/login?notice=google_only").text
    # Signed in (through Google), the password endpoints are still closed.
    assert round_trip(client, monkeypatch, email="me@gmail.com").status_code == 303
    h = csrf(client)
    assert client.post("/auth/verify/resend", headers=h).json()["error"] == "google_only"
    r = client.post("/api/me/password", json={"new_password": "Another-Long-Pass-99"}, headers=h)
    assert r.status_code == 404 and r.json()["error"] == "google_only"
    assert client.post("/auth/reauth", json={"password": PASSWORD}, headers=h).json()["error"] == "google_only"


def test_flag_off_keeps_both_ways_in(client, core, monkeypatch, saas_db):
    monkeypatch.setattr(core, "google_client_id", CID)
    monkeypatch.setattr(core, "google_client_secret", "secret")
    monkeypatch.setattr(core, "auth_google_only", False)
    make_user(saas_db, "pw@example.com")
    html = client.get("/login").text
    assert "data-login-form" in html and "data-magic-link" in html and "/auth/google/start" in html
    assert client.post("/auth/login", json={"email": "pw@example.com", "password": "wrong-password-1",
                                            **form_fields()}).status_code == 401
    assert client.post("/auth/login", json={"email": "pw@example.com", "password": PASSWORD,
                                            **form_fields()}).status_code == 200
    assert client.get("/forgot", follow_redirects=False).status_code == 200


def test_sign_in_pages_show_only_the_official_google_button(client, gonly):
    for path in ("/login", "/login?next=/app/billing", "/signup"):
        html = client.get(path).text
        page = parse(html)
        assert "/static/vendor/google-signin/light-1x.png" in html and 'alt="Sign in with Google"' in html
        assert "We keep only your name and email address from Google" in html
        assert "password" not in page.names and "email" not in page.names
        assert "data-magic-link" not in html and 'href="/forgot"' not in html
    assert "/auth/google/start?next=/app/billing" in parse(client.get("/login?next=/app/billing").text).hrefs
    for theme in ("light", "dark", "neutral"):
        for n in (1, 2, 3, 4):
            r = client.get(f"/static/vendor/google-signin/{theme}-{n}x.png")
            assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_explicit_google_only_without_google_says_so_instead_of_a_dead_button(client, core, monkeypatch):
    monkeypatch.setattr(core, "auth_google_only", True)
    monkeypatch.setattr(core, "google_client_id", "")
    html = client.get("/login").text
    assert "isn’t set up on this server yet" in html and "light-1x.png" not in html


# ---- no mail ----------------------------------------------------------------------------------------

def test_no_code_path_tries_to_send_mail(client, gonly, saas_db, monkeypatch, mail_spy):
    from saas.models.waitlist import WaitlistEntry
    from saas.waitlist import service as wl

    # Retired flows, a Google sign-up, 2FA on and off: the security notices are skipped too.
    for path in ("/auth/signup", "/auth/magic", "/auth/forgot"):
        client.post(path, json={"email": "x@example.com", "password": PASSWORD, **form_fields()})
    round_trip(client, monkeypatch, email="me@gmail.com")
    h = csrf(client)
    secret = client.post("/api/me/mfa/totp/setup", headers=h).json()["secret"]
    assert client.post("/api/me/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=h).status_code == 200
    code = pyotp.TOTP(secret).at(time.time() + 30)
    assert client.post("/api/me/mfa/totp/disable", json={"code": code}, headers=csrf(client)).status_code == 200

    # The waitlist: joins confirm at once, invites (single, batch, resend) become links.
    for i, email in enumerate(("a@company.io", "b@company.io", "c@company.io")):
        saas_db.add(WaitlistEntry(email=email, email_canonical=email, status="confirmed", seq=i + 1, rank_key=i + 1,
                                  referral_code=f"refx{i}abc"))
    saas_db.commit()
    e = saas_db.query(WaitlistEntry).filter_by(email="a@company.io").one()
    wl.invite(saas_db, e)  # send defaults to True; without mail it must not try
    assert wl.resend(saas_db, e) == "link" and e.invite_sends == 0
    assert wl.invite_batch(saas_db, 5).invited == 2
    assert not mail_spy, mail_spy


def test_the_senders_themselves_refuse_in_google_only_mode(gonly, monkeypatch):
    """The backstop under every caller: no thread, no outbox entry, no SMTP connection."""
    import threading

    from saas.auth import mailer
    from saas.waitlist import emails

    started, delivered = [], []
    monkeypatch.setattr(mailer, "_SYNC_FOR_TESTS", False)
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: started.append(k) or SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(mailer, "_deliver", lambda mail: delivered.append(mail) or (True, ""))
    mailer.send("x@example.com", "subject", ["body"])
    mailer.send_mfa_changed("x@example.com", True)
    assert emails.send("x@example.com", "s", "t", "<p>t</p>") is False
    assert emails.invite_email("x@example.com", "CODE-CODE-CODE", 14) is False
    assert not started and not delivered and not mailer.outbox
    ok, detail = mailer.send_test("x@example.com")
    assert ok is False and "Google-only" in detail


def test_waitlist_join_is_confirmed_without_a_mail(client, gonly, saas_db, monkeypatch, mail_spy):
    from saas.models.waitlist import WaitlistEntry

    monkeypatch.setattr(gonly, "trust_proxy", "forwarded")
    body = {"email": "joiner@company.io", "fp": "ab" * 16, "ts": int(time.time() * 1000) - 6000, "website": ""}
    r = client.post("/api/waitlist", json=body, headers={
        "Origin": "http://testserver", "X-Forwarded-For": "198.51.100.77",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"})
    assert r.status_code == 200, r.text
    assert r.json()["confirm_required"] is False and r.json()["position"] == 1
    saas_db.expire_all()
    assert saas_db.query(WaitlistEntry).one().status == "confirmed"
    assert not mail_spy


# ---- accounts ---------------------------------------------------------------------------------------

def test_existing_account_is_linked_only_when_google_verified_the_email(client, gonly, saas_db, monkeypatch):
    from saas.models.core import AuthSession, Identity, User

    old = make_user(saas_db, "old@example.com")  # a password-era account, email confirmed
    r = round_trip(client, monkeypatch, email="old@example.com", verified=False)
    assert r.headers["location"] == "/login?error=oauth_unverified_email"
    assert saas_db.query(Identity).count() == 0 and saas_db.query(AuthSession).count() == 0
    assert "Google hasn’t verified that email yet." in client.get(r.headers["location"]).text

    r = round_trip(client, monkeypatch, email="old@example.com", sub="g-old")
    assert r.status_code == 303 and r.headers["location"] == "/app"  # their account, not a new one
    assert client.get("/api/me").json()["id"] == old.id
    assert saas_db.query(Identity).filter_by(user_id=old.id, subject="g-old").count() == 1
    assert saas_db.query(User).count() == 1


def test_gmail_dots_and_tags_reach_the_same_verified_account(client, gonly, saas_db, monkeypatch):
    from saas.models.core import User

    old = make_user(saas_db, "j.doe+clips@gmail.com")
    assert round_trip(client, monkeypatch, email="jdoe@gmail.com").headers["location"] == "/app"
    assert client.get("/api/me").json()["id"] == old.id and saas_db.query(User).count() == 1


def test_first_google_sign_in_lands_in_the_app_with_credits(client, gonly, saas_db, monkeypatch):
    r = round_trip(client, monkeypatch, email="new@gmail.com")
    assert r.headers["location"] == "/app?welcome=1"
    me = client.get("/api/me").json()
    assert me["email_verified"] and me["free"] == {"minutes": 10, "shorts": 10} and not me["has_password"]
    assert "Your free 10 Shorts or 10 minutes are ready" in client.get("/app?welcome=1").text


# ---- the admin ----------------------------------------------------------------------------------------

def test_admin_grant_google_sign_in_totp_then_admin_and_next_survives_2fa(client, gonly, saas_db, monkeypatch,
                                                                           capsys):
    from saas.models.core import User

    waitlist_mode(gonly, monkeypatch)  # the owner must get in even while sign-ups are invite-only
    assert manage().main(["grant-admin", "Owner@Gmail.com"]) == 0
    assert "claimed at their first Google sign-in" in capsys.readouterr().out
    r = round_trip(client, monkeypatch, email="owner@gmail.com", sub="g-owner")
    assert r.status_code == 303 and r.headers["location"] == "/app", r.headers["location"]
    saas_db.expire_all()
    owner = saas_db.query(User).filter_by(email="owner@gmail.com").one()
    assert owner.role == "admin" and owner.email_verified_at is not None

    denied = client.get("/admin", headers={"Accept": "text/html"})
    assert denied.status_code == 403
    assert "/login?stepup=1&amp;next=/admin" in denied.text and "/app/settings#mfa-title" in denied.text
    assert client.get("/login?stepup=1&next=/admin", follow_redirects=False).headers["location"] == \
        "/app/settings#mfa-title"  # no 2FA yet: set it up first

    h = csrf(client)
    secret = client.post("/api/me/mfa/totp/setup", headers=h).json()["secret"]  # fresh session counts as recent
    assert client.post("/api/me/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()},
                       headers=csrf(client)).status_code == 200
    assert client.get("/admin/api/live").status_code == 200

    client.cookies.clear()  # a later visit straight to the panel
    r = round_trip(client, monkeypatch, email="owner@gmail.com", sub="g-owner", query="?next=/admin")
    loc = r.headers["location"]
    assert loc.startswith("/login?mfa=") and loc.endswith("&next=%2Fadmin"), loc
    page = client.get(loc).text
    assert 'data-next="/admin"' in page and "data-mfa-form" in page
    token = parse_qs(urlparse(loc).query)["mfa"][0]
    r = client.post("/auth/login/mfa", json={"mfa_token": token, "code": pyotp.TOTP(secret).at(time.time() + 30)})
    assert r.status_code == 200 and r.json()["next"] == "/admin"
    assert client.get("/admin/api/live").status_code == 200


def test_admin_email_list_skips_the_invite_gate(client, gonly, saas_db, monkeypatch):
    from saas.models.core import User

    waitlist_mode(gonly, monkeypatch)
    monkeypatch.setattr(gonly, "admin_emails", "boss@gmail.com")
    assert round_trip(client, monkeypatch, email="boss@gmail.com").headers["location"] == "/app?welcome=1"
    assert saas_db.query(User).filter_by(email="boss@gmail.com").one().role == "admin"
    client.cookies.clear()
    assert round_trip(client, monkeypatch, email="nobody@gmail.com").headers["location"] == "/login?error=invite_required"


def test_grant_admin_promotes_an_existing_account(client, gonly, saas_db, capsys):
    from saas.models.core import User

    u = make_user(saas_db, "staff@gmail.com")
    assert manage().main(["grant-admin", "staff@gmail.com"]) == 0
    saas_db.expire_all()
    assert saas_db.get(User, u.id).role == "admin"
    assert "turn on two-factor" in capsys.readouterr().out


# ---- invites -------------------------------------------------------------------------------------------

@pytest.fixture()
def admin_api(client):
    from saas.admin import guard

    client.app.dependency_overrides[guard.require_admin] = lambda: SimpleNamespace(
        id="usr_admin", email="owner@example.com", role="admin", status="active")
    client.cookies.set("sid", "s")
    client.cookies.set("csrf", "c" * 32)
    yield {"Origin": "http://testserver", "X-CSRF-Token": "c" * 32}
    client.app.dependency_overrides.clear()
    client.cookies.clear()


def test_admin_gets_copyable_invite_links_that_feed_google_start(client, gonly, saas_db, monkeypatch, mail_spy,
                                                                 admin_api):
    from saas.models.core import User
    from saas.models.waitlist import WaitlistEntry

    waitlist_mode(gonly, monkeypatch)
    saas_db.add(WaitlistEntry(email="friend@gmail.com", email_canonical="friend@gmail.com", status="confirmed",
                              seq=1, rank_key=1, referral_code="reffriend1"))
    saas_db.add(WaitlistEntry(email="next@gmail.com", email_canonical="next@gmail.com", status="confirmed",
                              seq=2, rank_key=2, referral_code="refnext001"))
    saas_db.commit()
    e = saas_db.query(WaitlistEntry).filter_by(email="friend@gmail.com").one()
    r = client.post(f"/admin/api/waitlist/{e.id}/invite", json={}, headers=admin_api)
    assert r.status_code == 200, r.text
    url = r.json()["invite_url"]
    saas_db.refresh(e)
    assert url == f"http://127.0.0.1:8700/signup?invite={e.invite_code}" and "Nothing was emailed" in r.json()["message"]
    batch = client.post("/admin/api/waitlist/invite", json={"count": 5}, headers=admin_api).json()
    assert batch["invited"] == 1 and batch["links"][0]["email"] == "next@gmail.com"
    panel = client.get("/admin/waitlist", headers={"Accept": "text/html"}).text
    assert f'data-copy-text="{url}"' in panel and "Create invite links" in panel and ">Resend<" not in panel
    assert not mail_spy
    client.app.dependency_overrides.clear()
    client.cookies.clear()

    # The link: a page about the invite whose one button starts Google with it, pre-selecting the account.
    page = client.get(f"/signup?invite={e.invite_code}").text
    assert "You’re invited." in page and "fr•••@gmail.com" in page
    href = f"/auth/google/start?next=/app&amp;invite={e.invite_code}"
    assert href in page
    state, q = start(client, f"?next=%2Fapp&invite={e.invite_code}")
    assert q["login_hint"] == ["friend@gmail.com"] and q["prompt"] == ["select_account"]

    # The wrong Google account gets told which one to pick; the right one gets in.
    r = round_trip(client, monkeypatch, email="someone.else@gmail.com", query=f"?invite={e.invite_code}")
    assert r.headers["location"] == f"/login?error=invite_required&invite={e.invite_code}"
    html = client.get(r.headers["location"]).text
    assert "This invite is for a different email." in html and "fr•••@gmail.com" in html
    assert f"invite={e.invite_code}" in html  # the retry keeps the invite
    r = round_trip(client, monkeypatch, email="friend@gmail.com", query=f"?invite={e.invite_code}")
    assert r.headers["location"] == "/app?welcome=1"
    saas_db.expire_all()
    assert saas_db.get(WaitlistEntry, e.id).status == "joined"
    assert saas_db.query(User).filter_by(email="friend@gmail.com").one().email_verified_at is not None


def test_cli_invite_prints_a_link_and_sends_nothing(client, gonly, saas_db, monkeypatch, mail_spy, capsys):
    waitlist_mode(gonly, monkeypatch)
    mod = manage()
    with pytest.raises(SystemExit):
        mod.main(["invite", "pal@gmail.com"])  # not on the list: say so, change nothing
    assert mod.main(["invite", "pal@gmail.com", "--add"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"http://127\.0\.0\.1:8700/signup\?invite=[A-Za-z0-9-]+", out) and "pal@gmail.com" in out
    assert not mail_spy


# ---- re-auth ---------------------------------------------------------------------------------------------

def test_sensitive_actions_reauth_through_google(client, gonly, saas_db, monkeypatch):
    from saas.common import utcnow
    from saas.models.core import AuthSession

    round_trip(client, monkeypatch, email="me@gmail.com", sub="g-me")
    saas_db.query(AuthSession).update({"reauth_at": utcnow() - timedelta(hours=1)})
    saas_db.commit()
    assert client.post("/api/me/mfa/totp/setup", headers=csrf(client)).json()["error"] == "reauth_required"

    other = round_trip(client, monkeypatch, email="other@gmail.com", sub="g-other",
                       query="?reauth=1&next=/app/settings")
    assert other.headers["location"] == "/login?error=reauth_other_account&next=%2Fapp%2Fsettings&reauth=1"
    html = client.get(other.headers["location"]).text
    assert "That’s a different Google account." in html and "m•••@gmail.com" in html
    assert "reauth=1" in html

    r = round_trip(client, monkeypatch, email="me@gmail.com", sub="g-me", query="?reauth=1&next=/app/settings")
    assert r.google_query["login_hint"] == ["me@gmail.com"]
    assert r.headers["location"] == "/app/settings"
    assert client.post("/api/me/mfa/totp/setup", headers=csrf(client)).status_code == 200


def test_app_pages_offer_google_where_they_offered_passwords(client, gonly, saas_db, monkeypatch):
    from saas.models.core import User

    round_trip(client, monkeypatch, email="me@gmail.com")
    html = client.get("/app/settings").text
    parse(html)
    assert "data-password-form" not in html and "How you sign in" in html
    assert "/auth/google/start?reauth=1" in html and 'name="password"' not in html
    u = saas_db.query(User).filter_by(email="me@gmail.com").one()
    u.email_verified_at = None  # an account from before the switch
    saas_db.commit()
    html = client.get("/app").text
    assert "Confirm with Google" in html and "data-resend-verify" not in html


def test_one_click_from_the_landing_page_and_signed_in_visitors_skip_google(client, gonly, monkeypatch):
    html = client.get("/").text
    assert 'href="/auth/google/start?next=/app"' in html  # header and hero, straight to Google
    round_trip(client, monkeypatch, email="me@gmail.com")
    r = client.get("/auth/google/start?next=/app/clips", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/clips"


# ---- error pages -------------------------------------------------------------------------------------------

def _error_page(client, location: str, title: str) -> Page:
    r = client.get(location)
    assert r.status_code == 200 and title in r.text, (location, r.text[:400])
    return parse(r.text)


def test_cancel_and_denied_consent_pages_retry_to_the_same_place(client, gonly, saas_db):
    from saas.models.core import OAuthState

    state, _ = start(client, "?next=/app/billing")
    r = client.get(f"/auth/google/callback?error=access_denied&state={state}", follow_redirects=False)
    assert r.headers["location"] == "/login?error=google_cancelled&next=%2Fapp%2Fbilling"
    page = _error_page(client, r.headers["location"], "You stopped at Google.")
    assert "/auth/google/start?next=/app/billing" in page.hrefs and "/" in page.hrefs
    assert saas_db.query(OAuthState).one().used_at is not None  # the attempt is spent

    state, _ = start(client)
    r = client.get(f"/auth/google/callback?error=admin_policy_enforced&state={state}", follow_redirects=False)
    assert r.headers["location"] == "/login?error=google_blocked"
    _error_page(client, r.headers["location"], "Google didn’t allow this sign-in.")


def test_state_mismatch_and_expiry_have_their_own_pages(client, gonly, saas_db, monkeypatch):
    from saas.common import utcnow
    from saas.models.core import OAuthState

    state, _ = start(client, "?next=/app/clips")
    client.cookies.delete("g_state")  # finished in another browser
    r = client.get(f"/auth/google/callback?state={state}&code=x", follow_redirects=False)
    assert r.headers["location"] == "/login?error=oauth_state_mismatch&next=%2Fapp%2Fclips"
    page = _error_page(client, r.headers["location"], "Finish signing in where you started.")
    assert "/auth/google/start?next=/app/clips" in page.hrefs

    from saas.auth import google

    state, _ = start(client)
    row = google.find_state(saas_db, state)
    row.expires_at = utcnow() - timedelta(minutes=1)
    saas_db.commit()
    r = client.get(f"/auth/google/callback?state={state}&code=x", follow_redirects=False)
    assert r.headers["location"] == "/login?error=oauth_state_expired"
    _error_page(client, r.headers["location"], "That sign-in took too long.")

    # Refreshing the callback after a finished sign-in just carries on.
    state, q = start(client, "?next=/app/brand")
    claims = {"iss": "accounts.google.com", "aud": CID, "sub": "g1", "email": "me@gmail.com", "email_verified": True,
              "exp": time.time() + 600, "nonce": q["nonce"][0]}
    monkeypatch.setattr(google, "exchange_code", lambda code, verifier, client=None: {"id_token": _id_token(claims)})
    assert client.get(f"/auth/google/callback?state={state}&code=a", follow_redirects=False).headers["location"] == "/app/brand"
    again = client.get(f"/auth/google/callback?state={state}&code=a", follow_redirects=False)
    assert again.headers["location"] == "/app/brand"


def test_google_failures_and_an_unconfigured_server_are_never_dead_ends(client, gonly, monkeypatch):
    from saas.auth import google
    from saas.auth.errors import ApiError

    def refuse(code, verifier, client=None):
        raise ApiError(400, "oauth_exchange", "Google refused the sign-in. Start again.")

    state, _ = start(client)
    monkeypatch.setattr(google, "exchange_code", refuse)
    r = client.get(f"/auth/google/callback?state={state}&code=a", follow_redirects=False)
    assert r.headers["location"] == "/login?error=google_failed"
    page = _error_page(client, r.headers["location"], "Google sign-in didn’t finish.")
    assert any(h.startswith("/auth/google/start") for h in page.hrefs)
    _error_page(client, "/login?error=something_new", "Google sign-in didn’t finish.")  # unknown codes too
    _error_page(client, "/login?error=invite_required", "Sign-ups are invite-only for now.")

    monkeypatch.setattr(gonly, "google_client_id", "")
    r = client.get("/auth/google/start?next=/app", follow_redirects=False)
    assert r.headers["location"] == "/login?error=google_unavailable"
    page = _error_page(client, r.headers["location"], "Sign-in isn’t set up yet.")
    assert "/" in page.hrefs and "light-1x.png" not in client.get(r.headers["location"]).text
    r = client.get("/auth/google/start", headers={"Accept": "application/json"})
    assert r.status_code == 404 and r.json()["error"] == "google_disabled"
