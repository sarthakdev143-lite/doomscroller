// /admin: a read-only waitlist dashboard + CSV export behind HTTP Basic auth (ADMIN_PASSWORD).
// Put Cloudflare Access (free Zero Trust) in front of /admin* as the first layer; this is the second.
// Read-only on purpose: Basic credentials ride along on cross-site requests, so any state-changing
// admin action would need its own CSRF defence. Edits go through `wrangler d1 execute` for now.
import { clientIp, hmacHex, hostKey, ipSalt, safeEqual } from "./http.js";

const FAIL_LIMIT = 10; // wrong passwords per IP per 15 minutes before a lockout
const FAIL_WINDOW = 900;

export function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function plain(status, text, headers = {}) {
  return new Response(text, {
    status,
    headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex", ...headers },
  });
}

async function failures(env, key) {
  const now = Math.floor(Date.now() / 1000);
  const ws = now - (now % FAIL_WINDOW);
  const rows = await env.DB.prepare(
    "SELECT window_start, count FROM rate_limits WHERE bucket = 'admin_fail' AND key = ?1 AND window_start IN (?2, ?3)",
  ).bind(key, ws, ws - FAIL_WINDOW).all();
  let cur = 0, prev = 0;
  for (const r of rows.results || []) (r.window_start === ws ? (cur = r.count) : (prev = r.count));
  return { estimate: prev * ((FAIL_WINDOW - (now - ws)) / FAIL_WINDOW) + cur, ws };
}

async function recordFailure(env, key, ws) {
  await env.DB.prepare(
    "INSERT INTO rate_limits (bucket, key, window_start, count) VALUES ('admin_fail', ?1, ?2, 1) " +
    "ON CONFLICT (bucket, key, window_start) DO UPDATE SET count = count + 1",
  ).bind(key, ws).run();
}

/** Returns null when the request carries the admin password, else the Response to send. */
export async function guard(request, env) {
  const password = env.ADMIN_PASSWORD || "";
  if (password.length < 12) return plain(503, "Admin is disabled: set the ADMIN_PASSWORD secret (12+ characters).");
  const key = await hmacHex(ipSalt(env, request) || password, `admin:${hostKey(clientIp(request))}`);
  const { estimate, ws } = await failures(env, key);
  // Checked before the password, so a locked-out guesser learns nothing from further tries.
  if (estimate >= FAIL_LIMIT) return plain(429, "Too many failed sign-ins. Try again in 15 minutes.", { "Retry-After": "900" });
  const header = request.headers.get("Authorization") || "";
  let user = "", pass = "";
  if (header.startsWith("Basic ")) {
    try {
      const decoded = new TextDecoder().decode(Uint8Array.from(atob(header.slice(6).trim()), (c) => c.charCodeAt(0)));
      const i = decoded.indexOf(":");
      user = i >= 0 ? decoded.slice(0, i) : decoded;
      pass = i >= 0 ? decoded.slice(i + 1) : "";
    } catch { /* malformed header counts as a failure */ }
  }
  const [okUser, okPass] = await Promise.all([safeEqual(user, env.ADMIN_USER || "admin"), safeEqual(pass, password)]);
  if (okUser && okPass) return null;
  if (header) await recordFailure(env, key, ws);
  return plain(401, "Authentication required.", { "WWW-Authenticate": 'Basic realm="waitlist admin", charset="UTF-8"' });
}

// ---- CSV ------------------------------------------------------------------------------------------
// A cell beginning with = + - @ (or tab / CR, which some apps strip first) runs as a formula in Excel,
// Sheets and Numbers. Every such text cell gets a leading apostrophe; numbers stay numbers.
export function safeCell(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return String(v);
  let s = String(v).replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
  if (/^[=+\-@\t\r|%]/.test(s) || /^\s*[=+@]/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
}

export function toCsv(header, rows) {
  const lines = [header.map(safeCell).join(",")];
  for (const r of rows) lines.push(r.map(safeCell).join(","));
  return "﻿" + lines.join("\r\n") + "\r\n"; // BOM: Excel opens UTF-8 correctly
}

const CSV_COLUMNS = ["seq", "id", "email", "role", "channel_url", "status", "created_at", "referral_code",
  "referrals_counted", "referred_by_seq", "referral_status", "referral_reason", "country", "region", "city", "asn",
  "as_org", "referrer_host", "landing_path", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
  "turnstile", "flagged", "risk"];

export async function exportCsv(env) {
  const rows = await env.DB.prepare(`SELECT ${CSV_COLUMNS.join(", ")} FROM waitlist ORDER BY seq LIMIT 100000`).all();
  const body = toCsv(CSV_COLUMNS, (rows.results || []).map((r) => CSV_COLUMNS.map((c) => r[c])));
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
  return new Response(body, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="waitlist-${stamp}.csv"`,
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex",
    },
  });
}

// ---- dashboard ------------------------------------------------------------------------------------
function bars(rows, labelKey = "k", valueKey = "n") {
  if (!rows.length) return '<p class="empty">Nothing yet.</p>';
  const top = Math.max(...rows.map((r) => r[valueKey])) || 1;
  // The bar is an SVG rect (a width *attribute*), because the admin CSP allows no inline style="".
  return `<ol class="bars">${rows.map((r) => `<li><svg class="bar" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">` +
    `<rect width="${(100 * r[valueKey] / top).toFixed(1)}" height="10"></rect></svg>` +
    `<span class="l">${esc(r[labelKey] ?? "(none)")}</span><b>${esc(r[valueKey])}</b></li>`).join("")}</ol>`;
}

function dailyChart(rows) {
  const days = [];
  const byDay = new Map(rows.map((r) => [r.d, r.n]));
  for (let i = 29; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400e3).toISOString().slice(0, 10);
    days.push({ d, n: byDay.get(d) || 0 });
  }
  const top = Math.max(1, ...days.map((x) => x.n));
  const w = 600, h = 140, bw = w / days.length;
  const rects = days.map((x, i) => {
    const bh = Math.round((h - 20) * x.n / top);
    return `<rect x="${(i * bw + 1).toFixed(1)}" y="${h - 16 - bh}" width="${(bw - 2).toFixed(1)}" height="${bh}"><title>${x.d}: ${x.n}</title></rect>`;
  }).join("");
  const labels = [0, 10, 20, 29].map((i) => `<text x="${(i * bw + bw / 2).toFixed(1)}" y="${h - 3}" text-anchor="middle">${days[i].d.slice(5)}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Joins per day, last 30 days">${rects}${labels}` +
    `<text x="2" y="10">${top}</text></svg>`;
}

async function all(env, sql, ...args) {
  return (await env.DB.prepare(sql).bind(...args).all()).results || [];
}

export async function dashboard(request, env) {
  const url = new URL(request.url);
  const q = (url.searchParams.get("q") || "").trim().toLowerCase().slice(0, 100);
  const dayStart = new Date().toISOString().slice(0, 10);
  const weekAgo = new Date(Date.now() - 7 * 86400e3).toISOString();
  const monthAgo = new Date(Date.now() - 30 * 86400e3).toISOString();
  const totals = await env.DB.prepare(
    `SELECT COUNT(*) AS total,
       SUM(CASE WHEN created_at >= ?1 THEN 1 ELSE 0 END) AS today,
       SUM(CASE WHEN created_at >= ?2 THEN 1 ELSE 0 END) AS week,
       SUM(flagged) AS flagged, SUM(referrals_counted) AS referrals, SUM(turnstile) AS verified,
       SUM(CASE WHEN referral_status = 'rejected' THEN 1 ELSE 0 END) AS rejected
     FROM waitlist WHERE status != 'removed'`,
  ).bind(dayStart, weekAgo).first();
  const [daily, countries, sources, campaigns, roles, referrers, reasons, flagged, recent] = await Promise.all([
    all(env, "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS n FROM waitlist WHERE created_at >= ?1 AND status != 'removed' GROUP BY d", monthAgo),
    all(env, "SELECT COALESCE(country, '??') AS k, COUNT(*) AS n FROM waitlist WHERE status != 'removed' GROUP BY k ORDER BY n DESC LIMIT 15"),
    all(env, "SELECT COALESCE(utm_source, referrer_host, 'direct') AS k, COUNT(*) AS n FROM waitlist WHERE status != 'removed' GROUP BY k ORDER BY n DESC LIMIT 15"),
    all(env, "SELECT utm_campaign AS k, COUNT(*) AS n FROM waitlist WHERE utm_campaign IS NOT NULL AND status != 'removed' GROUP BY k ORDER BY n DESC LIMIT 10"),
    all(env, "SELECT COALESCE(role, '(not given)') AS k, COUNT(*) AS n FROM waitlist WHERE status != 'removed' GROUP BY k ORDER BY n DESC"),
    all(env, "SELECT email, referral_code, referrals_counted FROM waitlist WHERE referrals_counted > 0 ORDER BY referrals_counted DESC, seq LIMIT 15"),
    all(env, "SELECT referral_reason AS k, COUNT(*) AS n FROM waitlist WHERE referral_status = 'rejected' GROUP BY k ORDER BY n DESC"),
    all(env, "SELECT seq, email, created_at, country, as_org, risk FROM waitlist WHERE flagged = 1 AND status != 'removed' ORDER BY seq DESC LIMIT 50"),
    q ? all(env, "SELECT * FROM waitlist WHERE instr(email, ?1) > 0 OR referral_code = ?1 ORDER BY seq DESC LIMIT 100", q)
      : all(env, "SELECT * FROM waitlist ORDER BY seq DESC LIMIT 50"),
  ]);
  const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
  const t = totals || {};
  const stat = (label, v, sub = "") => `<div class="stat"><span>${esc(label)}</span><b>${esc(v ?? 0)}</b>${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>Waitlist admin</title><style nonce="${nonce}">${CSS}</style></head><body>
<header><h1>Waitlist</h1><nav><a href="/admin">Refresh</a><a href="/admin/export.csv">Export CSV</a></nav></header>
<main>
<section class="stats">${stat("Total", t.total)}${stat("Today (UTC)", t.today)}${stat("Last 7 days", t.week)}
${stat("Referrals counted", t.referrals, `${t.rejected || 0} not counted`)}${stat("Flagged", t.flagged, "no referral credit")}
${stat("Passed Turnstile", t.verified)}</section>
<section class="card wide"><h2>Joins per day</h2>${dailyChart(daily)}</section>
<div class="grid">
<section class="card"><h2>Countries</h2>${bars(countries)}</section>
<section class="card"><h2>Source (UTM / referrer)</h2>${bars(sources)}</section>
<section class="card"><h2>Role</h2>${bars(roles)}</section>
<section class="card"><h2>Campaigns</h2>${bars(campaigns)}</section>
<section class="card"><h2>Top referrers</h2>${referrers.length ? `<table><tr><th>Email</th><th>Code</th><th class="n">Counted</th></tr>${referrers.map((r) =>
    `<tr><td>${esc(r.email)}</td><td><code>${esc(r.referral_code)}</code></td><td class="n">${esc(r.referrals_counted)}</td></tr>`).join("")}</table>` : '<p class="empty">No counted referrals yet.</p>'}</section>
<section class="card"><h2>Referrals not counted</h2>${bars(reasons)}</section>
</div>
<section class="card wide"><h2>Flagged (${flagged.length})</h2>${flagged.length ? `<table><tr><th>#</th><th>Email</th><th>When</th><th>Where</th><th>Network</th><th>Why</th></tr>${flagged.map((r) =>
    `<tr><td>${esc(r.seq)}</td><td>${esc(r.email)}</td><td>${esc(r.created_at.slice(0, 16).replace("T", " "))}</td><td>${esc(r.country)}</td><td>${esc(r.as_org)}</td><td>${esc(r.risk)}</td></tr>`).join("")}</table>` : '<p class="empty">Nothing flagged.</p>'}</section>
<section class="card wide"><h2>${q ? `Search: ${esc(q)}` : "Latest 50"}</h2>
<form method="get" action="/admin"><input type="search" name="q" value="${esc(q)}" placeholder="email contains, or referral code" aria-label="Search"><button>Search</button></form>
<div class="scroll"><table><tr><th>#</th><th>Email</th><th>Role</th><th>Channel</th><th>When (UTC)</th><th>Where</th><th>Source</th><th class="n">Refs</th><th>Referred</th><th>Flags</th></tr>
${recent.map((r) => `<tr><td>${esc(r.seq)}</td><td>${esc(r.email)}</td><td>${esc(r.role)}</td><td class="u">${esc(r.channel_url)}</td>` +
    `<td>${esc(r.created_at.slice(0, 16).replace("T", " "))}</td><td>${esc([r.city, r.country].filter(Boolean).join(", "))}</td>` +
    `<td>${esc(r.utm_source || r.referrer_host || "direct")}${r.utm_campaign ? ` / ${esc(r.utm_campaign)}` : ""}</td>` +
    `<td class="n">${esc(r.referrals_counted)}</td><td>${r.referred_by_seq ? `#${esc(r.referred_by_seq)} ${esc(r.referral_status)}${r.referral_reason ? ` (${esc(r.referral_reason)})` : ""}` : ""}</td>` +
    `<td>${r.flagged ? `<span class="flag">${esc(r.risk)}</span>` : ""}</td></tr>`).join("")}</table></div></section>
</main></body></html>`;
  return new Response(html, {
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex, nofollow",
      "Content-Security-Policy": `default-src 'none'; style-src 'nonce-${nonce}'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`,
    },
  });
}

const CSS = `
:root{--paper:#f4f4f0;--surface:#fff;--sunk:#ebebe5;--ink:#111;--ink2:#45453f;--ink3:#5f5f58;--rule:#d4d4cc;--accent:#ffe500;--wash:#fff6b0;--danger:#b42318;--night:#141413;--night-text:#f4f4f0}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 20px;background:var(--night);color:var(--night-text)}
header h1{margin:0;font-size:1.25rem}header h1::before{content:"";display:inline-block;width:10px;height:16px;background:var(--accent);margin-right:10px;border-radius:2px}
nav{display:flex;gap:14px}nav a{color:var(--night-text)}main{padding:20px;max-width:1300px;margin:0 auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:2px}
.stat span{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;font-weight:700;color:var(--ink2)}.stat b{font-size:1.8rem;line-height:1.1}
.stat small{color:var(--ink3)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:16px}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:14px 16px;min-width:0}.card.wide{margin-bottom:16px}
h2{margin:0 0 10px;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}.empty{color:var(--ink3);margin:4px 0}
.bars{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}.bars li{position:relative;display:flex;justify-content:space-between;gap:8px;padding:4px 8px}
.bars .bar{position:absolute;inset:0;width:100%;height:100%}.bars .bar rect{fill:var(--sunk)}.bars li:first-child .bar rect{fill:var(--wash)}.bars .l,.bars b{position:relative}
.bars .l{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.chart{width:100%;height:auto}.chart rect{fill:var(--ink)}.chart text{font-size:10px;fill:var(--ink3)}
table{width:100%;border-collapse:collapse;font-size:.85rem}th{text-align:left;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:var(--ink3);padding:6px 8px;border-bottom:1px solid var(--rule)}
td{padding:6px 8px;border-bottom:1px solid var(--sunk);vertical-align:top}td.n,th.n{text-align:right}td.u{max-width:220px;word-break:break-all}
.scroll{overflow-x:auto}.flag{color:var(--danger);font-weight:600}code{font-size:.8rem}
form{display:flex;gap:8px;margin-bottom:10px}input{flex:1;min-height:38px;padding:6px 10px;border:1px solid #8c8c84;border-radius:6px;font:inherit}
button{min-height:38px;padding:0 14px;border-radius:6px;border:1px solid var(--ink);background:var(--ink);color:#fff;font:inherit;font-weight:700;cursor:pointer}
@media (max-width:600px){main{padding:12px}.stat b{font-size:1.4rem}header{flex-wrap:wrap}}`;
