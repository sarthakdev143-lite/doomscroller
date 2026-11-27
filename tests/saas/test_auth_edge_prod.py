"""Production mode and edge trust: init-prod secret, SQLite backups, prod refusing to start without a
secret, the SMTP mail test, and SAAS_TRUST_PROXY=edge (signed client IPs, origin refused)."""
from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import Request

from core_fixtures import core  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SECRET = "edge-secret-for-tests-0123456789abcdef"


def _manage():
    spec = importlib.util.spec_from_file_location("saas_manage_t", ROOT / "scripts" / "saas_manage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- edge trust -----------------------------------------------------------------------------------

@pytest.fixture()
def edge(client, core, monkeypatch):
    from saas.common import client_ip

    monkeypatch.setattr(core, "trust_proxy", "edge")
    monkeypatch.setattr(core, "edge_secret", SECRET)
    if not any(getattr(r, "path", "") == "/__test_ip" for r in client.app.router.routes):
        @client.app.get("/__test_ip")
        def who(request: Request):
            return {"ip": client_ip(request), "leaked": [h for h in request.headers if h.startswith("x-ds-")]}
    return client


def signed(ip: str, *, secret: str = SECRET, minute_offset: int = 0) -> dict:
    from saas.auth import edge as edge_mod

    minute = int(time.time() // 60) + minute_offset
    return {"X-DS-Edge-Auth": secret, "X-DS-Client-IP": ip, "X-DS-Client-Sig": edge_mod.client_sig(secret, ip, minute)}


def test_two_edge_clients_get_their_own_ips(edge):
    a = edge.get("/__test_ip", headers=signed("203.0.113.7")).json()
    b = edge.get("/__test_ip", headers=signed("2001:db8::42")).json()
    assert a == {"ip": "203.0.113.7", "leaked": []} and b["ip"] == "2001:db8::42"
    assert edge.get("/__test_ip", headers=signed("198.51.100.9", minute_offset=-2)).status_code == 200  # clock skew


def test_forged_or_stale_client_ip_is_refused(edge):
    h = signed("203.0.113.7")
    h["X-DS-Client-IP"] = "198.51.100.1"  # someone swapping the IP keeps the old signature
    r = edge.get("/__test_ip", headers=h)
    assert r.status_code == 403 and r.json()["error"] == "bad_client_ip"
    assert edge.get("/__test_ip", headers=signed("203.0.113.7", minute_offset=-5)).status_code == 403
    assert edge.get("/__test_ip", headers=signed("203.0.113.7", secret="wrong-secret")).status_code == 403
    h = signed("203.0.113.7")
    del h["X-DS-Client-Sig"]
    assert edge.get("/__test_ip", headers=h).status_code == 403
    assert edge.get("/__test_ip", headers={**signed("203.0.113.7"), "X-DS-Client-IP": "not-an-ip"}).status_code == 403


def test_origin_without_edge_auth_is_refused(edge, core, monkeypatch):
    r = edge.get("/__test_ip")
    assert r.status_code == 403 and r.json()["error"] == "edge_only"
    h = signed("203.0.113.7")
    h["X-DS-Edge-Auth"] = "guess"
    assert edge.get("/__test_ip", headers=h).status_code == 403
    assert edge.post("/auth/login", json={"email": "a@b.com", "password": "x"}).status_code == 403
    monkeypatch.setattr(core, "edge_secret", "")  # edge mode without a secret fails closed
    assert edge.get("/__test_ip", headers=signed("203.0.113.7")).status_code == 403


def test_edge_headers_are_ignored_when_edge_mode_is_off(client, core):
    from saas.common import client_ip

    if not any(getattr(r, "path", "") == "/__test_ip2" for r in client.app.router.routes):
        @client.app.get("/__test_ip2")
        def who(request: Request):
            return {"ip": client_ip(request)}
    assert client.get("/__test_ip2", headers=signed("203.0.113.7")).json()["ip"] != "203.0.113.7"


# ---- production mode --------------------------------------------------------------------------------

def test_init_prod_writes_a_secret_once_and_never_prints_it(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env.saas"
    env_file.write_text("SAAS_PUBLIC_URL=https://example.com\nSAAS_SECRET_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("SAAS_ENV_FILE", str(env_file))
    manage = _manage()
    assert manage.main(["init-prod"]) == 0
    text = env_file.read_text(encoding="utf-8")
    keys = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("SAAS_SECRET_KEY=")]
    assert len(keys) == 1 and len(keys[0]) >= 48 and "SAAS_PUBLIC_URL=https://example.com" in text
    out = capsys.readouterr()
    assert keys[0] not in out.out + out.err
    with pytest.raises(SystemExit):
        manage.main(["init-prod"])
    assert env_file.read_text(encoding="utf-8") == text  # untouched


def test_prod_refuses_to_start_without_a_secret(core, monkeypatch):
    from saas.auth.crypto import SecretKeyMissing
    from saas.main import create_app

    monkeypatch.setattr(core, "env", "prod")
    monkeypatch.setattr(core, "secret_key", "")
    with pytest.raises(SecretKeyMissing):
        create_app()


def test_sqlite_backup_is_consistent_and_pruned(tmp_path, monkeypatch, core):
    from saas.jobs import backup

    db_file = tmp_path / "live.db"
    con = sqlite3.connect(db_file)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    con.commit()
    url = f"sqlite:///{db_file.as_posix()}"
    dest = tmp_path / "backups"
    made = [backup.run_backup(db_url=url, dest=dest, keep=2) for _ in range(3)]
    con.execute("INSERT INTO t VALUES (999)")  # the live DB keeps working while backed up
    con.commit()
    con.close()
    left = sorted(dest.glob("saas-*.db"))
    assert len(left) == 2 and made[-1] in left and made[0] not in left
    copy = sqlite3.connect(left[-1])
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 500
    copy.close()
    monkeypatch.setattr(core, "database_url", url)
    monkeypatch.setattr(core, "backup_dir", str(dest))
    assert backup.due() is False
    monkeypatch.setattr(core, "backup_every_hours", 0)
    assert backup.due() is True
    with pytest.raises(backup.BackupError):
        backup.run_backup(db_url="postgresql://x/y")


def test_mail_test_reports_smtp_failures_without_secrets(core, monkeypatch, capsys):
    import smtplib

    from saas.auth import mailer

    monkeypatch.setattr(core, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(core, "smtp_user", "owner@gmail.com")
    monkeypatch.setattr(core, "smtp_password", "app-password-secret")
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["addr"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, user, password):
            if password != "right":
                raise smtplib.SMTPAuthenticationError(535, b"bad")

        def send_message(self, msg):
            sent["to"] = msg["To"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    ok, detail = mailer.send_test("me@example.com")
    assert not ok and "App Password" in detail and "app-password-secret" not in detail
    assert sent["addr"] == ("smtp.gmail.com", 587) and sent["tls"] is True
    monkeypatch.setattr(core, "smtp_password", "right")
    assert _manage().main(["mail-test", "me@example.com"]) == 0
    assert sent["to"] == "me@example.com" and "OK: sent via smtp.gmail.com:587" in capsys.readouterr().out
