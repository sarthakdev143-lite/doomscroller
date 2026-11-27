// The Worker in front of the REAL product app in edge mode: saas.main:app under uvicorn from the repo's
// .venv-saas, with SAAS_TRUST_PROXY=edge, a throwaway SQLite file and no env file. This checks the header
// contract against saas/auth/edge.py itself, not only against the shared vectors. Skipped when there is no
// .venv-saas next to edge-app/.
import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { EDGE_SECRET, PUBLIC, disposeAll, makeMf } from "./helpers.mjs";

const REPO = fileURLToPath(new URL("../..", import.meta.url));
const PY = [join(REPO, ".venv-saas", "Scripts", "python.exe"), join(REPO, ".venv-saas", "bin", "python")].find(existsSync);
const skip = PY ? false : "no .venv-saas in the repo";

async function freePort() {
  const s = net.createServer();
  await new Promise((r) => s.listen(0, "127.0.0.1", r));
  const { port } = s.address();
  await new Promise((r) => s.close(r));
  return port;
}

let app, port, mf, dir;
let logs = "";
before(async () => {
  if (!PY) return;
  port = await freePort();
  dir = mkdtempSync(join(tmpdir(), "edge-e2e-"));
  const env = {
    ...process.env,
    DOOMSCROLLER_ENV_FILE: "", SAAS_ENV_FILE: "", SAAS_ENV: "prod", SAAS_ENGINE: "mock",  // prod: what the owner runs
    SAAS_TRUST_PROXY: "edge", SAAS_EDGE_SECRET: EDGE_SECRET, SAAS_PUBLIC_URL: PUBLIC,
    SAAS_DATABASE_URL: `sqlite:///${join(dir, "e2e.db").replace(/\\/g, "/")}`, SAAS_DATA_DIR: join(dir, "data"),
    SAAS_SECRET_KEY: "e2e-secret-key-not-for-production-0123456789", SAAS_HIBP_ENABLED: "false",
    SAAS_TURNSTILE_SECRET_KEY: "", SAAS_SMTP_HOST: "", SAAS_GOOGLE_CLIENT_ID: "",
  };
  app = spawn(PY, ["-m", "uvicorn", "saas.main:app", "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning"],
    { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  app.stdout.on("data", (d) => { logs += d; });
  app.stderr.on("data", (d) => { logs += d; });
  for (let i = 0; ; i++) {
    try {
      await fetch(`http://127.0.0.1:${port}/login`);
      break;
    } catch {
      if (app.exitCode !== null || i > 240) throw new Error(`the app did not start:\n${logs}`);
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  mf = makeMf(port);
  await mf.ready;
});

after(async () => {
  await disposeAll();
  if (app && app.exitCode === null) {
    app.kill();
    await new Promise((r) => app.once("exit", r));
  }
  try {
    if (dir) rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 });
  } catch {
    // Windows can hold the SQLite file for a moment after the process exits; it is a temp dir.
  }
});

const visitor = (ip = "203.0.113.7", extra = {}) => ({ "CF-Connecting-IP": ip, ...extra });

test("in edge mode the app refuses anything that did not come through the Worker", { skip }, async () => {
  const res = await fetch(`http://127.0.0.1:${port}/api/me`);
  assert.equal(res.status, 403);
  assert.equal((await res.json()).error, "edge_only");
});

test("through the Worker the app accepts the signed visitor (401 sign-in needed, not 403)", { skip }, async () => {
  const res = await mf.dispatchFetch(`${PUBLIC}/api/me`, { headers: visitor() });
  assert.equal(res.status, 401, logs);
});

test("client-forged X-DS-* headers never reach the app (it would answer 403 edge_headers)", { skip }, async () => {
  const res = await mf.dispatchFetch(`${PUBLIC}/api/me`, { headers: visitor("203.0.113.7", {
    "X-DS-Edge-Auth": "forged", "X-DS-Client-IP": "1.1.1.1", "X-DS-Client-Sig": "f".repeat(64),
  }) });
  assert.equal(res.status, 401);
});

test("IPv6 and IPv4-mapped visitors verify against Python's canonical form", { skip }, async () => {
  for (const ip of ["2001:DB8:0:0:1:0:0:1", "2001:0db8:0000:0000:0000:ff00:0042:8329", "::ffff:198.51.100.9", "2a06:98c0:3600::103"]) {
    const res = await mf.dispatchFetch(`${PUBLIC}/api/me`, { headers: visitor(ip) });
    assert.equal(res.status, 401, ip);
  }
});

test("pages render through the Worker", { skip }, async () => {
  const res = await mf.dispatchFetch(`${PUBLIC}/login`, { headers: visitor("203.0.113.7", { Accept: "text/html" }) });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type"), /text\/html/);
});

test("with SAAS_PUBLIC_URL set to the Worker origin, the https Origin is same-site; another origin is not", { skip }, async () => {
  const body = JSON.stringify({ email: "nobody@example.com", password: "Correct-Horse-Battery-42",
    fp: "e2e", ts: Date.now() - 6000, website: "" });
  const ours = await mf.dispatchFetch(`${PUBLIC}/auth/login`, { method: "POST", body,
    headers: visitor("198.51.100.20", { "Content-Type": "application/json", Origin: PUBLIC }) });
  assert.notEqual(ours.status, 403, await ours.clone().text());
  assert.equal((await ours.json()).error, "invalid_credentials");
  const theirs = await mf.dispatchFetch(`${PUBLIC}/auth/login`, { method: "POST", body,
    headers: visitor("198.51.100.21", { "Content-Type": "application/json", Origin: "https://evil.example" }) });
  assert.equal(theirs.status, 403);
  assert.equal((await theirs.json()).error, "bad_origin");
});

test("a Worker holding a different secret is refused by the app", { skip }, async () => {
  const wrong = makeMf(port, { EDGE_SECRET: "a-different-secret-0123456789abcdef0123" });
  const res = await wrong.dispatchFetch(`${PUBLIC}/api/me`, { headers: visitor() });
  assert.equal(res.status, 403);
  assert.equal((await res.json()).error, "edge_only");
});
