"""Auth: signup/verify, enumeration-safe login, throttles, sessions, CSRF, reset, magic link, Google."""
from __future__ import annotations

import base64
import json
import time
from datetime import timedelta

import pytest

from core_fixtures import PASSWORD, core, csrf, form_fields, login, make_user, outbox_link  # noqa: F401


def _signup(client, email, password=PASSWORD, **extra):
    return client.post("/auth/signup", json={"email": email, "password": password, "name": "Test", **form_fields(),
                                             **extra})


# ---- signup & verification ------------------------------------------------------------------------

def test_signup_then_verify_logs_in_same_browser_and_grants_free_credits(client, core, saas_db):
    r = _signup(client, "new.person@example.com")
    assert r.status_code == 201
    assert r.json() == {"ok": True, "verify_required": True}
    assert "sid" not in r.cookies  # signup never logs in: a cookie would reveal "new account"
    token = outbox_link("/auth/verify/")
    r = client.get(f"/auth/verify/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app?verified=1"
    assert client.cookies.get("sid")
    me = client.get("/api/me").json()
    assert me["email_verified"] is True
    assert me["balance"] == core.signup_free_credits
    # Single use: the link can't verify twice.
    r = client.get(f"/auth/verify/{token}", follow_redirects=False)
    assert r.headers["location"] == "/login?verified=1"


def test_verify_link_in_another_browser_does_not_log_in(client, core):
    _signup(client, "other.browser@example.com")
    token = outbox_link("/auth/verify/")
    client.cookies.clear()  # a different browser: no `su` binding cookie
    r = client.get(f"/auth/verify/{token}", follow_redirects=False)
    assert r.headers["location"] == "/login?verified=1"
    assert not client.cookies.get("sid")


def test_duplicate_signup_is_indistinguishable(client, core, saas_db):
    from saas.auth import mailer
    from saas.models.core import User

    first = _signup(client, "dup@example.com")
    mailer.outbox.clear()
    second = _signup(client, "dup@example.com")
    assert second.status_code == first.status_code == 201
    assert second.json() == first.json()
    assert set(second.cookies.keys()) == set(first.cookies.keys())
    assert saas_db.query(User).filter(User.email == "dup@example.com").count() == 1
    assert "already have" in mailer.outbox[-1].subject


def test_signup_rejects_weak_and_breached_passwords(client, core, monkeypatch):
    r = _signup(client, "weak@example.com", password="password123")
    assert r.status_code == 400 and r.json()["error"] == "weak_password"
    r = _signup(client, "weak2@example.com", password="short")
    assert r.json()["error"] == "weak_password"
    from saas.auth import passwords

    monkeypatch.setattr(passwords, "breached_count", lambda pw, **k: 1234)
    r = _signup(client, "breached@example.com", password="Unique-Enough-Passphrase-9")
    assert r.status_code == 400 and r.json()["error"] == "breached_password"


def test_hibp_check_uses_k_anonymity_and_fails_open(core, monkeypatch):
    import hashlib

    import httpx

    from saas.auth import passwords

    monkeypatch.setattr(core, "hibp_enabled", True)
    pw = "Some-Password-For-Hibp-1"
    digest = hashlib.sha1(pw.encode()).hexdigest().upper()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["padding"] = request.headers.get("add-padding")
        return httpx.Response(200, text=f"0000000000000000000000000000000000A:0\r\n{digest[5:]}:42\r\n")

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert passwords.breached_count(pw, client=c) == 42
    assert seen["url"].endswith("/range/" + digest[:5]) and pw not in seen["url"]
    assert seen["padding"] == "true"

    def boom(request):
        raise httpx.ConnectTimeout("down")

    with httpx.Client(transport=httpx.MockTransport(boom)) as c:
        assert passwords.breached_count(pw, client=c) is None  # fail open


def test_waitlist_mode_requires_invite(client, core, monkeypatch):
    from saas.auth import compat

    monkeypatch.setattr(core, "launch_mode", "waitlist")
    monkeypatch.setattr(compat, "redeem_invite", lambda db, code, email: object() if code == "GOOD" else None)
    r = _signup(client, "inv@example.com")
    assert r.status_code == 403 and r.json()["error"] == "invite_required"
    r = _signup(client, "inv@example.com", invite="GOOD")
    assert r.status_code == 201


def test_risk_block_refuses_signup(client, core, monkeypatch):
    from saas.auth import compat

    monkeypatch.setattr(compat, "assess_signup", lambda *a, **k: {"score": 99, "reasons": ["tor"], "block": True,
                                                                   "grant_free_credits": False})
    r = _signup(client, "bot@example.com")
    assert r.status_code == 403 and r.json()["error"] == "signup_blocked"


def test_free_credits_once_per_inbox_and_per_device(client, core, saas_db):
    from saas.auth import service
    from saas.billing import ledger
    from saas.models.core import User

    def verify(email, fp):
        client.cookies.clear()
        r = client.post("/auth/signup", json={"email": email, "password": PASSWORD, **form_fields(), "fp": fp})
        assert r.status_code == 201
        client.get(f"/auth/verify/{outbox_link('/auth/verify/')}", follow_redirects=False)
        saas_db.expire_all()
        return saas_db.query(User).filter(User.email == email).one()

    a = verify("john.doe@gmail.com", "a" * 32)
    b = verify("johndoe+promo@gmail.com", "b" * 32)  # same inbox, different spelling
    c = verify("someone.else@example.com", "a" * 32)  # different inbox, same device
    assert ledger.balance(saas_db, a.id) == core.signup_free_credits
    assert ledger.balance(saas_db, b.id) == 0 and b.flags["free_credits_withheld"] == "inbox_already_granted"
    assert ledger.balance(saas_db, c.id) == 0 and c.flags["free_credits_withheld"] == "fingerprint_already_granted"
    assert service.canonical_email("J.O.H.N.Doe+x@googlemail.com") == "johndoe@gmail.com"


def test_free_credits_withheld_when_risk_says_no(client, core, monkeypatch, saas_db):
    from saas.auth import compat
    from saas.billing import ledger
    from saas.models.core import User

    monkeypatch.setattr(compat, "assess_signup", lambda *a, **k: {"score": 40, "reasons": ["datacenter"],
                                                                   "block": False, "grant_free_credits": False})
    _signup(client, "risky@example.com")
    client.get(f"/auth/verify/{outbox_link('/auth/verify/')}", follow_redirects=False)
    u = saas_db.query(User).filter(User.email == "risky@example.com").one()
    assert ledger.balance(saas_db, u.id) == 0


# ---- login ----------------------------------------------------------------------------------------

def test_login_is_enumeration_safe(client, core, saas_db, monkeypatch):
    from saas.auth import passwords

    make_user(saas_db, "known@example.com")
    calls = []
    real = passwords.verify_password
    monkeypatch.setattr(passwords, "verify_password", lambda h, p: calls.append(h is not None) or real(h, p))
    wrong = client.post("/auth/login", json={"email": "known@example.com", "password": "nope-nope-nope", **form_fields()})
    unknown = client.post("/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope", **form_fields()})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"error": "invalid_credentials", "message": "Email or password is incorrect."}
    assert calls == [True, False]  # the unknown email still paid for one argon2 verify


def test_login_throttles_per_account_and_per_ip(client, core, saas_db, monkeypatch):
    make_user(saas_db, "target@example.com")
    monkeypatch.setattr(core, "login_max_per_account_15m", 3)
    codes = [client.post("/auth/login", json={"email": "target@example.com", "password": "bad-password-x",
                                              **form_fields()}).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    r = client.post("/auth/login", json={"email": "target@example.com", "password": PASSWORD, **form_fields()})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"

    monkeypatch.setattr(core, "login_max_per_account_15m", 100)
    monkeypatch.setattr(core, "login_max_per_ip_15m", 2)
    from saas.auth import compat

    compat._local.reset()
    try:
        from saas.security import limits
        limits.reset_state()
    except ImportError:
        pass
    codes = [client.post("/auth/login", json={"email": f"x{i}@example.com", "password": "bad-password-x",
                                              **form_fields()}).status_code for i in range(3)]
    assert codes[-1] == 429


def test_login_rotates_session_and_logout_revokes(client, core, saas_db):
    make_user(saas_db, "sess@example.com")
    login(client, "sess@example.com")
    first = client.cookies.get("sid")
    # Log in again while presenting the first session: it must be replaced, not reused.
    r = client.post("/auth/login", json={"email": "sess@example.com", "password": PASSWORD, **form_fields()},
                    headers=csrf(client))
    second = r.cookies.get("sid")
    assert second and second != first
    client.cookies.set("sid", first)
    assert client.get("/api/me").status_code == 401  # the presented session was revoked
    client.cookies.set("sid", second)
    assert client.get("/api/me").status_code == 200
    r = client.post("/auth/logout", headers=csrf(client))
    assert r.status_code == 200
    client.cookies.set("sid", second)
    assert client.get("/api/me").status_code == 401


def test_attacker_chosen_session_id_is_never_adopted(client, core, saas_db):
    make_user(saas_db, "fix@example.com")
    client.cookies.set("sid", "attacker-chosen-value")
    r = client.post("/auth/login", json={"email": "fix@example.com", "password": PASSWORD, **form_fields()},
                    headers=csrf(client))
    assert r.cookies.get("sid") not in (None, "attacker-chosen-value")


def test_idle_and_absolute_expiry(client, core, saas_db):
    from saas.common import utcnow
    from saas.models.core import AuthSession

    make_user(saas_db, "idle@example.com")
    login(client, "idle@example.com")
    sess = saas_db.query(AuthSession).one()
    sess.last_seen_at = utcnow() - timedelta(hours=core.session_idle_hours + 1)
    saas_db.commit()
    assert client.get("/api/me").status_code == 401
    sess.last_seen_at = utcnow()
    sess.expires_at = utcnow() - timedelta(seconds=1)
    saas_db.commit()
    assert client.get("/api/me").status_code == 401


def test_csrf_required_for_cookie_authenticated_writes(client, core, saas_db):
    make_user(saas_db, "csrf@example.com")
    login(client, "csrf@example.com")
    client.cookies.delete("csrf")
    r = client.post("/api/me/sessions/revoke-others")
    try:
        from saas.security import middleware  # noqa: F401
        has_csrf = hasattr(middleware, "CSRF_COOKIE")
    except ImportError:
        has_csrf = False
    if not has_csrf:
        pytest.skip("security middleware not installed yet")
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"
    r = client.post("/api/me/sessions/revoke-others", headers=csrf(client))
    assert r.status_code == 200


def test_error_envelope_for_401_and_422(client, core):
    r = client.get("/api/me")
    assert r.status_code == 401 and r.json() == {"error": "unauthenticated", "message": "Sign in to continue."}
    r = client.post("/auth/login", json={"email": "x@example.com"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_request" and "message" in body and body["fields"]


def test_require_user_redirects_pages_to_login():
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from saas.auth.deps import require_user

    app = FastAPI()

    @app.get("/app/thing")
    def page(user=Depends(require_user)):
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/app/thing?x=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fapp%2Fthing%3Fx%3D1"


# ---- reset, magic link ----------------------------------------------------------------------------

def test_forgot_and_reset_revoke_all_sessions(client, core, saas_db):
    make_user(saas_db, "reset@example.com")
    login(client, "reset@example.com")
    old_sid = client.cookies.get("sid")
    r = client.post("/auth/forgot", json={"email": "reset@example.com", **form_fields()}, headers=csrf(client))
    assert r.json() == {"ok": True}
    assert client.post("/auth/forgot", json={"email": "nobody@example.com", **form_fields()},
                       headers=csrf(client)).json() == {"ok": True}
    token = outbox_link("/reset/")
    weak = client.post("/auth/reset", json={"token": token, "password": "short"}, headers=csrf(client))
    assert weak.status_code == 400 and weak.json()["error"] == "weak_password"
    ok = client.post("/auth/reset", json={"token": token, "password": "Brand-New-Passphrase-77"}, headers=csrf(client))
    assert ok.status_code == 200
    again = client.post("/auth/reset", json={"token": token, "password": "Another-Passphrase-88"}, headers=csrf(client))
    assert again.status_code == 400 and again.json()["error"] == "invalid_token"
    client.cookies.set("sid", old_sid)
    assert client.get("/api/me").status_code == 401
    client.cookies.clear()
    r = client.post("/auth/login", json={"email": "reset@example.com", "password": PASSWORD, **form_fields()})
    assert r.status_code == 401
    login(client, "reset@example.com", "Brand-New-Passphrase-77")


def test_magic_link_confirm_page_then_post(client, core, saas_db):
    make_user(saas_db, "magic@example.com")
    assert client.post("/auth/magic", json={"email": "magic@example.com", **form_fields()}).json() == {"ok": True}
    token = outbox_link("/auth/magic/")
    page = client.get(f"/auth/magic/{token}")
    assert page.status_code == 200 and "<form method=\"post\"" in page.text
    assert not client.cookies.get("sid")  # a GET (mail scanner) never signs anyone in
    r = client.post(f"/auth/magic/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert client.get("/api/me").status_code == 200
    client.cookies.clear()
    assert client.post(f"/auth/magic/{token}", follow_redirects=False).headers["location"] == "/login?error=link_expired"


def test_magic_link_for_unknown_email_sends_nothing(client, core):
    from saas.auth import mailer

    mailer.outbox.clear()
    assert client.post("/auth/magic", json={"email": "ghost@example.com", **form_fields()}).json() == {"ok": True}
    assert len(mailer.outbox) == 0


# ---- Google ---------------------------------------------------------------------------------------

def _id_token(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def _google_flow(client, monkeypatch, core, *, email, sub="g-123", email_verified=True, query=""):
    from urllib.parse import parse_qs, urlparse

    from saas.auth import google

    monkeypatch.setattr(core, "google_client_id", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(core, "google_client_secret", "secret")
    r = client.get(f"/auth/google/start{query}", follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    state, nonce = q["state"][0], q["nonce"][0]
    claims = {"iss": "https://accounts.google.com", "aud": core.google_client_id, "sub": sub, "email": email,
              "email_verified": email_verified, "exp": time.time() + 600, "iat": time.time(), "nonce": nonce,
              "name": "G User"}
    seen = {}

    def fake_exchange(code, verifier, client=None):
        seen["verifier"] = verifier
        return {"id_token": _id_token(claims)}

    monkeypatch.setattr(google, "exchange_code", fake_exchange)
    return client.get(f"/auth/google/callback?state={state}&code=abc", follow_redirects=False), seen


def test_google_signup_creates_verified_user(client, core, monkeypatch, saas_db):
    from saas.models.core import Identity, User

    r, seen = _google_flow(client, monkeypatch, core, email="guser@example.com")
    assert r.status_code == 303 and r.headers["location"] == "/app"
    u = saas_db.query(User).filter(User.email == "guser@example.com").one()
    assert u.email_verified_at is not None and u.password_hash is None
    assert saas_db.query(Identity).filter(Identity.user_id == u.id).count() == 1
    assert client.get("/api/me").json()["balance"] == core.signup_free_credits
    assert len(seen["verifier"]) >= 43


def test_google_state_must_match_browser_cookie(client, core, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(core, "google_client_id", "cid")
    monkeypatch.setattr(core, "google_client_secret", "secret")
    r = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.cookies.delete("g_state")  # the callback lands in a different browser (login CSRF)
    r = client.get(f"/auth/google/callback?state={state}&code=x", follow_redirects=False)
    assert r.headers["location"] == "/login?error=oauth_state"


def test_google_rejects_bad_nonce_and_unverified_email(core, monkeypatch):
    from saas.auth import google
    from saas.auth.errors import ApiError

    monkeypatch.setattr(core, "google_client_id", "cid")
    good = {"iss": "accounts.google.com", "aud": "cid", "sub": "1", "email": "a@b.com", "email_verified": True,
            "exp": time.time() + 60, "nonce": "n1"}
    assert google.validate_id_token(_id_token(good), nonce="n1")["sub"] == "1"
    for bad, nonce in (({**good, "nonce": "other"}, "n1"), ({**good, "aud": "evil"}, "n1"),
                       ({**good, "iss": "https://evil"}, "n1"), ({**good, "exp": time.time() - 3600}, "n1"),
                       ({**good, "email_verified": False}, "n1")):
        with pytest.raises(ApiError):
            google.validate_id_token(_id_token(bad), nonce=nonce)


def test_google_pre_hijack_defence_wipes_squatter_password(client, core, monkeypatch, saas_db):
    from saas.models.core import User

    squatter = make_user(saas_db, "victim@example.com", verified=False)
    r, _ = _google_flow(client, monkeypatch, core, email="victim@example.com")
    assert r.status_code == 303
    saas_db.expire_all()
    u = saas_db.get(User, squatter.id)
    assert u.password_hash is None and u.email_verified_at is not None
    client.cookies.clear()
    r = client.post("/auth/login", json={"email": "victim@example.com", "password": PASSWORD, **form_fields()})
    assert r.status_code == 401


# ---- re-auth, account -----------------------------------------------------------------------------

def test_sensitive_actions_need_recent_auth(client, core, saas_db):
    from saas.common import utcnow
    from saas.models.core import AuthSession, User

    u = make_user(saas_db, "del@example.com")
    h = login(client, "del@example.com")
    sess = saas_db.query(AuthSession).one()
    sess.reauth_at = utcnow() - timedelta(hours=1)
    saas_db.commit()
    r = client.post("/api/me/delete", json={"confirm": "DELETE"}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "reauth_required"
    assert client.post("/auth/reauth", json={"password": "wrong-password"}, headers=h).status_code == 401
    assert client.post("/auth/reauth", json={"password": PASSWORD}, headers=h).status_code == 200
    r = client.post("/api/me/delete", json={"confirm": "DELETE"}, headers=h)
    assert r.status_code == 200
    saas_db.expire_all()
    gone = saas_db.get(User, u.id)
    assert gone.status == "deleted" and "del@example.com" not in gone.email and gone.password_hash is None
    assert client.get("/api/me").status_code == 401


def test_session_list_and_revoke_is_scoped_to_owner(client, core, saas_db):
    from saas.models.core import AuthSession

    a = make_user(saas_db, "sa@example.com")
    make_user(saas_db, "sb@example.com")
    login(client, "sa@example.com")
    a_sess = saas_db.query(AuthSession).filter(AuthSession.user_id == a.id).one()
    h = login(client, "sb@example.com")
    r = client.delete(f"/api/me/sessions/{a_sess.id}", headers=h)
    assert r.status_code == 404
    listed = client.get("/api/me/sessions").json()["sessions"]
    assert len(listed) == 1 and listed[0]["current"] is True


def test_export_contains_only_own_data(client, core, saas_db):
    make_user(saas_db, "exp@example.com")
    make_user(saas_db, "other-exp@example.com")
    login(client, "exp@example.com")
    data = client.get("/api/me/export").json()
    assert data["user"]["email"] == "exp@example.com"
    assert "other-exp@example.com" not in json.dumps(data)
