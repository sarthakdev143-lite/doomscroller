"""Edge waitlist (D1) -> app waitlist bridge: the exact admin.js CSV format, idempotent upserts, flagged
handling, position-ordered invites, and a real signup with an imported invite."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from core_fixtures import PASSWORD, core, form_fields  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
COLUMNS = ["seq", "id", "email", "role", "channel_url", "status", "created_at", "referral_code",
           "referrals_counted", "referred_by_seq", "referral_status", "referral_reason", "country", "region", "city",
           "asn", "as_org", "referrer_host", "landing_path", "utm_source", "utm_medium", "utm_campaign", "utm_term",
           "utm_content", "turnstile", "flagged", "risk"]


def safe_cell(v) -> str:
    """Port of edge/functions/_lib/admin.js safeCell."""
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(v))
    if re.match(r"^[=+\-@\t\r|%]", s) or re.match(r"^\s*[=+@]", s):
        s = "'" + s
    return '"' + s.replace('"', '""') + '"' if re.search(r'[",\r\n]', s) else s


def to_csv(rows: list[dict]) -> bytes:
    """Port of admin.js toCsv: BOM + CRLF."""
    lines = [",".join(safe_cell(c) for c in COLUMNS)]
    lines += [",".join(safe_cell(r.get(c)) for c in COLUMNS) for r in rows]
    return ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")


def edge_row(seq, email, **kw):
    base = {"seq": seq, "id": f"wl_{seq:032x}", "email": email, "role": "creator", "channel_url": None,
            "status": "joined", "created_at": f"2026-11-2{seq % 7}T10:00:0{seq % 10}.123Z",
            "referral_code": f"REF{seq:05d}", "referrals_counted": 0, "referred_by_seq": None,
            "referral_status": None, "referral_reason": None, "country": "IN", "region": None, "city": None,
            "asn": 13335, "as_org": "Cloudflare", "referrer_host": "news.ycombinator.com", "landing_path": "/",
            "utm_source": None, "utm_medium": None, "utm_campaign": None, "utm_term": None, "utm_content": None,
            "turnstile": 1, "flagged": 0, "risk": None}
    base.update(kw)
    return base


@pytest.fixture()
def export(tmp_path):
    rows = [
        edge_row(1, "first@example.com"),
        edge_row(2, "Second@Example.com", role="=HYPERLINK(\"http://evil\",\"x\")", channel_url="https://youtube.com/@a,b"),
        edge_row(3, "third@example.com", referrals_counted=1),  # one counted referral: 3 - 5 = -2, now first
        edge_row(4, "fourth@example.com", referred_by_seq=3, referral_status="counted", utm_source="-launch"),
        edge_row(5, "flagged@example.com", flagged=1, risk="disposable_email,datacenter_ip"),
        edge_row(6, "gone@example.com", status="removed"),
        edge_row(7, "not-an-email", country="US"),
    ]
    path = tmp_path / "waitlist-202611270130.csv"
    path.write_bytes(to_csv(rows))
    return path


def test_csv_format_is_read_back_exactly(export):
    from saas.auth import waitlist_bridge as bridge

    raw = export.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and b"'=HYPERLINK" in raw  # the fixture is escaped
    rows = bridge.read_export(export)
    assert rows[1]["role"] == '=HYPERLINK("http://evil","x")'  # escaping undone
    assert rows[1]["channel_url"] == "https://youtube.com/@a,b"
    assert rows[3]["utm_source"] == "-launch"
    assert bridge.unescape_cell("'plain") == "'plain"  # only formula escapes are stripped
    assert rows[0]["seq"] == "1" and rows[2]["referrals_counted"] == "1"


def test_import_upserts_and_is_idempotent(core, saas_db, export):
    from saas.auth import waitlist_bridge as bridge
    from saas.models.waitlist import WaitlistEntry

    stats = bridge.import_rows(saas_db, bridge.read_export(export))
    saas_db.commit()
    assert stats == {"rows": 7, "created": 5, "updated": 0, "unchanged": 0, "skipped_flagged": 1, "skipped_invalid": 1}
    second = saas_db.query(WaitlistEntry).filter_by(email="second@example.com").one()
    assert second.seq == 2 and second.status == "confirmed" and second.country == "IN"
    assert second.role.startswith("=HYPERLINK") and second.created_at.isoformat().startswith("2026-11-22T10:00:02.123")
    third = saas_db.query(WaitlistEntry).filter_by(email="third@example.com").one()
    fourth = saas_db.query(WaitlistEntry).filter_by(email="fourth@example.com").one()
    assert third.rank_key == 3 - core.waitlist_referral_bump and fourth.referred_by_id == third.id
    assert third.referral_code == "REF00003" and fourth.source["utm_source"] == "-launch"
    assert saas_db.query(WaitlistEntry).filter_by(email="gone@example.com").one().status == "removed"
    assert saas_db.query(WaitlistEntry).filter_by(email="flagged@example.com").one_or_none() is None

    again = bridge.import_rows(saas_db, bridge.read_export(export))
    saas_db.commit()
    assert again["created"] == 0 and again["updated"] == 0 and again["unchanged"] == 5

    with_flagged = bridge.import_rows(saas_db, bridge.read_export(export), include_flagged=True)
    assert with_flagged["created"] == 1
    flagged = saas_db.query(WaitlistEntry).filter_by(email="flagged@example.com").one()
    assert flagged.flagged is True and flagged.risk_reasons == ["disposable_email", "datacenter_ip"]


def test_invites_follow_position_and_survive_reimport(core, saas_db, export):
    from saas.auth import waitlist_bridge as bridge
    from saas.models.waitlist import WaitlistEntry

    bridge.import_rows(saas_db, bridge.read_export(export), include_flagged=True)
    first = bridge.issue_invites(saas_db, 2)
    saas_db.commit()
    # Position: third (rank -2, one referral) first, then first (seq 1). Flagged and removed never.
    assert [r["email"] for r in first] == ["third@example.com", "first@example.com"]
    assert first[0]["signup_url"] == f"{core.public_url}/signup?invite={first[0]['invite_code']}"
    nxt = bridge.issue_invites(saas_db, 10)
    saas_db.commit()
    assert [r["email"] for r in nxt] == ["second@example.com", "fourth@example.com"]
    bridge.import_rows(saas_db, bridge.read_export(export), include_flagged=True)  # re-import keeps invites
    saas_db.commit()
    third = saas_db.query(WaitlistEntry).filter_by(email="third@example.com").one()
    assert third.status == "invited" and third.invite_code == first[0]["invite_code"]
    assert {r["email"] for r in bridge.outstanding_invites(saas_db)} == {
        "third@example.com", "first@example.com", "second@example.com", "fourth@example.com"}
    text = bridge.invites_csv(first)
    assert text.splitlines()[0] == "email,invite_code,signup_url,expires_at" and "third@example.com" in text
    # Someone who asks to leave the waitlist after being invited loses the unused invite.
    rows = bridge.read_export(export)
    for r in rows:
        if r["email"] == "first@example.com":
            r["status"] = "removed"
    bridge.import_rows(saas_db, rows, include_flagged=True)
    saas_db.commit()
    assert saas_db.query(WaitlistEntry).filter_by(email="first@example.com").one().status == "removed"
    assert "first@example.com" not in {r["email"] for r in bridge.outstanding_invites(saas_db)}


def test_imported_invite_works_for_real_signup(client, core, saas_db, export, monkeypatch):
    from saas.auth import waitlist_bridge as bridge
    from saas.models.waitlist import WaitlistEntry

    monkeypatch.setattr(core, "launch_mode", "waitlist")
    bridge.import_rows(saas_db, bridge.read_export(export))
    code = bridge.issue_invites(saas_db, 1)[0]["invite_code"]
    saas_db.commit()
    r = client.post("/auth/signup", json={"email": "third@example.com", "password": PASSWORD, "invite": code,
                                          **form_fields()})
    assert r.status_code == 201, r.text
    saas_db.expire_all()
    assert saas_db.query(WaitlistEntry).filter_by(email="third@example.com").one().status == "joined"


def test_cli_dry_run_writes_nothing_and_real_run_exports(core, saas_db, export, tmp_path, capsys):
    from saas.models.waitlist import WaitlistEntry

    spec = importlib.util.spec_from_file_location("saas_manage", ROOT / "scripts" / "saas_manage.py")
    manage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manage)
    assert manage.main(["import-waitlist", str(export), "--invite", "2", "--dry-run"]) == 0
    saas_db.expire_all()
    assert saas_db.query(WaitlistEntry).count() == 0
    assert "would invite 2" in capsys.readouterr().err
    out = tmp_path / "invites.csv"
    assert manage.main(["import-waitlist", str(export), "--invite", "2", "--out", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and lines[1].startswith("third@example.com,")
    saas_db.expire_all()
    assert saas_db.query(WaitlistEntry).filter_by(status="invited").count() == 2
