// End-to-end tests against the REAL runtime: `wrangler pages dev` (workerd + local D1 via miniflare),
// fresh database, migrations applied with `wrangler d1 migrations apply --local`, then plain HTTP.
// No Cloudflare account needed. Run: npm test   (from edge/)
//
// Each scenario uses its own CF-Connecting-IP (Cloudflare sets that header at the edge; locally it is
// passed through), so per-IP limits and the /24 referral rule can be exercised deterministically.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const EDGE = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WRANGLER = resolve(EDGE, "node_modules/wrangler/bin/wrangler.js");
const STATE = "test/.state";
const PORT = Number(process.env.EDGE_TEST_PORT || 8799);
const BASE = `http://127.0.0.1:${PORT}`;
const ADMIN_PASSWORD = "correct-horse-battery-staple";
const SALT = "test-salt-0123456789abcdef0123";
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const PUBLIC = process.env.EDGE_PUBLIC || (existsSync(resolve(EDGE, "public/index.html")) ? "public" : "test/fixtures/public");

let passed = 0;
const failures = [];
function check(name, cond, detail = "") {
  if (cond) { passed++; console.log(`PASS  ${name}`); }
  else { failures.push(name); console.log(`FAIL  ${name}${detail ? `  -> ${detail}` : ""}`); }
}

function wrangler(args, opts = {}) {
  return spawnSync(process.execPath, [WRANGLER, ...args], { cwd: EDGE, encoding: "utf8", stdio: "pipe", ...opts });
}

function startServer(extraBindings = [], publicDir = PUBLIC) {
  const args = [WRANGLER, "pages", "dev", publicDir, "--port", String(PORT), "--ip", "127.0.0.1", "--persist-to", STATE,
    "--binding", `ADMIN_PASSWORD=${ADMIN_PASSWORD}`, "--binding", `IP_HASH_SALT=${SALT}`, ...extraBindings,
    "--log-level", "warn", "--show-interactive-dev-session=false"];
  const child = spawn(process.execPath, args, { cwd: EDGE, stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  child.stdout.on("data", (d) => { log += d; });
  child.stderr.on("data", (d) => { log += d; });
  child.logs = () => log;
  return child;
}

function stopServer(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  else child.kill("SIGTERM");
}

async function waitReady(child) {
  for (let i = 0; i < 120; i++) {
    try {
      const r = await fetch(`${BASE}/api/waitlist`, { method: "GET" });
      if (r.status === 405) return;
    } catch { /* not yet */ }
    if (child.exitCode !== null) throw new Error(`wrangler exited early:\n${child.logs()}`);
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`server not ready:\n${child.logs()}`);
}

function headers(ip, extra = {}) {
  return { "Content-Type": "application/json", Origin: BASE, "User-Agent": UA, "CF-Connecting-IP": ip, ...extra };
}

async function join(ip, body, extra = {}) {
  const res = await fetch(`${BASE}/api/waitlist`, {
    method: "POST", headers: headers(ip, extra),
    body: JSON.stringify({ ts: Date.now() - 6000, website: "", ...body }),
  });
  let data = null;
  try { data = await res.json(); } catch { /* not json */ }
  return { status: res.status, data, headers: res.headers };
}

async function me(code, ip = "100.64.9.9") {
  const res = await fetch(`${BASE}/api/waitlist/me?code=${encodeURIComponent(code)}`, { headers: { "CF-Connecting-IP": ip } });
  return { status: res.status, data: await res.json() };
}

function basic(pass, user = "admin") {
  return { Authorization: `Basic ${Buffer.from(`${user}:${pass}`).toString("base64")}` };
}

async function admin(path, auth, ip = "100.64.1.1") {
  const res = await fetch(`${BASE}${path}`, { headers: { ...(auth || {}), "CF-Connecting-IP": ip, "User-Agent": UA } });
  return { status: res.status, text: await res.text(), headers: res.headers };
}

// ---------------------------------------------------------------------------------------------------
async function scenarios() {
  // ---- headers & method handling -------------------------------------------------------------------
  const page = await fetch(`${BASE}/`, { headers: { Accept: "text/html" } });
  const html = await page.text();
  const pcsp = page.headers.get("content-security-policy") || "";
  const nonce = (pcsp.match(/'nonce-([^']+)'/) || [])[1];
  check("HTML page has a strict CSP with a nonce", !!nonce && pcsp.includes("frame-ancestors 'none'") &&
    pcsp.includes("https://challenges.cloudflare.com") && pcsp.includes("googletagmanager.com") &&
    pcsp.includes("clarity.ms") && !/script-src[^;]*'unsafe-inline'/.test(pcsp), pcsp);
  const inline = [...html.matchAll(/<script\b(?![^>]*\bsrc=)[^>]*>/gi)].map((m) => m[0]);
  check("every inline script carries this response's nonce", inline.every((t) => t.includes(`nonce="${nonce}"`)),
    inline.join(" "));
  check("HTML has no validators (nonce-safe caching)", !page.headers.get("etag") && !page.headers.get("last-modified"));
  check("nosniff / frame / referrer headers", page.headers.get("x-content-type-options") === "nosniff" &&
    page.headers.get("x-frame-options") === "DENY" && page.headers.get("referrer-policy") === "strict-origin-when-cross-origin");
  const g = await fetch(`${BASE}/api/waitlist`);
  check("GET /api/waitlist is 405", g.status === 405);
  check("API responses are no-store with a deny-all CSP", g.headers.get("cache-control") === "no-store" &&
    (g.headers.get("content-security-policy") || "").startsWith("default-src 'none'"));

  // ---- join ----------------------------------------------------------------------------------------
  const a = await join("203.0.113.1", { email: "  Alice@Company.io ", role: "creator", channel_url: "youtube.com/@alice" },
    { Referer: `${BASE}/?utm_source=newsletter&utm_campaign=launch` });
  check("join works", a.status === 200 && a.data.ok && a.data.already === false && a.data.position === 1 &&
    /\/\?ref=[a-z0-9]{8}$/.test(a.data.referral_url), JSON.stringify(a.data));
  const aCode = a.data?.referral_code;

  const dup = await join("203.0.113.1", { email: "alice@company.io" });
  check("duplicate (same person) returns already + their link", dup.status === 200 && dup.data.already === true &&
    dup.data.referral_url === a.data.referral_url && dup.data.position === 1, JSON.stringify(dup.data));
  const dupOther = await join("198.51.100.200", { email: "ALICE@company.io" });
  check("duplicate from someone else leaks no link", dupOther.data?.already === true && dupOther.data.referral_url === null &&
    dupOther.data.position === null, JSON.stringify(dupOther.data));
  await join("203.0.113.2", { email: "joe@gmail.com" });
  const alias = await join("203.0.113.2", { email: "j.o.e+again@googlemail.com" });
  check("gmail alias counts as already on the list", alias.data?.already === true, JSON.stringify(alias.data));

  // ---- rejections ----------------------------------------------------------------------------------
  const hp = await join("203.0.113.3", { email: "bot@company.io", website: "http://spam.example" });
  check("honeypot gets a fake ok", hp.status === 200 && hp.data.ok === true && hp.data.referral_url === null);
  const fast = await join("203.0.113.3", { email: "fast@company.io", ts: Date.now() - 200 });
  check("too-fast form rejected", fast.status === 400 && fast.data.error === "too_fast", JSON.stringify(fast.data));
  const disp = await join("203.0.113.3", { email: "temp@mailinator.com" });
  check("disposable email rejected", disp.status === 400 && disp.data.error === "disposable_email");
  const disp2 = await join("203.0.113.3", { email: "temp@sub.10minutemail.com" });
  check("disposable subdomain rejected", disp2.status === 400 && disp2.data.error === "disposable_email");
  const bad = await join("203.0.113.4", { email: "not-an-email" });
  check("invalid email rejected", bad.status === 400 && bad.data.error === "invalid_email");
  const xsite = await join("203.0.113.4", { email: "x@company.io" }, { Origin: "https://evil.example" });
  check("cross-site POST refused", xsite.status === 403 && xsite.data.error === "bad_origin");
  const tool = await join("203.0.113.4", { email: "y@company.io" }, { "User-Agent": "python-requests/2.32" });
  check("scripted user-agent refused", tool.status === 403 && tool.data.error === "blocked");
  const big = await fetch(`${BASE}/api/waitlist`, { method: "POST", headers: headers("203.0.113.4"),
    body: JSON.stringify({ email: "z@company.io", pad: "x".repeat(20000) }) });
  check("oversized body rejected", big.status === 413);
  const noJson = await fetch(`${BASE}/api/waitlist`, { method: "POST", headers: headers("203.0.113.4"), body: "{nope" });
  check("malformed JSON rejected", noJson.status === 400);
  const hpRows = await admin("/admin/export.csv", basic(ADMIN_PASSWORD));
  check("rejected joins stored nothing", !/bot@company|fast@company|mailinator|not-an-email|y@company/.test(hpRows.text));

  // ---- rate limits ---------------------------------------------------------------------------------
  const codes = [];
  for (let i = 0; i < 7; i++) codes.push((await join("192.0.2.77", { email: `rl${i}@company.io` })).status);
  check("per-IP rate limit trips (5/min)", codes.slice(0, 5).every((c) => c === 200) && codes[6] === 429, codes.join(","));
  const r429 = await join("192.0.2.77", { email: "rl-late@company.io" });
  check("429 carries Retry-After", r429.status === 429 && Number(r429.headers.get("retry-after")) >= 1 &&
    r429.data.error === "rate_limited");
  const emailCodes = [];
  for (let i = 0; i < 7; i++) emailCodes.push((await join(`100.70.${i}.1`, { email: "same@company.io" })).status);
  check("per-email rate limit trips (5/hour)", emailCodes[6] === 429, emailCodes.join(","));

  // ---- referrals -----------------------------------------------------------------------------------
  // Five people join first, so a bump is visible in the referrer's position.
  for (let i = 0; i < 4; i++) await join(`100.80.${i}.10`, { email: `early${i}@company.io` });
  const r = await join("100.90.1.10", { email: "referrer@company.io" });
  const rCode = r.data.referral_code;
  const before = (await me(rCode)).data.position;
  const f1 = await join("100.91.2.10", { email: "friend1@company.io", ref: rCode });
  let after = await me(rCode);
  check("referral from another network counts", after.data.referrals === 1 && f1.status === 200, JSON.stringify(after.data));
  check("counted referral moves the referrer up", after.data.position < before, `${before} -> ${after.data.position}`);
  await join("100.90.1.99", { email: "samenet@company.io", ref: rCode }); // same /24 as the referrer
  await join("100.91.2.77", { email: "sibling@company.io", ref: rCode }); // same /24 as friend1
  await join("100.95.0.1", { email: "burst1@company.io" });
  await join("100.95.0.1", { email: "burst2@company.io" });
  await join("100.95.0.1", { email: "burst3@company.io", ref: rCode }); // third from one IP within an hour: flagged
  after = await me(rCode);
  check("same-/24, sibling-network and flagged referrals do not count", after.data.referrals === 1, JSON.stringify(after.data));
  const csv = await admin("/admin/export.csv", basic(ADMIN_PASSWORD));
  const line = (email) => csv.text.split("\r\n").find((l) => l.includes(email)) || "";
  check("rejection reasons recorded", line("samenet@company.io").includes("same_network") &&
    line("sibling@company.io").includes("sibling_network") && line("burst3@company.io").includes("flagged"),
    [line("samenet@company.io"), line("sibling@company.io"), line("burst3@company.io")].join(" | "));
  const self = await join("100.96.0.1", { email: "referrer+2@company.io", ref: rCode });
  check("self-referral via alias is just 'already'", self.data?.already === true);

  // ---- user agents ---------------------------------------------------------------------------------
  const cubot = await join("100.101.0.1", { email: "cubot.owner@company.io" }, {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; CUBOT_X30) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36" });
  check("a real phone whose model contains 'bot' is accepted", cubot.status === 200 && cubot.data.ok, JSON.stringify(cubot.data));
  const crawler = await join("100.101.1.1", { email: "crawler@company.io" }, {
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)" });
  const uaCsv = (await admin("/admin/export.csv", basic(ADMIN_PASSWORD))).text.split("\r\n");
  const crawlerRow = uaCsv.find((l) => l.includes("crawler@company.io")) || "";
  const cubotRow = uaCsv.find((l) => l.includes("cubot.owner@company.io")) || "";
  check("a self-declared crawler is accepted but flagged", crawler.status === 200 &&
    crawlerRow.includes("crawler_user_agent") && !cubotRow.includes("crawler_user_agent"), `${crawlerRow} | ${cubotRow}`);
  const curl = await join("100.101.2.1", { email: "curl@company.io" }, { "User-Agent": "curl/8.9.1" });
  check("curl is refused", curl.status === 403 && curl.data.error === "blocked");

  // ---- IPv6 ----------------------------------------------------------------------------------------
  const v6 = await join("2001:db8:aa:1::5", { email: "six@company.io" });
  check("IPv6 join works", v6.status === 200 && v6.data.ok && v6.data.already === false, JSON.stringify(v6.data));
  const v6same = await join("2001:db8:aa:1:ffff::9", { email: "six@company.io" });
  check("same IPv6 /64 is the same visitor (gets their link back)", v6same.data?.already === true &&
    v6same.data.referral_url === v6.data.referral_url, JSON.stringify(v6same.data));
  const v6codes = [];
  for (let i = 1; i <= 7; i++) v6codes.push((await join(`2001:db8:bb:1::${i.toString(16)}`, { email: `v6rl${i}@company.io` })).status);
  check("per-host limit holds across addresses rotated inside one IPv6 /64", v6codes[6] === 429, v6codes.join(","));
  const v6r = await join("2001:db8:cc:1::1", { email: "v6referrer@company.io" });
  await join("2001:db8:dd:1::1", { email: "v6friend@company.io", ref: v6r.data.referral_code });
  await join("2001:db8:cc:2::1", { email: "v6samenet@company.io", ref: v6r.data.referral_code });
  const v6me = await me(v6r.data.referral_code);
  check("IPv6 referrals: other /48 counts, same /48 does not", v6me.data.referrals === 1, JSON.stringify(v6me.data));
  const mapped = await join("::ffff:203.0.113.1", { email: "alice@company.io" });
  check("IPv4-mapped IPv6 is the same client as the IPv4 address", mapped.data?.already === true &&
    mapped.data.referral_url === a.data.referral_url, JSON.stringify(mapped.data));

  // ---- /api/waitlist/me ------------------------------------------------------------------------------
  const m = await me(aCode);
  check("/api/waitlist/me returns position, referrals and link, no email", m.status === 200 && m.data.position >= 1 &&
    m.data.referrals === 0 && m.data.referral_url.endsWith(aCode) && !JSON.stringify(m.data).includes("@"),
    JSON.stringify(m.data));
  check("/api/waitlist/me unknown code is 404", (await me("zzzzzzzz")).status === 404);
  check("/api/waitlist/me malformed code is 404", (await me("<script>")).status === 404);

  // ---- admin ---------------------------------------------------------------------------------------
  const anon = await admin("/admin");
  check("/admin without password is 401 + Basic challenge", anon.status === 401 &&
    (anon.headers.get("www-authenticate") || "").startsWith("Basic"));
  check("/admin/export.csv without password is 401", (await admin("/admin/export.csv")).status === 401);
  check("/admin wrong password is 401", (await admin("/admin", basic("wrong-password-123"))).status === 401);
  const ok = await admin("/admin", basic(ADMIN_PASSWORD));
  check("/admin works with the password", ok.status === 200 && ok.text.includes("Joins per day") &&
    ok.text.includes("alice@company.io") && ok.text.includes("Top referrers"));
  check("/admin is no-store and noindex", ok.headers.get("cache-control") === "no-store" &&
    (ok.headers.get("x-robots-tag") || "").includes("noindex"));
  check("/admin keeps its own strict CSP", (ok.headers.get("content-security-policy") || "").startsWith("default-src 'none'"));
  const search = await admin("/admin?q=friend1", basic(ADMIN_PASSWORD));
  check("/admin search", search.text.includes("friend1@company.io") && !search.text.includes("early0@company.io"));
  const xss = await join("100.97.0.1", { email: "xss@company.io", channel_url: "https://x.io/<script>alert(1)</script>" });
  const esc = await admin("/admin?q=xss", basic(ADMIN_PASSWORD));
  check("/admin escapes stored text", xss.status === 200 && !esc.text.includes("<script>alert(1)</script>"));

  // CSV formula injection: emails and UTM values that a spreadsheet would execute.
  await join("100.98.0.1", { email: "=cmd@company.io" });
  await join("100.98.1.1", { email: "+sum@company.io" });
  await join("100.98.2.1", { email: "-neg@company.io" });
  await join("100.98.3.1", { email: "utm@company.io", page_url: `${BASE}/?utm_source=%40SUM(A1)&utm_campaign=%3DHYPERLINK(%22x%22)` });
  const csvRes = await fetch(`${BASE}/admin/export.csv`, { headers: { ...basic(ADMIN_PASSWORD), "CF-Connecting-IP": "100.64.1.1" } });
  const csvBytes = new Uint8Array(await csvRes.arrayBuffer());
  const csv2 = new TextDecoder().decode(csvBytes); // strips the BOM, which is checked on the raw bytes below
  const cells = csv2.split(/\r\n/).flatMap((l) => l.split(","));
  check("CSV escapes = + - @ at cell start", csv2.includes("'=cmd@company.io") && csv2.includes("'+sum@company.io") &&
    csv2.includes("'-neg@company.io") && csv2.includes("'@SUM(A1)") && csv2.includes("\"'=HYPERLINK(\"\"x\"\")\"") &&
    !cells.some((c) => /^[=+@]/.test(c) || /^-[^0-9]/.test(c)));
  check("CSV starts with a UTF-8 BOM and is an attachment", csvBytes[0] === 0xef && csvBytes[1] === 0xbb &&
    csvBytes[2] === 0xbf && (csvRes.headers.get("content-disposition") || "").startsWith("attachment"));

  // Lockout: 10 wrong passwords from one IP, then even the right one is refused for 15 minutes.
  for (let i = 0; i < 10; i++) await admin("/admin", basic(`wrong-${i}-password`), "100.99.0.1");
  const locked = await admin("/admin", basic(ADMIN_PASSWORD), "100.99.0.1");
  check("/admin locks out after repeated failures", locked.status === 429, String(locked.status));
  check("lockout is per IP", (await admin("/admin", basic(ADMIN_PASSWORD), "100.99.0.2")).status === 200);
}

async function fixtureScenarios() {
  // The fixture page has an inline <script> (like a GA / Clarity bootstrap snippet).
  const r = await fetch(`${BASE}/`, { headers: { Accept: "text/html", "If-None-Match": "\"abc\"" } });
  const html = await r.text();
  const nonce = ((r.headers.get("content-security-policy") || "").match(/'nonce-([^']+)'/) || [])[1];
  check("inline script gets the per-response nonce", r.status === 200 && !!nonce && html.includes(`<script nonce="${nonce}">`),
    html.slice(0, 200));
  const r2 = await fetch(`${BASE}/`, { headers: { Accept: "text/html" } });
  const nonce2 = ((r2.headers.get("content-security-policy") || "").match(/'nonce-([^']+)'/) || [])[1];
  check("nonce is fresh on every response", nonce2 && nonce2 !== nonce);
}

async function turnstileScenarios() {
  // Cloudflare's documented test secret that always passes; tokens are verified over the network.
  const noToken = await join("203.0.113.200", { email: "ts1@company.io" });
  check("Turnstile on: missing token rejected", noToken.status === 400 && noToken.data.error === "turnstile_failed",
    JSON.stringify(noToken.data));
  const withToken = await join("203.0.113.201", { email: "ts2@company.io", cf_turnstile_response: "XXXX.DUMMY.TOKEN.XXXX" });
  if (withToken.status === 400 && withToken.data?.error === "turnstile_failed") {
    console.log("SKIP  Turnstile pass-through (siteverify unreachable from this machine: fails closed as designed)");
  } else {
    check("Turnstile on: valid token accepted (live siteverify)", withToken.status === 200, JSON.stringify(withToken.data));
  }
}

async function main() {
  rmSync(resolve(EDGE, STATE), { recursive: true, force: true });
  const mig = wrangler(["d1", "migrations", "apply", "DB", "--local", "--persist-to", STATE]);
  check("migrations apply to a fresh local D1", mig.status === 0 && mig.stdout.includes("0001_waitlist.sql"),
    mig.stderr || mig.stdout);
  console.log(`serving ${PUBLIC}`);
  let server = startServer();
  try {
    await waitReady(server);
    await scenarios();
  } finally {
    stopServer(server);
  }
  server = startServer(["--binding", "TURNSTILE_SECRET_KEY=1x0000000000000000000000000000000AA"], "test/fixtures/public");
  try {
    await waitReady(server);
    await fixtureScenarios();
    await turnstileScenarios();
  } finally {
    stopServer(server);
  }
  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) process.exit(1);
}

main().catch((err) => { console.error(err); process.exit(1); });
