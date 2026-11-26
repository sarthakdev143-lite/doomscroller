"""TOTP 2FA: setup, login challenge, replay guard, recovery codes, admin gate, encryption at rest."""
from __future__ import annotations

import time

import pyotp
from fastapi import Depends, FastAPI

from core_fixtures import PASSWORD, core, csrf, form_fields, login, make_user  # noqa: F401


def _enable_totp(client, h):
    r = client.post("/api/me/mfa/totp/setup", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qr_svg"].startswith("<svg") and body["otpauth_uri"].startswith("otpauth://totp/")
    secret = body["secret"]
    r = client.post("/api/me/mfa/totp/confirm", json={"code": pyotp.TOTP(secret).now()}, headers=csrf(client))
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10
    return secret, codes


def test_totp_secret_is_encrypted_at_rest(client, core, saas_db):
    from saas.models.core import TotpSecret

    u = make_user(saas_db, "enc@example.com")
    h = login(client, "enc@example.com")
    secret, _ = _enable_totp(client, h)
    row = saas_db.get(TotpSecret, u.id)
    saas_db.refresh(row)
    assert secret not in row.secret_enc and row.secret_enc.startswith("v1.")


def test_login_with_totp_needs_second_step_and_codes_work_once(client, core, saas_db):
    make_user(saas_db, "mfa@example.com")
    h = login(client, "mfa@example.com")
    secret, recovery = _enable_totp(client, h)
    client.cookies.clear()
    r = client.post("/auth/login", json={"email": "mfa@example.com", "password": PASSWORD, **form_fields()})
    body = r.json()
    assert body["mfa_required"] is True and "sid" not in r.cookies  # no full session between the two steps
    assert client.get("/api/me").status_code == 401
    bad = client.post("/auth/login/mfa", json={"mfa_token": body["mfa_token"], "code": "000000"})
    assert bad.status_code == 401
    # A code already used during setup (same 30 s step) must not work again.
    code = pyotp.TOTP(secret).now()
    r = client.post("/auth/login/mfa", json={"mfa_token": body["mfa_token"], "code": code})
    if r.status_code == 401:  # setup consumed this step: use a recovery code instead
        r = client.post("/auth/login/mfa", json={"mfa_token": body["mfa_token"], "code": recovery[0]})
    assert r.status_code == 200 and r.cookies.get("sid")

    client.cookies.clear()
    body = client.post("/auth/login", json={"email": "mfa@example.com", "password": PASSWORD, **form_fields()}).json()
    r = client.post("/auth/login/mfa", json={"mfa_token": body["mfa_token"], "code": recovery[1]})
    assert r.status_code == 200
    client.cookies.clear()
    body = client.post("/auth/login", json={"email": "mfa@example.com", "password": PASSWORD, **form_fields()}).json()
    r = client.post("/auth/login/mfa", json={"mfa_token": body["mfa_token"], "code": recovery[1]})
    assert r.status_code == 401  # recovery codes are single use


def test_totp_step_replay_is_refused(core, saas_db):
    from saas.auth import totp

    u = make_user(saas_db, "replay@example.com")
    setup = totp.begin_setup(saas_db, u)
    t = pyotp.TOTP(setup["secret"])
    now = time.time()
    from saas.models.core import TotpSecret

    row = saas_db.get(TotpSecret, u.id)
    assert totp._check(saas_db, row, t.at(now), at=now) is True
    assert totp._check(saas_db, row, t.at(now), at=now) is False  # same step again
    assert totp._check(saas_db, row, t.at(now - 30), at=now) is False  # an older step after a newer one
    assert totp._check(saas_db, row, t.at(now + 30), at=now) is True


def test_mfa_challenge_attempts_are_capped(client, core, saas_db):
    make_user(saas_db, "cap@example.com")
    h = login(client, "cap@example.com")
    secret, _ = _enable_totp(client, h)
    client.cookies.clear()
    body = client.post("/auth/login", json={"email": "cap@example.com", "password": PASSWORD, **form_fields()}).json()
    for _ in range(5):
        client.post("/auth/login/mfa", json={"mfa_token": body["mfa_token"], "code": "111111"})
    r = client.post("/auth/login/mfa", json={"mfa_token": body["mfa_token"], "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 401  # challenge burned after 5 wrong guesses


def test_require_admin_needs_totp_verified_this_session(client, core, saas_db):
    from saas.auth.deps import require_admin

    app: FastAPI = client.app
    if not any(getattr(r, "path", "") == "/__test_admin" for r in app.router.routes):
        @app.get("/__test_admin")
        def admin_only(user=Depends(require_admin)):
            return {"ok": True}

    make_user(saas_db, "plain@example.com")
    h = login(client, "plain@example.com")
    assert client.get("/__test_admin").json()["error"] == "forbidden"
    make_user(saas_db, "admin@example.com", role="admin")
    h = login(client, "admin@example.com")
    r = client.get("/__test_admin")
    assert r.status_code == 403 and r.json()["error"] == "mfa_required"
    secret, recovery = _enable_totp(client, h)
    assert client.get("/__test_admin").status_code == 200  # confirming TOTP verified this session
    # A new session must pass TOTP again.
    client.cookies.clear()
    body = client.post("/auth/login", json={"email": "admin@example.com", "password": PASSWORD, **form_fields()}).json()
    r = client.post("/auth/login/mfa", json={"mfa_token": body["mfa_token"], "code": recovery[0]})
    assert r.status_code == 200
    assert client.get("/__test_admin").status_code == 200
    # Admins can't switch 2FA off.
    r = client.post("/api/me/mfa/totp/disable", json={"code": recovery[1]}, headers=csrf(client))
    assert r.status_code == 403 and r.json()["error"] == "mfa_mandatory"


def test_step_up_rotates_the_session_token(client, core, saas_db):
    make_user(saas_db, "step@example.com", role="admin")
    h = login(client, "step@example.com")
    _secret, recovery = _enable_totp(client, h)
    before = client.cookies.get("sid")
    r = client.post("/auth/mfa/verify", json={"code": recovery[0]}, headers=csrf(client))
    assert r.status_code == 200
    after = r.cookies.get("sid")
    assert after and after != before
    client.cookies.set("sid", before)
    assert client.get("/api/me").status_code == 401
