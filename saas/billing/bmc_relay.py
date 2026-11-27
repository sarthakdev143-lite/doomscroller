"""Pull Buy Me a Coffee events from the edge relay and run them through the existing billing path.

Why: BMC disables a webhook after 10 failed deliveries in a row, and the laptop that runs this app is not always on.
So BMC posts to the edge Worker (edge-app/, `POST /webhooks/bmc`), which checks the signature, stores the exact
bytes in D1 and answers at once. This module pulls those events when the laptop is online and acknowledges each
outcome. Setup: docs/saas/beta-edge.md. Relay endpoints: docs/saas/CONTRACT.md ("BMC relay").

Nothing here trusts the relay. Every event is checked again on this side, the same way `POST /webhooks/bmc`
(saas.billing.bmc.bmc_webhook) checks a direct delivery, and with the same code:
* the stored raw body against its stored `x-signature-sha256`, with `providers.verify_bmc_signature`
  and SAAS_BMC_WEBHOOK_SECRET. A relay that altered or invented an event cannot credit anything;
* the same test-event rule (`live_mode` false is ignored unless SAAS_BMC_ACCEPT_TEST_EVENTS, never in prod);
* the same age limit (SAAS_BMC_MAX_EVENT_AGE_HOURS), measured from the moment the relay *received* the event,
  not from now. A laptop that was off for a week must still credit last week's payments;
* then `webhooks.process_event("bmc", bmc.stable_key(...), ..., bmc.handle)`, so a payment is credited once
  however many deliveries of it the relay holds. The relay dedupes identical bytes only. BMC's retries carry a
  new event_id, so a retry is a different body.

Outcomes. A definitive result (processed, duplicate, deferred, rejected, ignored, invalid_payload) is acked,
and the relay stops offering the event. A failure that may pass is not acked: the app answered 5xx, the
signature does not match (usually the two secrets differ), or the event cannot be decoded. It is retried on
every poll, and reported to the relay as `error` only when its reason changes, which keeps D1 writes flat
while an event is stuck. Paging uses the relay's cursor, so a stuck event never blocks the ones after it.

Settings (SAAS_ env vars or the SaaS env file, like saas.config):
    SAAS_BMC_RELAY_URL           the Worker's origin, e.g. https://doomscroller-app.<account>.workers.dev
    SAAS_BMC_RELAY_TOKEN         the Worker's RELAY_TOKEN secret (32+ characters)
    SAAS_BMC_RELAY_POLL_SECONDS  default 30

Run: `scripts/saas_serve.py` starts `run_forever` in the web process when the relay is configured.
By hand: `python -m saas.billing.bmc_relay --once` (prints a JSON summary) or `--loop`.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import settings
from . import bmc, providers
from .webhooks import process_event

log = logging.getLogger("saas.billing.bmc_relay")

MAX_PAGES = 20  # per poll: 20 x 50 events is far more than a beta will ever queue


class RelaySettings(BaseSettings):
    """Kept here rather than in saas/config.py (integrator-owned); same prefix and env file."""

    model_config = SettingsConfigDict(
        env_prefix="SAAS_BMC_RELAY_",
        env_file=os.environ.get("SAAS_ENV_FILE", ".env.saas") or None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str = ""
    token: str = ""
    poll_seconds: int = 30
    page_size: int = 50
    timeout_s: float = 20.0


relay_settings = RelaySettings()


@dataclass(frozen=True)
class Outcome:
    ack: bool
    result: str
    detail: str = ""


class RelayNotConfigured(RuntimeError):
    pass


def _base_url() -> str:
    return relay_settings.url.strip().rstrip("/")


def configured() -> bool:
    url = _base_url()
    secure = url.startswith("https://") or (not settings.is_prod and url.startswith("http://"))
    return bool(url) and secure and len(relay_settings.token.strip()) >= 32


def _result_name(value: Any) -> str:
    name = re.sub(r"[^a-z_]", "_", str(value or "").lower())[:32]
    return name or "processed"


def apply_event(ev: dict) -> Outcome:
    """Check one relayed event exactly as the direct webhook route would, then process it.

    Raises RelayNotConfigured only when SAAS_BMC_WEBHOOK_SECRET is empty; every other problem is an Outcome.
    """
    secret = settings.bmc_webhook_secret
    if not secret:
        raise RelayNotConfigured("SAAS_BMC_WEBHOOK_SECRET is not set")
    try:
        raw = base64.b64decode(str(ev.get("body_b64") or ""), validate=True)
    except (binascii.Error, ValueError):
        return Outcome(False, "error", "stored body is not valid base64")
    if not providers.verify_bmc_signature(raw, ev.get("signature"), secret):
        return Outcome(False, "error", "signature mismatch: SAAS_BMC_WEBHOOK_SECRET must equal the Worker's "
                                       "BMC_WEBHOOK_SECRET")
    try:
        event = json.loads(raw)
        etype = str(event["type"])
        data = event.get("data") or {}
        if not isinstance(data, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError, AttributeError):
        return Outcome(True, "invalid_payload", "Unreadable event.")
    if event.get("live_mode") is False and (settings.is_prod or not settings.bmc_accept_test_events):
        return Outcome(True, "ignored", "test event")
    try:
        age = int(ev.get("received_at")) - int(event.get("created") or 0)
    except (TypeError, ValueError):
        age = 10**9
    if age > settings.bmc_max_event_age_hours * 3600:
        return Outcome(True, "ignored", "stale event")
    try:
        response = process_event("bmc", bmc.stable_key(etype, data), etype,
                                 lambda db, _payload: bmc.handle(db, etype, data), event)
        body = json.loads(bytes(response.body))
    except Exception as exc:  # process_event already catches; this is belt and braces for a poll loop
        log.exception("relayed BMC event %s failed", ev.get("seq"))
        return Outcome(False, "error", f"{type(exc).__name__}")
    if response.status_code == 200:
        result = "duplicate" if body.get("duplicate") else _result_name(body.get("status"))
        return Outcome(True, result, str(body.get("detail") or ""))
    return Outcome(False, "error", f"{response.status_code} {body.get('error', '')}: {body.get('message', '')}"[:500])


def _client(transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    return httpx.Client(timeout=relay_settings.timeout_s, follow_redirects=False, transport=transport,
                        headers={"Authorization": f"Bearer {relay_settings.token.strip()}",
                                 "User-Agent": "doomscroller-bmc-relay-poller/1"})


def poll_once(*, transport: Optional[httpx.BaseTransport] = None) -> dict:
    """Pull every queued event once, process them in receipt order, ack the outcomes. Returns a summary.

    `transport` is for tests (httpx.MockTransport); production uses the default network transport.
    """
    if not configured():
        return {"skipped": "relay not configured (SAAS_BMC_RELAY_URL / SAAS_BMC_RELAY_TOKEN)"}
    if not settings.bmc_webhook_secret:
        return {"error": "SAAS_BMC_WEBHOOK_SECRET is not set; relayed events stay queued"}
    summary: dict[str, Any] = {"pulled": 0, "acked": 0, "errors": 0, "results": {}}
    base = _base_url()
    c = _client(transport)
    try:
        after = 0
        for _ in range(MAX_PAGES):
            r = c.get(f"{base}/_relay/bmc/events", params={"after": after, "limit": relay_settings.page_size})
            if r.status_code != 200:
                summary["error"] = f"pull answered {r.status_code}"
                break
            page = r.json()
            events = page.get("events") or []
            acks = []
            for ev in events:
                outcome = apply_event(ev)
                summary["pulled"] += 1
                summary["results"][outcome.result] = summary["results"].get(outcome.result, 0) + 1
                detail = outcome.detail[:500] or None
                if outcome.ack:
                    acks.append({"seq": ev["seq"], "result": outcome.result, "detail": detail})
                else:
                    summary["errors"] += 1
                    # Report a failure to the relay only when its reason changes: a stuck event is pulled every
                    # poll, and one D1 write per stuck event per poll would eat the free daily write allowance.
                    if ev.get("last_error") != detail:
                        log.warning("relayed BMC event seq=%s not processed: %s", ev.get("seq"), outcome.detail)
                        acks.append({"seq": ev["seq"], "result": "error", "detail": detail})
            if acks:
                a = c.post(f"{base}/_relay/bmc/ack", json={"acks": acks})
                if a.status_code != 200:
                    # Already processed here; the next poll sees them again and they come back as duplicates.
                    summary["error"] = f"ack answered {a.status_code}"
                    break
                summary["acked"] += int(a.json().get("acked") or 0)
            after = int(page.get("next_after") or after)
            if not page.get("more"):
                break
    except RelayNotConfigured as exc:
        summary["error"] = str(exc)
    except (httpx.HTTPError, ValueError) as exc:
        summary["error"] = f"relay unreachable: {type(exc).__name__}"
    finally:
        c.close()
    return summary


def run_forever(stop: threading.Event, interval: Optional[float] = None) -> None:
    """Poll until `stop` is set. Started by scripts/saas_serve.py in the web process.

    Prints to the saas_serve console like its other "[saas]" lines (that process configures no logging, so
    INFO records would be dropped): a line when events were handled, and an error only when it changes.
    """
    every = max(5.0, float(interval if interval is not None else relay_settings.poll_seconds))
    last_error = None
    while not stop.is_set():
        try:
            out = poll_once()
            if out.get("acked"):
                print(f"[saas] bmc relay: {out['pulled']} pulled, {out['acked']} acked {out['results']}", flush=True)
            err = out.get("error")
            if err and err != last_error:
                print(f"[saas] bmc relay: {err}", flush=True)
            last_error = err
        except Exception:
            log.exception("bmc relay poll failed")
        stop.wait(every)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Pull Buy Me a Coffee events from the edge relay.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="poll once and print a JSON summary (default)")
    mode.add_argument("--loop", action="store_true", help="poll every SAAS_BMC_RELAY_POLL_SECONDS until Ctrl+C")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from ..db import init_db

    init_db()
    if args.loop:
        stop = threading.Event()
        try:
            run_forever(stop)
        except KeyboardInterrupt:
            stop.set()
        return 0
    out = poll_once()
    print(json.dumps(out, indent=2))
    return 1 if out.get("error") else 0


if __name__ == "__main__":
    os.environ["DOOMSCROLLER_ENV_FILE"] = ""  # never the owner's .env, as scripts/saas_serve.py does
    raise SystemExit(main())
