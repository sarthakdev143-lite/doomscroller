// Buy Me a Coffee webhook relay. BMC must never point at the laptop: deliveries are retried only 4 times
// (15, 30, 45 and 60 minutes later), a 2xx is due within 15 seconds, and after 10 consecutive failures the
// webhook is disabled (https://cdn.buymeacoffee.com/assets/integrations/bmc-webhooks-openapi.json v1.0.0;
// https://help.buymeacoffee.com/en/articles/15743173-how-to-setup-and-use-buy-me-a-coffee-webhooks).
// So the Worker answers BMC, stores the delivery in D1, and the laptop pulls it when it is online
// (saas/billing/bmc_relay.py), re-verifies the signature with the existing Python code and runs it through
// the existing billing path.
//
//   POST /webhooks/bmc                 BMC -> Worker. Verify x-signature-sha256 over the raw body, refuse stale
//                                      events, store the exact bytes once (idempotent on their SHA-256).
//   GET  /_relay/bmc/events?after=&limit=   laptop: unacknowledged events, oldest first (non-destructive).
//   POST /_relay/bmc/ack {acks: [...]} laptop: record outcomes; only an ack removes an event from the queue.
//
// Idempotency is on the body's SHA-256, not on BMC's event_id: event_id identifies a delivery attempt and
// is 1 for every dashboard test event. A BMC retry of the same payment is a new delivery (new event_id,
// new attempt number) and is stored as its own row; the laptop collapses those on the business key
// (saas.billing.bmc.stable_key), so a payment is credited once.

import { bytesToBase64, safeEqual, sha256Hex, verifyHmacHex } from "./crypto.js";
import { json } from "./pages.js";

export const MAX_BODY_BYTES = 64 * 1024;
const MAX_PULL = 100;
const MAX_ACKS = 200;
const RESULT = /^[a-z_]{1,32}$/;

async function readCapped(request, cap) {
  const declared = Number(request.headers.get("content-length") || "0");
  if (declared > cap) return null;
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > cap) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(size);
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.byteLength; }
  return out;
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

// Mirrors the checks in saas/billing/bmc.py::bmc_webhook that need the moment of delivery.
function eventAge(event, now) {
  const created = Number(event.created ?? 0);
  return Number.isFinite(created) ? now - Math.trunc(created) : 1e9;
}

export async function receiveBmc(request, env) {
  if (request.method !== "POST") {
    return json(405, { error: "method_not_allowed", message: "Use POST." }, { Allow: "POST" });
  }
  const secret = env.BMC_WEBHOOK_SECRET || "";
  if (!secret || !env.DB) {
    console.error(`bmc relay misconfigured: ${!secret ? "BMC_WEBHOOK_SECRET missing" : "DB binding missing"}`);
    return json(503, { error: "not_configured", message: "Buy Me a Coffee webhooks are not configured." });
  }
  const raw = await readCapped(request, MAX_BODY_BYTES);
  if (raw === null) return json(413, { error: "payload_too_large", message: "Event too large." });
  const sig = (request.headers.get("x-signature-sha256") || "").trim().toLowerCase();
  if (!(await verifyHmacHex(secret, raw, sig))) {
    return json(400, { error: "invalid_signature", message: "Signature verification failed." });
  }

  let event;
  try {
    event = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
  } catch {
    event = null;
  }
  const data = event && typeof event === "object" ? event.data : undefined;
  if (!event || typeof event !== "object" || Array.isArray(event) || event.type === undefined || event.type === null
      || (data && (typeof data !== "object" || Array.isArray(data)))) {
    return json(400, { error: "invalid_payload", message: "Unreadable event." });
  }

  const now = nowSeconds();
  const maxAge = Number(env.BMC_MAX_EVENT_AGE_HOURS || 72) * 3600;
  if (eventAge(event, now) > maxAge) {
    // BMC's last retry comes 2.5 hours after the first attempt, so a genuine delivery is never this old.
    // An old signed body arriving now is a replay: acknowledge it (so nobody retries) but do not store it.
    return json(200, { ok: true, status: "ignored", detail: "stale event" });
  }

  const created = Number.isFinite(Number(event.created)) ? Math.trunc(Number(event.created)) : null;
  const attempt = Number.isFinite(Number(event.attempt)) ? Math.trunc(Number(event.attempt)) : null;
  try {
    const result = await env.DB.prepare(
      `INSERT INTO bmc_events (body_sha256, event_id, type, live_mode, created, attempt, received_at, signature, body_b64)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
       ON CONFLICT(body_sha256) DO NOTHING`,
    ).bind(await sha256Hex(raw), event.event_id == null ? null : String(event.event_id).slice(0, 64),
      String(event.type).slice(0, 80), event.live_mode === false ? 0 : 1, created, attempt, now, sig,
      bytesToBase64(raw)).run();
    const stored = (result.meta && result.meta.changes) === 1;
    return json(200, { ok: true, stored, duplicate: !stored });
  } catch (err) {
    // 5xx so BMC retries this delivery later.
    console.error(`bmc relay: D1 write failed: ${(err && err.message) || err}`);
    return json(503, { error: "store_failed", message: "Could not store the event right now." });
  }
}

// ---- laptop side: pull and ack -------------------------------------------------------------------

function authorize(request, env) {
  const token = env.RELAY_TOKEN || "";
  if (token.length < 32 || !env.DB) {
    return json(503, { error: "not_configured", message: "The relay is not configured." });
  }
  const m = /^Bearer\s+(\S+)\s*$/i.exec(request.headers.get("authorization") || "");
  if (!safeEqual(m ? m[1] : "", token)) {
    return json(401, { error: "unauthorized", message: "A valid relay token is required." },
      { "WWW-Authenticate": 'Bearer realm="relay"' });
  }
  return null;
}

function intParam(value, fallback, lo, hi) {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : fallback;
}

async function pull(env, url) {
  const after = intParam(url.searchParams.get("after"), 0, 0, Number.MAX_SAFE_INTEGER);
  const limit = intParam(url.searchParams.get("limit"), 50, 1, MAX_PULL);
  const { results } = await env.DB.prepare(
    `SELECT seq, event_id, type, live_mode, created, attempt, received_at, signature, body_b64, failures, last_error
       FROM bmc_events WHERE acked_at IS NULL AND seq > ?1 ORDER BY seq LIMIT ?2`,
  ).bind(after, limit).all();
  const events = (results || []).map((r) => ({ ...r, live_mode: r.live_mode === 1 }));
  return json(200, {
    ok: true,
    events,
    next_after: events.length ? events[events.length - 1].seq : after,
    more: events.length === limit,
  });
}

async function ack(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    body = null;
  }
  const acks = body && Array.isArray(body.acks) ? body.acks : null;
  if (!acks || acks.length > MAX_ACKS || !acks.every((a) => a && Number.isSafeInteger(a.seq) && a.seq > 0
      && typeof a.result === "string" && RESULT.test(a.result))) {
    return json(400, { error: "bad_request", message: `Send {acks: [{seq, result, detail?}]}, at most ${MAX_ACKS}.` });
  }
  const now = nowSeconds();
  const statements = acks.map((a) => {
    const detail = a.detail == null ? null : String(a.detail).slice(0, 500);
    if (a.result === "error") {
      // A failure on the laptop: count it, keep the event queued.
      return env.DB.prepare("UPDATE bmc_events SET failures = failures + 1, last_error = ?2 WHERE seq = ?1 AND acked_at IS NULL")
        .bind(a.seq, detail);
    }
    return env.DB.prepare("UPDATE bmc_events SET acked_at = ?2, result = ?3, detail = ?4 WHERE seq = ?1 AND acked_at IS NULL")
      .bind(a.seq, now, a.result, detail);
  });
  const results = statements.length ? await env.DB.batch(statements) : [];
  let acked = 0, errors = 0;
  results.forEach((r, i) => {
    const changed = (r.meta && r.meta.changes) || 0;
    if (acks[i].result === "error") errors += changed; else acked += changed;
  });
  // Housekeeping: acknowledged events are kept for RELAY_RETENTION_DAYS (they hold supporter emails), then
  // dropped. Unacknowledged events are never deleted.
  const days = intParam(env.RELAY_RETENTION_DAYS, 90, 1, 3650);
  await env.DB.prepare("DELETE FROM bmc_events WHERE acked_at IS NOT NULL AND acked_at < ?1")
    .bind(now - days * 86400).run();
  return json(200, { ok: true, acked, errors_recorded: errors });
}

export async function relayApi(request, env, url, path) {
  const denied = authorize(request, env);
  if (denied) return denied;
  try {
    if (path === "/_relay/bmc/events" && request.method === "GET") return await pull(env, url);
    if (path === "/_relay/bmc/ack" && request.method === "POST") return await ack(request, env);
  } catch (err) {
    console.error(`relay api: ${(err && err.message) || err}`);
    return json(503, { error: "relay_unavailable", message: "The relay store is unavailable." });
  }
  return json(404, { error: "not_found", message: "No such relay endpoint." });
}
