// Shared by the edge-app test files: build the Worker exactly as `wrangler deploy` would, and run it on the
// real runtime (workerd, through the Miniflare API) with a local D1 and a stand-in for the VPC binding.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

export const EDGE_SECRET = "edge-secret-for-tests-0123456789abcdef";
export const BMC_SECRET = "bmc-signing-secret-for-tests";
export const RELAY_TOKEN = "relay-token-for-tests-0123456789abcdef0123";
export const PUBLIC = "https://doomscroller-app.example.workers.dev";

const EDGE_APP = fileURLToPath(new URL("..", import.meta.url));
const BUNDLE = fileURLToPath(new URL("../.wrangler/test-bundle/index.js", import.meta.url));
const MIGRATION = readFileSync(new URL("../migrations/0001_bmc_relay.sql", import.meta.url), "utf8");

// Bundle with wrangler.toml through a dry run: nothing is uploaded and no account is touched. This also checks
// that this wrangler accepts the [[vpc_services]] config. Runs once per test process.
let bundled = false;
export function bundle() {
  if (bundled) return;
  const wrangler = fileURLToPath(new URL("../node_modules/wrangler/bin/wrangler.js", import.meta.url));
  const out = spawnSync(process.execPath, [wrangler, "deploy", "--dry-run", "--outdir", ".wrangler/test-bundle"], {
    cwd: EDGE_APP, encoding: "utf8", env: { ...process.env, WRANGLER_SEND_METRICS: "false", CI: "true" },
  });
  if (out.status !== 0) throw new Error(`wrangler dry run failed:\n${out.stdout}\n${out.stderr}`);
  assert.match(out.stdout, /env\.STUDIO .*VPC Service/);
  assert.match(out.stdout, /env\.DB \(doomscroller-relay\)/);
  bundled = true;
}

const instances = [];

// `port`: where the stand-in for the laptop listens. The VPC binding becomes a service binding with the same
// name and the same fetch() interface, over a real HTTP connection (so streaming is real). A closed port makes
// env.STUDIO.fetch() reject, as Workers VPC does when the service cannot be reached.
export function makeMf(port, bindings = {}) {
  bundle();
  // Miniflare 5 (shipped with wrangler 4.161) takes a new option shape; convertV4MiniflareOptions maps the
  // classic per-Worker options onto it.
  const mf = new Miniflare(convertV4MiniflareOptions({
    modules: true,
    scriptPath: BUNDLE,
    compatibilityDate: "2026-11-01",
    bindings: {
      EDGE_SECRET, BMC_WEBHOOK_SECRET: BMC_SECRET, RELAY_TOKEN, BRAND_NAME: "doomscroller",
      PUBLIC_SITE_URL: "https://doomscroller-studio.pages.dev", UPSTREAM_HEADERS_TIMEOUT_MS: "1500",
      BMC_MAX_EVENT_AGE_HOURS: "72", RELAY_RETENTION_DAYS: "90", ...bindings,
    },
    d1Databases: ["DB"],
    serviceBindings: { STUDIO: { external: { address: `127.0.0.1:${port}`, http: {} } } },
  }));
  instances.push(mf);
  return mf;
}

export async function disposeAll() {
  while (instances.length) await instances.pop().dispose();
}

export async function migrate(mf) {
  const db = await mf.getD1Database("DB");
  const sql = MIGRATION.replace(/--[^\n]*/g, "");
  for (const stmt of sql.split(";").map((s) => s.trim()).filter(Boolean)) await db.prepare(stmt).run();
  return db;
}
