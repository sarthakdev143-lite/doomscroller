"""BMC relay poller (saas.billing.bmc_relay) and the contract vectors shared with the edge Worker (edge-app/).

The fake relay below implements the Worker's pull/ack semantics (edge-app/src/relay.js, tested on the real
runtime by edge-app/test). Here the point is the laptop side: every relayed event is re-verified and judged
exactly like a direct POST /webhooks/bmc, staleness is measured from when the relay received it, and only
definitive outcomes are acknowledged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import itertools
import json
import time
from pathlib import Path

import httpx
import pytest

from core_fixtures import core, make_user  # noqa: F401

SECRET = "bmc_signing_secret"
TOKEN = "relay-token-0123456789abcdef0123456789"
VECTORS = json.loads((Path(__file__).resolve().parents[2] / "edge-app" / "test" / "vectors.json").read_text("utf-8"))
_delivery = itertools.count(7000)


# ---- contract vectors (the JS tests check the same file) -------------------------------------------

def test_vectors_match_the_edge_gate():
    from saas.auth.edge import client_sig, verify_client_ip

    secret = VECTORS["edge_secret"]
    for v in VECTORS["client_sig"]:
        assert client_sig(secret, v["ip"], v["minute"]) == v["sig"]
        assert verify_client_ip(secret, v["ip"], v["sig"], now=v["minute"] * 60 + 30)


def test_ip_vectors_are_what_the_edge_gate_signs_over():
    from saas.auth.edge import client_sig, verify_client_ip

    for v in VECTORS["ip_canonical"]:
        parsed = ipaddress.ip_address(v["in"].strip())
        out = ipaddress.ip_address(v["out"])
        if getattr(parsed, "ipv4_mapped", None) is not None:
            assert parsed.ipv4_mapped == out  # the Worker sends the IPv4 client itself
        else:
            assert str(parsed) == v["out"], v
        # edge.py re-canonicalises X-DS-Client-IP before checking the signature: it must be a fixed point.
        assert str(out) == v["out"]
        sig = client_sig(VECTORS["edge_secret"], v["out"], 1000)
        assert verify_client_ip(VECTORS["edge_secret"], str(ipaddress.ip_address(v["out"])), sig, now=1000 * 60)
    for bad in VECTORS["ip_invalid"]:
        if "%" in bad:
            continue  # zone ids: Python accepts them, the Worker refuses them, Cloudflare never sends one
        with pytest.raises(ValueError):
            ipaddress.ip_address(bad)


def test_bmc_vector_matches_the_existing_verifier():
    from saas.billing import providers

    v = VECTORS["bmc"]
    body = v["body"].encode("utf-8")
    assert providers.verify_bmc_signature(body, v["sig"], v["secret"])
    assert v["sig"] == hmac.new(v["secret"].encode(), body, hashlib.sha256).hexdigest()
    assert not providers.verify_bmc_signature(body + b" ", v["sig"], v["secret"])


# ---- a fake relay with the Worker's pull/ack semantics -------------------------------------------

class FakeRelay:
    def __init__(self):
        self.rows: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_pull = False

    def add(self, body: bytes, signature: str, *, received_at: int | None = None) -> int:
        seq = len(self.rows) + 1
        self.rows.append({"seq": seq, "body_b64": base64.b64encode(body).decode(), "signature": signature,
                          "received_at": int(time.time()) if received_at is None else received_at,
                          "acked_at": None, "result": None, "failures": 0, "last_error": None})
        return seq

    def row(self, seq: int) -> dict:
        return self.rows[seq - 1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if self.fail_pull:
            raise httpx.ConnectError("relay down", request=request)
        if request.method == "GET" and request.url.path == "/_relay/bmc/events":
            after = int(request.url.params.get("after", 0))
            limit = int(request.url.params.get("limit", 50))
            queued = [r for r in self.rows if r["acked_at"] is None and r["seq"] > after][:limit]
            events = [{k: r[k] for k in ("seq", "body_b64", "signature", "received_at", "failures", "last_error")}
                      for r in queued]
            return httpx.Response(200, json={"ok": True, "events": events,
                                             "next_after": queued[-1]["seq"] if queued else after,
                                             "more": len(queued) == limit})
        if request.method == "POST" and request.url.path == "/_relay/bmc/ack":
            acked = errors = 0
            for a in json.loads(request.content)["acks"]:
                r = self.row(a["seq"])
                if r["acked_at"] is not None:
                    continue
                if a["result"] == "error":
                    r["failures"] += 1
                    r["last_error"] = a.get("detail")
                    errors += 1
                else:
                    r["acked_at"], r["result"] = int(time.time()), a["result"]
                    acked += 1
            return httpx.Response(200, json={"ok": True, "acked": acked, "errors_recorded": errors})
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture()
def relay(core, monkeypatch, saas_db):
    from saas.billing import bmc_relay
    from saas.models.billing import Price

    monkeypatch.setattr(core, "bmc_webhook_secret", SECRET)
    monkeypatch.setattr(bmc_relay.relay_settings, "url", "https://doomscroller-app.example.workers.dev/")
    monkeypatch.setattr(bmc_relay.relay_settings, "token", TOKEN)
    monkeypatch.setattr(bmc_relay.relay_settings, "page_size", 50)
    pack = saas_db.query(Price).filter_by(item_kind="pack", item_slug="pack-300", currency="USD", tier="default").one()
    pack.provider_refs = {"bmc_extra_id": 77}
    saas_db.commit()
    fake = FakeRelay()

    def poll():
        return bmc_relay.poll_once(transport=httpx.MockTransport(fake.handler))

    fake.poll = poll
    return fake


def envelope(etype: str, data: dict, *, live: bool = True, created: int | None = None, event_id: int | None = None) -> bytes:
    return json.dumps({"event_id": event_id or next(_delivery), "type": etype, "live_mode": live,
                       "created": int(time.time()) if created is None else created, "attempt": 1,
                       "data": data}).encode()


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def extra(pay_id, email, *, amount=35.0, extra_id=77):
    return {"id": pay_id, "object": "payment", "transaction_id": f"pi_{pay_id}", "status": "succeeded",
            "refunded": "false", "amount": amount, "currency": "USD", "supporter_email": email,
            "support_type": "Extra", "extras": [{"id": extra_id, "object": "extra", "title": "300 minutes",
                                                 "amount": amount, "quantity": 1, "currency": "USD"}]}


# ---- poller -------------------------------------------------------------------------------------

def test_relayed_purchase_is_credited_once_and_acked(relay, saas_db):
    from saas.billing import ledger

    u = make_user(saas_db, "buyer@example.com")
    first = envelope("extra_purchase.created", extra(9001, "Buyer@Example.com"))
    retry = envelope("extra_purchase.created", extra(9001, "buyer@example.com"))  # BMC retry: new event_id
    s1, s2 = relay.add(first, sign(first)), relay.add(retry, sign(retry))
    out = relay.poll()
    assert out["pulled"] == 2 and out["acked"] == 2 and out["errors"] == 0, out
    assert relay.row(s1)["result"] == "processed"
    assert relay.row(s2)["result"] == "duplicate"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 300
    assert relay.poll()["pulled"] == 0  # acked events are not offered again
    # Pulling uses the relay token and the Worker's paths only.
    assert {c[1] for c in relay.calls} == {"/_relay/bmc/events", "/_relay/bmc/ack"}


def test_a_laptop_that_was_off_for_days_still_credits(relay, saas_db, client):
    """The direct route judges age against now; the relay must judge it against receipt, or a sleeping laptop
    silently drops paid events."""
    from saas.billing import ledger

    u = make_user(saas_db, "late@example.com")
    created = int(time.time()) - 5 * 86400
    body = envelope("extra_purchase.created", extra(4242, "late@example.com"), created=created)
    # What the direct route would do with the same bytes today:
    direct = client.post("/webhooks/bmc", content=body, headers={"x-signature-sha256": sign(body)})
    assert direct.json()["detail"] == "stale event"
    seq = relay.add(body, sign(body), received_at=created + 60)
    out = relay.poll()
    assert out["results"] == {"processed": 1}, out
    assert relay.row(seq)["result"] == "processed"
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 300


def test_an_event_already_stale_when_the_relay_got_it_is_ignored(relay, saas_db):
    from saas.billing import ledger

    u = make_user(saas_db, "replay@example.com")
    created = int(time.time()) - 10 * 86400
    body = envelope("extra_purchase.created", extra(5151, "replay@example.com"), created=created)
    seq = relay.add(body, sign(body), received_at=created + 73 * 3600)
    assert relay.poll()["results"] == {"ignored": 1}
    assert relay.row(seq)["result"] == "ignored" and relay.row(seq)["acked_at"]
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 0


def test_a_signature_that_does_not_verify_here_is_never_credited_or_acked(relay, saas_db):
    from saas.billing import ledger

    u = make_user(saas_db, "forged@example.com")
    body = envelope("extra_purchase.created", extra(6161, "forged@example.com"))
    forged = relay.add(body, sign(body, "the-relays-own-secret"))  # e.g. a relay with a different secret
    tampered_body = envelope("extra_purchase.created", extra(6162, "forged@example.com"))
    tampered = relay.add(tampered_body.replace(b"35.0", b"350.0"), sign(tampered_body))
    garbage = relay.add(b"", "zz")
    relay.rows[garbage - 1]["body_b64"] = "%%%not-base64%%%"
    out = relay.poll()
    assert out["errors"] == 3 and out["acked"] == 0, out
    for seq in (forged, tampered, garbage):
        assert relay.row(seq)["acked_at"] is None
        assert relay.row(seq)["failures"] == 1
    assert "BMC_WEBHOOK_SECRET" in relay.row(forged)["last_error"]
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 0
    assert relay.poll()["errors"] == 3  # still queued: fix the secret and the next poll picks them up
    # ...but an unchanged reason is not reported again: one D1 write per stuck event, not one per poll.
    assert [relay.row(s)["failures"] for s in (forged, tampered, garbage)] == [1, 1, 1]
    assert [c for c in relay.calls if c[1] == "/_relay/bmc/ack"] == [("POST", "/_relay/bmc/ack")]


def test_test_events_follow_the_route_rule(relay, saas_db, core, monkeypatch):
    from saas.billing import ledger

    u = make_user(saas_db, "test-evt@example.com")
    body = envelope("extra_purchase.created", extra(1, "test-evt@example.com"), live=False, event_id=1)
    seq = relay.add(body, sign(body))
    assert relay.poll()["results"] == {"ignored": 1}  # dev, SAAS_BMC_ACCEPT_TEST_EVENTS off
    assert relay.row(seq)["result"] == "ignored"
    monkeypatch.setattr(core, "bmc_accept_test_events", True)
    body2 = envelope("extra_purchase.created", extra(2, "test-evt@example.com"), live=False, event_id=1)
    relay.add(body2, sign(body2))
    assert relay.poll()["results"] == {"processed": 1}
    monkeypatch.setattr(core, "env", "prod")  # never in prod, whatever the flag says
    body3 = envelope("extra_purchase.created", extra(3, "test-evt@example.com"), live=False, event_id=1)
    relay.add(body3, sign(body3))
    assert relay.poll()["results"] == {"ignored": 1}
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 300


def test_a_failure_in_the_app_leaves_the_event_queued_and_does_not_block_later_ones(relay, saas_db, monkeypatch):
    from saas.billing import bmc, ledger

    u = make_user(saas_db, "flaky@example.com")
    real = bmc.handle
    broken = {"on": True}

    def handle(db, etype, data, user=None):
        if broken["on"] and data.get("id") == 1:
            raise RuntimeError("database is locked")
        return real(db, etype, data, user)

    monkeypatch.setattr(bmc, "handle", handle)
    seqs = []
    for pay_id in (1, 2, 3):
        b = envelope("extra_purchase.created", extra(pay_id, "flaky@example.com"))
        seqs.append(relay.add(b, sign(b)))
    out = relay.poll()
    assert out["results"] == {"error": 1, "processed": 2}, out
    assert relay.row(seqs[0])["acked_at"] is None and relay.row(seqs[0])["failures"] == 1
    assert "500" in relay.row(seqs[0])["last_error"]
    assert all(relay.row(s)["result"] == "processed" for s in seqs[1:])
    broken["on"] = False
    assert relay.poll()["results"] == {"processed": 1}
    saas_db.expire_all()
    assert ledger.balance(saas_db, u.id) == 900


def test_paging_reaches_every_queued_event(relay, saas_db):
    make_user(saas_db, "many@example.com")
    for pay_id in range(1, 121):
        b = envelope("extra_purchase.created", extra(pay_id, "many@example.com"))
        relay.add(b, sign(b))
    out = relay.poll()
    assert out["pulled"] == 120 and out["acked"] == 120, {k: v for k, v in out.items() if k != "results"}
    assert all(r["acked_at"] for r in relay.rows)


def test_not_configured_or_unreachable_never_raises(relay, core, monkeypatch, saas_db):
    from saas.billing import bmc_relay

    b = envelope("extra_purchase.created", extra(1, "x@example.com"))
    relay.add(b, sign(b))
    relay.fail_pull = True
    out = relay.poll()
    assert out["error"].startswith("relay unreachable") and out["pulled"] == 0
    relay.fail_pull = False
    monkeypatch.setattr(core, "bmc_webhook_secret", "")
    assert "SAAS_BMC_WEBHOOK_SECRET" in relay.poll()["error"]
    assert relay.rows[0]["acked_at"] is None
    monkeypatch.setattr(bmc_relay.relay_settings, "token", "short")
    calls = len(relay.calls)
    assert "skipped" in relay.poll()
    assert len(relay.calls) == calls
    monkeypatch.setattr(bmc_relay.relay_settings, "token", TOKEN)
    monkeypatch.setattr(core, "env", "prod")
    monkeypatch.setattr(bmc_relay.relay_settings, "url", "http://doomscroller-app.example.workers.dev")
    assert not bmc_relay.configured()  # prod refuses a plain-http relay (the token would travel in clear)


def test_run_forever_stops_when_asked(relay, monkeypatch):
    import threading

    from saas.billing import bmc_relay

    polls = []
    stop = threading.Event()

    def fake_poll(**_kw):
        polls.append(1)
        stop.set()
        return {"pulled": 0}

    monkeypatch.setattr(bmc_relay, "poll_once", fake_poll)
    bmc_relay.run_forever(stop, interval=5)
    assert polls == [1]


# ---- parity with the direct webhook route --------------------------------------------------------

@pytest.mark.parametrize("case", ["processed", "deferred_unknown_pack", "deferred_no_account", "rejected",
                                  "test_event", "invalid_payload", "stale"])
def test_route_and_relay_reach_the_same_decision(case, relay, client, saas_db):
    """Same event shape, judged once by POST /webhooks/bmc and once through the relay (distinct payment ids so
    neither run sees the other as a duplicate)."""
    make_user(saas_db, "parity@example.com")

    def body_for(pay_id):
        if case == "processed":
            return envelope("extra_purchase.created", extra(pay_id, "parity@example.com"))
        if case == "deferred_unknown_pack":
            return envelope("extra_purchase.created", extra(pay_id, "parity@example.com", amount=7.0, extra_id=999))
        if case == "deferred_no_account":
            return envelope("extra_purchase.created", extra(pay_id, "nobody@example.com"))
        if case == "rejected":
            return envelope("extra_purchase.created", extra(pay_id, "parity@example.com", amount=1.0))
        if case == "test_event":
            return envelope("extra_purchase.created", extra(pay_id, "parity@example.com"), live=False, event_id=1)
        if case == "invalid_payload":
            return json.dumps({"event_id": pay_id, "type": "extra_purchase.created", "created": int(time.time()),
                               "data": ["not", "an", "object"]}).encode()
        return envelope("extra_purchase.created", extra(pay_id, "parity@example.com"),
                        created=int(time.time()) - 73 * 3600)

    direct_body = body_for(100)
    direct = client.post("/webhooks/bmc", content=direct_body, headers={"x-signature-sha256": sign(direct_body)})
    if direct.status_code == 400:
        route_result = direct.json()["error"]
    else:
        route_result = "duplicate" if direct.json().get("duplicate") else direct.json()["status"]

    relayed = body_for(200)
    seq = relay.add(relayed, sign(relayed))  # received now, like the direct delivery
    relay.poll()
    expected = {"invalid_payload": "invalid_payload"}.get(route_result, route_result)
    assert relay.row(seq)["result"] == expected, (case, route_result, relay.row(seq))
