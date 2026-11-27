// Tests for the beta edge Worker, on the real runtime (workerd via the Miniflare API) with a local D1.
// The Worker is the bundle `wrangler deploy --dry-run` builds from wrangler.toml (nothing is uploaded, no
// account is used). The Workers VPC binding `STUDIO` is replaced by a service binding
// to a plain Node HTTP server (a stand-in for the laptop app), reached over a real HTTP connection, so
// streaming in both directions is exercised for real. "Offline" is a binding to a closed port, which makes
// env.STUDIO.fetch() reject, the same signal Workers VPC gives when the service cannot be reached.
//
// Run: npm test   (from edge-app/)
import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";

import { canonicalIp, hmacHex, verifyHmacHex } from "../src/crypto.js";
import { BMC_SECRET, EDGE_SECRET, PUBLIC, RELAY_TOKEN, disposeAll, makeMf, migrate } from "./helpers.mjs";

const vectors = JSON.parse(readFileSync(new URL("./vectors.json", import.meta.url), "utf8"));

// ---- the stand-in laptop ---------------------------------------------------------------------------
const laptop = { requests: [], handlers: new Map() };
const server = http.createServer((req, res) => {
  const chunks = [];
  const record = { method: req.method, url: req.url, headers: req.headers, rawHeaders: req.rawHeaders, body: null };
  laptop.requests.push(record);
  const handler = laptop.handlers.get(`${req.method} ${req.url.split("?")[0]}`);
  if (handler) return handler(req, res, record);
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    record.body = Buffer.concat(chunks);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ echo: true }));
  });
});

let closedPort;

function withTimeout(promise, ms, what) {
  let t;
  return Promise.race([promise, new Promise((_, rej) => { t = setTimeout(() => rej(new Error(`timed out: ${what}`)), ms); })])
    .finally(() => clearTimeout(t));
}

function rawCount(record, name) {
  return record.rawHeaders.filter((h, i) => i % 2 === 0 && h.toLowerCase() === name).length;
}

const BROWSER = { Accept: "text/html,application/xhtml+xml", "CF-Connecting-IP": "203.0.113.7" };

let mf, base, db;
before(async () => {
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const probe = http.createServer();
  await new Promise((r) => probe.listen(0, "127.0.0.1", r));
  closedPort = probe.address().port;
  await new Promise((r) => probe.close(r));
  mf = makeMf(server.address().port);
  base = (await mf.ready).toString().replace(/\/$/, "");
  db = await migrate(mf);
});

after(async () => {
  await disposeAll();
  await new Promise((r) => server.close(r));
});

// ---- shared vectors (generated from the Python side) -----------------------------------------------
describe("vectors shared with the Python app", () => {
  test("canonical client IPs match Python's ipaddress", () => {
    for (const v of vectors.ip_canonical) assert.equal(canonicalIp(v.in), v.out, v.in);
    for (const bad of vectors.ip_invalid) assert.equal(canonicalIp(bad), null, JSON.stringify(bad));
  });

  test("X-DS-Client-Sig matches saas.auth.edge.client_sig", async () => {
    for (const v of vectors.client_sig) {
      assert.equal(await hmacHex(vectors.edge_secret, `${v.ip}|${v.minute}`), v.sig);
    }
  });

  test("BMC signature check matches providers.verify_bmc_signature", async () => {
    const body = new TextEncoder().encode(vectors.bmc.body);
    assert.equal(await verifyHmacHex(vectors.bmc.secret, body, vectors.bmc.sig), true);
    assert.equal(await verifyHmacHex(vectors.bmc.secret, body, vectors.bmc.sig.replace(/^./, (c) => (c === "0" ? "1" : "0"))), false);
    assert.equal(await verifyHmacHex("other-secret", body, vectors.bmc.sig), false);
    assert.equal(await verifyHmacHex(vectors.bmc.secret, body, vectors.bmc.sig.slice(2)), false);
  });
});

// ---- proxy ----------------------------------------------------------------------------------------
describe("app proxy", () => {
  test("injects the edge headers, strips client copies, keeps the request intact", async () => {
    laptop.requests.length = 0;
    const res = await mf.dispatchFetch(`${PUBLIC}/api/jobs/estimate?x=1&y=two`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json", Cookie: "sid=abc; csrf=t", "X-CSRF-Token": "t",
        Authorization: "Bearer dsk_live_example", "X-Custom": "kept",
        "CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "6.6.6.6", "X-Real-IP": "6.6.6.6",
        Forwarded: "for=6.6.6.6", "True-Client-IP": "6.6.6.6",
        "X-DS-Edge-Auth": "forged", "x-ds-client-ip": "1.1.1.1", "X-Ds-Client-Sig": "f".repeat(64), "X-DS-Extra": "nope",
      },
      body: JSON.stringify({ url: "https://www.youtube.com/watch?v=abc" }),
    });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { echo: true });
    const r = laptop.requests.at(-1);
    assert.equal(r.method, "POST");
    assert.equal(r.url, "/api/jobs/estimate?x=1&y=two");
    assert.equal(r.body.toString(), JSON.stringify({ url: "https://www.youtube.com/watch?v=abc" }));
    assert.equal(r.headers.host, "doomscroller-app.example.workers.dev");
    for (const h of ["x-ds-edge-auth", "x-ds-client-ip", "x-ds-client-sig"]) assert.equal(rawCount(r, h), 1, h);
    assert.equal(rawCount(r, "x-ds-extra"), 0);
    assert.equal(r.headers["x-ds-edge-auth"], EDGE_SECRET);
    assert.equal(r.headers["x-ds-client-ip"], "203.0.113.7");
    const minute = Math.floor(Date.now() / 60000);
    const valid = [minute, minute - 1].map((m) => createHmac("sha256", EDGE_SECRET).update(`203.0.113.7|${m}`).digest("hex"));
    assert.ok(valid.includes(r.headers["x-ds-client-sig"]), "signature is for this minute");
    for (const h of ["x-forwarded-for", "x-real-ip", "forwarded", "true-client-ip", "cf-connecting-ip"]) {
      assert.equal(r.headers[h], undefined, h);
    }
    assert.equal(r.headers.cookie, "sid=abc; csrf=t");
    assert.equal(r.headers["x-csrf-token"], "t");
    assert.equal(r.headers.authorization, "Bearer dsk_live_example");
    assert.equal(r.headers["x-custom"], "kept");
  });

  test("signs the canonical form of an IPv6 visitor", async () => {
    laptop.requests.length = 0;
    await (await mf.dispatchFetch(`${PUBLIC}/app`, { headers: { ...BROWSER, "CF-Connecting-IP": "2001:DB8:0:0:1:0:0:1" } })).text();
    const r = laptop.requests.at(-1);
    assert.equal(r.headers["x-ds-client-ip"], "2001:db8::1:0:0:1");
    const minute = Math.floor(Date.now() / 60000);
    const valid = [minute, minute - 1].map((m) => createHmac("sha256", EDGE_SECRET).update(`2001:db8::1:0:0:1|${m}`).digest("hex"));
    assert.ok(valid.includes(r.headers["x-ds-client-sig"]));
  });

  test("a request without a usable client IP is refused, never forwarded unsigned", async () => {
    laptop.requests.length = 0;
    const res = await mf.dispatchFetch(`${PUBLIC}/api/me`, { headers: { "CF-Connecting-IP": "not-an-ip" } });
    assert.equal(res.status, 400);
    assert.equal((await res.json()).error, "bad_client");
    assert.equal(laptop.requests.length, 0);
  });

  test("streams the response: the first bytes arrive before the app finishes", async () => {
    let release;
    const released = new Promise((r) => { release = r; });
    const first = Buffer.alloc(256 * 1024, 1);
    const second = Buffer.alloc(128 * 1024, 2);
    laptop.handlers.set("GET /api/clips/c1/download", async (req, res) => {
      res.writeHead(200, { "Content-Type": "video/mp4", "Cache-Control": "private, no-store",
        "Content-Disposition": 'attachment; filename="clip.mp4"' });
      res.write(first);
      await released; // only finish once the visitor has seen the first chunk
      res.end(second);
    });
    const got = await withTimeout(new Promise((resolve, reject) => {
      http.get(`${base}/api/clips/c1/download`, { headers: { "CF-Connecting-IP": "203.0.113.7" } }, (res) => {
        let n = 0, sawEarly = false;
        res.on("data", (c) => { if (n === 0) { sawEarly = true; release(); } n += c.length; });
        res.on("end", () => resolve({ n, sawEarly, headers: res.headers, status: res.statusCode }));
      }).on("error", reject);
    }), 10000, "streamed download (a buffering proxy deadlocks here)");
    laptop.handlers.delete("GET /api/clips/c1/download");
    assert.equal(got.status, 200);
    assert.equal(got.sawEarly, true);
    assert.equal(got.n, first.length + second.length);
    assert.equal(got.headers["cache-control"], "private, no-store");
    assert.equal(got.headers["content-type"], "video/mp4");
  });

  test("streams the request: an upload reaches the app while it is still being sent", async () => {
    let gotFirst;
    const firstSeen = new Promise((r) => { gotFirst = r; });
    const part1 = Buffer.alloc(200 * 1024, 7);
    const part2 = Buffer.alloc(100 * 1024, 8);
    laptop.handlers.set("POST /api/uploads", (req, res) => {
      let n = 0;
      req.on("data", (c) => { n += c.length; if (n >= part1.length) gotFirst(); });
      req.on("end", () => {
        res.writeHead(201, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ bytes: n }));
      });
    });
    const result = await withTimeout(new Promise((resolve, reject) => {
      const req = http.request(`${base}/api/uploads`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream", "CF-Connecting-IP": "203.0.113.7" },
      }, (res) => {
        let body = "";
        res.on("data", (c) => { body += c; });
        res.on("end", () => resolve({ status: res.statusCode, body: JSON.parse(body) }));
      });
      req.on("error", reject);
      req.write(part1);
      firstSeen.then(() => req.end(part2)); // the rest is sent only after the app has the first part
    }), 10000, "streamed upload (a buffering proxy deadlocks here)");
    laptop.handlers.delete("POST /api/uploads");
    assert.equal(result.status, 201);
    assert.equal(result.body.bytes, part1.length + part2.length);
  });

  test("passes Range through for video seeking", async () => {
    laptop.handlers.set("GET /api/clips/c2/video", (req, res, record) => {
      record.range = req.headers.range;
      res.writeHead(206, { "Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Content-Range": "bytes 100-199/5000",
        "Content-Length": "100" });
      res.end(Buffer.alloc(100, 3));
    });
    const res = await mf.dispatchFetch(`${PUBLIC}/api/clips/c2/video`, { headers: { Range: "bytes=100-199", "CF-Connecting-IP": "203.0.113.7" } });
    laptop.handlers.delete("GET /api/clips/c2/video");
    assert.equal(res.status, 206);
    assert.equal(res.headers.get("content-range"), "bytes 100-199/5000");
    assert.equal(res.headers.get("accept-ranges"), "bytes");
    assert.equal((await res.arrayBuffer()).byteLength, 100);
    assert.equal(laptop.requests.at(-1).range, "bytes=100-199");
  });

  test("redirects are passed to the browser, not followed; cookies survive; http locations become https", async () => {
    laptop.handlers.set("POST /auth/magic/tok", (req, res) => {
      res.writeHead(303, [["Location", "/app"], ["Set-Cookie", "sid=s1; HttpOnly; Path=/"], ["Set-Cookie", "csrf=c1; Path=/"]]);
      res.end();
    });
    laptop.handlers.set("GET /app/settings", (req, res) => {
      res.writeHead(307, { Location: "http://doomscroller-app.example.workers.dev/login?next=%2Fapp%2Fsettings" });
      res.end();
    });
    laptop.handlers.set("GET /app/old", (req, res) => { res.writeHead(308, { Location: "http://127.0.0.1:8700/app/new" }); res.end(); });
    laptop.handlers.set("GET /auth/google/start", (req, res) => {
      res.writeHead(303, { Location: "https://accounts.google.com/o/oauth2/v2/auth?client_id=x" });
      res.end();
    });
    const a = await mf.dispatchFetch(`${PUBLIC}/auth/magic/tok`, { method: "POST", redirect: "manual", headers: { "CF-Connecting-IP": "203.0.113.7" } });
    assert.equal(a.status, 303);
    assert.equal(a.headers.get("location"), "/app");
    assert.deepEqual(a.headers.getSetCookie(), ["sid=s1; HttpOnly; Path=/", "csrf=c1; Path=/"]);
    const b = await mf.dispatchFetch(`${PUBLIC}/app/settings`, { redirect: "manual", headers: BROWSER });
    assert.equal(b.headers.get("location"), `${PUBLIC}/login?next=%2Fapp%2Fsettings`);
    const c = await mf.dispatchFetch(`${PUBLIC}/app/old`, { redirect: "manual", headers: BROWSER });
    assert.equal(c.headers.get("location"), `${PUBLIC}/app/new`);
    const d = await mf.dispatchFetch(`${PUBLIC}/auth/google/start`, { redirect: "manual", headers: BROWSER });
    assert.equal(d.headers.get("location"), "https://accounts.google.com/o/oauth2/v2/auth?client_id=x");
    for (const k of ["POST /auth/magic/tok", "GET /app/settings", "GET /app/old", "GET /auth/google/start"]) laptop.handlers.delete(k);
  });

  test("the app's own errors pass through untouched (only an unreachable studio is 'offline')", async () => {
    laptop.handlers.set("POST /webhooks/stripe", (req, res) => {
      res.writeHead(503, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "retry_later", message: "subscription not linked yet" }));
    });
    laptop.handlers.set("GET /app/boom", (req, res) => { res.writeHead(500, { "Content-Type": "text/html" }); res.end("<h1>app 500</h1>"); });
    const a = await mf.dispatchFetch(`${PUBLIC}/webhooks/stripe`, { method: "POST", body: "{}", headers: { "CF-Connecting-IP": "203.0.113.7" } });
    assert.equal(a.status, 503);
    assert.deepEqual(await a.json(), { error: "retry_later", message: "subscription not linked yet" });
    const b = await mf.dispatchFetch(`${PUBLIC}/app/boom`, { headers: BROWSER });
    assert.equal(b.status, 500);
    assert.equal(await b.text(), "<h1>app 500</h1>");
    laptop.handlers.delete("POST /webhooks/stripe");
    laptop.handlers.delete("GET /app/boom");
  });

  test("a GET with no answer in time gets the timeout page; a slow POST is never cut off", async () => {
    laptop.handlers.set("GET /app/hang", () => { /* never answers */ });
    laptop.handlers.set("POST /api/jobs/estimate", (req, res) => {
      req.resume();
      req.on("end", () => setTimeout(() => { res.writeHead(200, { "Content-Type": "application/json" }); res.end('{"ok":true}'); }, 2500));
    });
    const a = await mf.dispatchFetch(`${PUBLIC}/app/hang`, { headers: BROWSER });
    assert.equal(a.status, 504);
    assert.match(await a.text(), /not answering/);
    const b = await mf.dispatchFetch(`${PUBLIC}/api/jobs/estimate`, { method: "POST", body: "{}", headers: { "CF-Connecting-IP": "203.0.113.7" } });
    assert.equal(b.status, 200);
    assert.deepEqual(await b.json(), { ok: true });
    laptop.handlers.delete("GET /app/hang");
    laptop.handlers.delete("POST /api/jobs/estimate");
  });
});

describe("studio offline", () => {
  let off;
  before(async () => {
    off = makeMf(closedPort);
    await off.ready;
    await migrate(off);
  });

  test("browsers get the branded offline page, not a Cloudflare error", async () => {
    const res = await off.dispatchFetch(`${PUBLIC}/app`, { headers: BROWSER });
    assert.equal(res.status, 503);
    assert.equal(res.headers.get("retry-after"), "60");
    assert.equal(res.headers.get("cache-control"), "no-store");
    assert.match(res.headers.get("content-type"), /text\/html/);
    assert.match(res.headers.get("content-security-policy"), /default-src 'none'/);
    const html = await res.text();
    assert.match(html, /The studio is offline/);
    assert.match(html, /doomscroller/);
    assert.match(html, /http-equiv="refresh"/);
    assert.match(html, /doomscroller-studio\.pages\.dev/);
  });

  test("API, MCP and file clients get JSON in the app's error envelope", async () => {
    for (const [method, path] of [["GET", "/api/jobs"], ["POST", "/api/v1/jobs"], ["POST", "/mcp"], ["GET", "/files/clips/c1/video"]]) {
      const res = await off.dispatchFetch(`${PUBLIC}${path}`, { method, headers: { ...BROWSER }, body: method === "POST" ? "{}" : undefined });
      assert.equal(res.status, 503, path);
      assert.match(res.headers.get("content-type"), /application\/json/);
      assert.deepEqual(await res.json(), { error: "studio_offline", message: "The studio is offline right now. Try again in a few minutes." });
    }
  });

  test("HEAD gets the status without a body", async () => {
    const res = await off.dispatchFetch(`${PUBLIC}/app`, { method: "HEAD", headers: BROWSER });
    assert.equal(res.status, 503);
    assert.equal((await res.arrayBuffer()).byteLength, 0);
  });

  test("Buy Me a Coffee webhooks are still accepted while the laptop is off", async () => {
    const res = await sendBmc(off, bmcEvent({ id: 777 }));
    assert.equal(res.status, 200);
    assert.equal((await res.json()).stored, true);
  });
});

describe("misconfigured edge fails closed", () => {
  test("no EDGE_SECRET: nothing reaches the app", async () => {
    const bad = makeMf(server.address().port, { EDGE_SECRET: "" });
    laptop.requests.length = 0;
    const res = await bad.dispatchFetch(`${PUBLIC}/app`, { headers: BROWSER });
    assert.equal(res.status, 503);
    assert.match(await res.text(), /The studio is offline/);
    assert.equal(laptop.requests.length, 0);
  });

  test("a short EDGE_SECRET is refused too", async () => {
    const bad = makeMf(server.address().port, { EDGE_SECRET: "short" });
    laptop.requests.length = 0;
    const res = await bad.dispatchFetch(`${PUBLIC}/api/me`, { headers: { "CF-Connecting-IP": "203.0.113.7" } });
    assert.equal(res.status, 503);
    assert.equal(laptop.requests.length, 0);
  });
});

// ---- BMC relay ------------------------------------------------------------------------------------
let delivery = 5000;
function bmcEvent({ id = 9001, type = "extra_purchase.created", live = true, created, eventId, attempt = 1 } = {}) {
  return JSON.stringify({
    event_id: eventId ?? delivery++, type, live_mode: live, created: created ?? Math.floor(Date.now() / 1000), attempt,
    data: { id, status: "succeeded", amount: 35.0, currency: "USD", supporter_email: "buyer@example.com",
      support_note: "café ☕" },
  });
}
function sign(body, secret = BMC_SECRET) {
  return createHmac("sha256", secret).update(Buffer.from(body, "utf8")).digest("hex");
}
function sendBmc(target, body, { signature, headers = {} } = {}) {
  return target.dispatchFetch(`${PUBLIC}/webhooks/bmc`, {
    method: "POST", body,
    headers: { "Content-Type": "application/json", "x-signature-sha256": signature ?? sign(body), ...headers },
  });
}
function relay(target, path, { method = "GET", token = RELAY_TOKEN, body } = {}) {
  return target.dispatchFetch(`${PUBLIC}${path}`, {
    method, body: body === undefined ? undefined : JSON.stringify(body),
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), "Content-Type": "application/json" },
  });
}
async function rows() {
  return (await db.prepare("SELECT * FROM bmc_events ORDER BY seq").all()).results;
}

describe("BMC relay: receive", () => {
  before(async () => { await db.prepare("DELETE FROM bmc_events").run(); });

  test("a valid delivery is stored once, byte-exact, with its signature", async () => {
    const body = bmcEvent({ id: 1 });
    const res = await sendBmc(mf, body);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true, stored: true, duplicate: false });
    const [row] = await rows();
    assert.equal(Buffer.from(row.body_b64, "base64").toString("utf8"), body);
    assert.equal(row.signature, sign(body));
    assert.equal(row.body_sha256, createHash("sha256").update(body).digest("hex"));
    assert.equal(row.type, "extra_purchase.created");
    assert.equal(row.live_mode, 1);
    assert.equal(row.acked_at, null);
    assert.ok(Math.abs(row.received_at - Date.now() / 1000) < 60);
  });

  test("the app never sees a BMC delivery directly", async () => {
    laptop.requests.length = 0;
    await sendBmc(mf, bmcEvent({ id: 2 }));
    await sendBmc(mf, bmcEvent({ id: 3 }), { headers: {} });
    const viaEncoded = await mf.dispatchFetch(`${PUBLIC}/webhooks/%62mc`, { method: "POST", body: "{}" });
    assert.equal(viaEncoded.status, 400); // routed to the relay, which wants a signature
    assert.equal(laptop.requests.length, 0);
  });

  test("a replayed delivery (same bytes) is stored once", async () => {
    const body = bmcEvent({ id: 4 });
    assert.equal((await (await sendBmc(mf, body)).json()).stored, true);
    const again = await (await sendBmc(mf, body)).json();
    assert.deepEqual(again, { ok: true, stored: false, duplicate: true });
    const matching = (await rows()).filter((r) => Buffer.from(r.body_b64, "base64").toString() === body);
    assert.equal(matching.length, 1);
  });

  test("a BMC retry of the same payment is a new delivery; the laptop collapses it", async () => {
    const before = (await rows()).length;
    await sendBmc(mf, bmcEvent({ id: 5, attempt: 1 }));
    await sendBmc(mf, bmcEvent({ id: 5, attempt: 2 })); // new event_id + attempt, same business event
    assert.equal((await rows()).length, before + 2);
  });

  test("invalid, missing, malformed or wrong-secret signatures are refused and not stored", async () => {
    const before = (await rows()).length;
    const body = bmcEvent({ id: 6 });
    for (const signature of [sign(body, "wrong-secret"), "", "zz", sign(body).slice(1), sign(`${body} `)]) {
      const res = await sendBmc(mf, body, { signature });
      assert.equal(res.status, 400, JSON.stringify(signature));
      assert.equal((await res.json()).error, "invalid_signature");
    }
    // Upper-case hex with whitespace is the same signature (Python strips and lower-cases too).
    assert.equal((await sendBmc(mf, body, { signature: ` ${sign(body).toUpperCase()} ` })).status, 200);
    assert.equal((await rows()).length, before + 1);
  });

  test("a stale signed event (a replay of an old capture) is acknowledged but not stored", async () => {
    const before = (await rows()).length;
    const old = bmcEvent({ id: 7, created: Math.floor(Date.now() / 1000) - 73 * 3600 });
    const res = await sendBmc(mf, old);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { ok: true, status: "ignored", detail: "stale event" });
    assert.equal((await rows()).length, before);
  });

  test("dashboard test events (event_id 1, live_mode false) are all kept for the laptop to judge", async () => {
    await sendBmc(mf, bmcEvent({ id: 8, live: false, eventId: 1 }));
    await sendBmc(mf, bmcEvent({ id: 9, live: false, eventId: 1 }));
    const tests = (await rows()).filter((r) => r.live_mode === 0);
    assert.equal(tests.length, 2);
  });

  test("unreadable, oversized and wrong-method requests", async () => {
    const notJson = "not json";
    assert.equal((await sendBmc(mf, notJson)).status, 400);
    const noType = JSON.stringify({ event_id: 1, created: Math.floor(Date.now() / 1000), data: {} });
    assert.equal((await (await sendBmc(mf, noType)).json()).error, "invalid_payload");
    const big = JSON.stringify({ type: "donation.created", created: Math.floor(Date.now() / 1000), pad: "x".repeat(70 * 1024) });
    assert.equal((await sendBmc(mf, big)).status, 413);
    assert.equal((await mf.dispatchFetch(`${PUBLIC}/webhooks/bmc`)).status, 405);
  });

  test("not configured: 503 so BMC retries, nothing stored", async () => {
    const bare = makeMf(server.address().port, { BMC_WEBHOOK_SECRET: "" });
    const res = await sendBmc(bare, bmcEvent({ id: 10 }));
    assert.equal(res.status, 503);
    assert.equal((await res.json()).error, "not_configured");
  });
});

describe("BMC relay: pull and ack", () => {
  before(async () => {
    await db.prepare("DELETE FROM bmc_events").run();
    for (const id of [101, 102, 103]) await sendBmc(mf, bmcEvent({ id }));
  });

  test("pull and ack need the relay token", async () => {
    for (const token of [null, "wrong", `${RELAY_TOKEN}x`, RELAY_TOKEN.slice(0, -1)]) {
      const res = await relay(mf, "/_relay/bmc/events", { token });
      assert.equal(res.status, 401, String(token));
      assert.equal(res.headers.get("www-authenticate"), 'Bearer realm="relay"');
    }
    assert.equal((await relay(mf, "/_relay/bmc/ack", { method: "POST", token: "wrong", body: { acks: [] } })).status, 401);
    const weak = makeMf(server.address().port, { RELAY_TOKEN: "too-short" });
    assert.equal((await relay(weak, "/_relay/bmc/events", { token: "too-short" })).status, 503);
  });

  test("pull returns queued events oldest first, byte-exact, with a cursor; it does not consume them", async () => {
    const a = await (await relay(mf, "/_relay/bmc/events?limit=2")).json();
    assert.equal(a.ok, true);
    assert.equal(a.events.length, 2);
    assert.equal(a.more, true);
    assert.ok(a.events[0].seq < a.events[1].seq);
    const ev = a.events[0];
    const body = Buffer.from(ev.body_b64, "base64").toString("utf8");
    assert.equal(JSON.parse(body).data.id, 101);
    assert.equal(ev.signature, sign(body));
    assert.equal(ev.live_mode, true);
    const b = await (await relay(mf, `/_relay/bmc/events?after=${a.next_after}&limit=2`)).json();
    assert.deepEqual(b.events.map((e) => JSON.parse(Buffer.from(e.body_b64, "base64")).data.id), [103]);
    assert.equal(b.more, false);
    const again = await (await relay(mf, "/_relay/bmc/events")).json();
    assert.equal(again.events.length, 3);
  });

  test("ack removes an event from the queue; an 'error' ack keeps it queued and counts the failure", async () => {
    const { events } = await (await relay(mf, "/_relay/bmc/events")).json();
    const [e1, e2] = events;
    const res = await relay(mf, "/_relay/bmc/ack", { method: "POST", body: { acks: [
      { seq: e1.seq, result: "processed", detail: "pay_1" },
      { seq: e2.seq, result: "error", detail: "internal_error" },
      { seq: 999999, result: "processed" },
    ] } });
    assert.deepEqual(await res.json(), { ok: true, acked: 1, errors_recorded: 1 });
    const left = await (await relay(mf, "/_relay/bmc/events")).json();
    assert.deepEqual(left.events.map((e) => e.seq), [e2.seq, events[2].seq]);
    assert.equal(left.events[0].failures, 1);
    assert.equal(left.events[0].last_error, "internal_error");
    // Acking twice is harmless.
    const twice = await relay(mf, "/_relay/bmc/ack", { method: "POST", body: { acks: [{ seq: e1.seq, result: "processed" }] } });
    assert.deepEqual(await twice.json(), { ok: true, acked: 0, errors_recorded: 0 });
    const [row] = (await db.prepare("SELECT result, detail, acked_at FROM bmc_events WHERE seq = ?").bind(e1.seq).all()).results;
    assert.equal(row.result, "processed");
    assert.equal(row.detail, "pay_1");
    assert.ok(row.acked_at > 0);
  });

  test("malformed acks are refused", async () => {
    for (const body of [{}, { acks: "x" }, { acks: [{ seq: "1", result: "processed" }] }, { acks: [{ seq: 1, result: "Bad Result!" }] },
      { acks: Array.from({ length: 201 }, (_, i) => ({ seq: i + 1, result: "processed" })) }]) {
      assert.equal((await relay(mf, "/_relay/bmc/ack", { method: "POST", body })).status, 400, JSON.stringify(body).slice(0, 60));
    }
    assert.equal((await relay(mf, "/_relay/bmc/nope")).status, 404);
  });

  test("acknowledged events are deleted after the retention period; queued ones never are", async () => {
    const old = Math.floor(Date.now() / 1000) - 91 * 86400;
    await db.prepare("UPDATE bmc_events SET acked_at = ?1, received_at = ?1 WHERE acked_at IS NOT NULL").bind(old).run();
    await db.prepare("UPDATE bmc_events SET received_at = ?1 WHERE acked_at IS NULL").bind(old).run();
    const queuedBefore = (await rows()).filter((r) => r.acked_at === null).length;
    await relay(mf, "/_relay/bmc/ack", { method: "POST", body: { acks: [] } });
    const after = await rows();
    assert.equal(after.filter((r) => r.acked_at !== null).length, 0);
    assert.equal(after.filter((r) => r.acked_at === null).length, queuedBefore);
  });
});
