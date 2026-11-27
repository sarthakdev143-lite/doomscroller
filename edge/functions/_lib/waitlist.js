// Waitlist logic for the edge: join, position, referrals. Mirrors saas/waitlist/service.py minus the
// double opt-in (no sending domain at launch): entries are 'joined' immediately.
//
// Referral rules (a counted referral moves the referrer up REFERRAL_BUMP places):
//   - the referred entry is not flagged (datacenter/VPN network, tool user-agent, burst from one IP ...);
//   - it comes from a different network (/24 IPv4, /48 IPv6) than the referrer;
//   - no referral already counted for that referrer came from the same network (one person farming
//     many addresses from one connection);
//   - it is not an alias of the referrer's own address;
//   - at most 25 counted referrals per referrer per day.
import { canonical, domainOf, isDisposable, normalise } from "./email.js";
import { clientIp, fail, hmacHex, hostKey, ipSalt, isLocal, json, networkOf, randomCode, randomId, rateLimit,
  readBody, sameOrigin, tooMany } from "./http.js";

const ROLES = new Set(["creator", "agency", "editor", "marketer", "developer", "podcaster", "educator", "business",
  "other"]);
const MIN_FILL_MS = 1200;
const DAILY_REFERRAL_CAP = 25;

// Hosting / VPN networks: people sit behind residential and mobile ISPs, scripts behind these.
const HOSTING_ASNS = new Set([16509, 14618, 8987, 15169, 396982, 19527, 36040, 8075, 8068, 8069, 12076, 14061,
  16276, 35540, 24940, 213230, 63949, 20473, 51167, 31898, 45102, 37963, 132203, 45090, 12876, 60781, 28753,
  59253, 9009, 212238, 60068, 46606, 26496, 47583, 204915, 62240, 53667, 40021, 396356, 211252, 136787, 207990,
  8100, 29802, 11878, 19318, 36352, 55286, 174, 13335, 31173, 39351, 209103, 41108]);
const HOSTING_WORDS = /\b(hosting|host|cloud|datacenter|data center|server|servers|vps|colo|colocation|dedicated|amazon|aws|google llc|microsoft|azure|digitalocean|ovh|hetzner|linode|akamai|vultr|choopa|contabo|oracle|alibaba|tencent|scaleway|leaseweb|m247|datacamp|cdn77|hostinger|ionos|hostwinds|kamatera|upcloud|vpn|nordvpn|expressvpn|surfshark|mullvad|proton|private internet access|ipvanish|cyberghost|windscribe|purevpn|torguard|packethub|tefincom)\b/i;
// Refused outright: tokens only HTTP libraries and automation frameworks send. Word-bounded so a
// phone model or browser name that merely contains the letters is never caught.
const TOOL_UA = /\b(curl|wget|python-requests|python-urllib|python-httpx|aiohttp|go-http-client|java|okhttp|axios|node-fetch|undici|libwww-perl|scrapy|postmanruntime|insomnia|windowspowershell|headlesschrome|phantomjs|puppeteer|playwright|selenium)\b/i;
// Only flagged (keeps its place, no referral credit): crawlers announce themselves, but "bot" is also
// inside real device names (e.g. CUBOT phones), so this never blocks.
const CRAWLER_UA = /(bot|crawl|spider|slurp)\b/i;

export function isHostingNetwork(asn, org) {
  return HOSTING_ASNS.has(Number(asn)) || (!!org && HOSTING_WORDS.test(org));
}

function clip(v, n) {
  if (v === undefined || v === null) return null;
  const s = String(v).trim();
  return s ? s.slice(0, n) : null;
}

function cleanChannel(raw) {
  const s = clip(raw, 500);
  if (!s) return null;
  try {
    const u = new URL(s.includes("://") ? s : `https://${s}`);
    if (!["http:", "https:"].includes(u.protocol) || !u.hostname.includes(".")) return null;
    return u.toString().slice(0, 500);
  } catch {
    return null;
  }
}

/** UTM and landing path from the page URL (the form's own page, sent as Referer or page_url). */
function attribution(pageUrl, externalReferrer, requestOrigin) {
  const out = {};
  try {
    const u = new URL(pageUrl);
    if (u.origin === requestOrigin) {
      out.landing_path = u.pathname.slice(0, 300);
      for (const k of ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]) {
        const v = u.searchParams.get(k);
        if (v) out[k] = v.slice(0, 200);
      }
      const ref = u.searchParams.get("ref");
      if (ref && !out.utm_source) Object.assign(out, { utm_source: "referral", utm_campaign: ref.slice(0, 32) });
    }
  } catch { /* no page URL */ }
  try {
    const r = new URL(externalReferrer);
    if (r.origin !== requestOrigin && ["http:", "https:"].includes(r.protocol)) {
      out.referrer = `${r.origin}${r.pathname}`.slice(0, 500); // never the query: it can hold search terms / PII
      out.referrer_host = r.hostname.slice(0, 200);
    }
  } catch { /* direct */ }
  return out;
}

export async function position(db, row) {
  const r = await db.prepare(
    "SELECT COUNT(*) AS n FROM waitlist WHERE status != 'removed' AND flagged = 0 AND seq != ?1 " +
    "AND (rank_key < ?2 OR (rank_key = ?2 AND seq < ?1))",
  ).bind(row.seq, row.rank_key).first();
  return (r?.n ?? 0) + 1;
}

export function referralUrl(request, code) {
  return `${new URL(request.url).origin}/?ref=${code}`;
}

// The widget on the waitlist forms renders with data-action="waitlist"; a token minted for another action
// (or another site using the same widget) is refused.
export const TURNSTILE_ACTION = "waitlist";
// Cloudflare's documented test secrets answer with hostname "example.com" and an empty action, so the
// hostname/action checks cannot apply to them. They are never valid in production.
const TURNSTILE_TEST_SECRETS = new Set([
  "1x0000000000000000000000000000000AA",
  "2x0000000000000000000000000000000AA",
  "3x0000000000000000000000000000000AA",
]);

function turnstileHostnames(env, request) {
  const raw = String(env.TURNSTILE_HOSTNAMES || "").split(",").map((h) => h.trim().toLowerCase()).filter(Boolean);
  if (raw.length) return raw;
  try {
    if (env.PUBLIC_URL) return [new URL(env.PUBLIC_URL).hostname.toLowerCase()];
  } catch (_) { /* fall through */ }
  return [new URL(request.url).hostname.toLowerCase()];
}

async function verifyTurnstile(env, request, token, ip) {
  if (!env.TURNSTILE_SECRET_KEY) return { ok: true, used: false };
  if (!token || typeof token !== "string" || token.length > 2048) return { ok: false, used: true };
  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET_KEY);
  form.append("response", token);
  form.append("remoteip", ip);
  form.append("idempotency_key", crypto.randomUUID());
  try {
    const res = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST", body: form, signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) return { ok: false, used: true };
    const data = await res.json();
    if (data.success !== true) return { ok: false, used: true };
    if (TURNSTILE_TEST_SECRETS.has(env.TURNSTILE_SECRET_KEY)) return { ok: true, used: true };
    const host = String(data.hostname || "").toLowerCase();
    if (data.action !== TURNSTILE_ACTION || !turnstileHostnames(env, request).includes(host)) {
      console.warn("turnstile token refused", JSON.stringify({ action: data.action, hostname: host }));
      return { ok: false, used: true };
    }
    return { ok: true, used: true };
  } catch (err) {
    console.error("turnstile unavailable", err && err.message);
    return { ok: false, used: true }; // fail closed: an unverifiable token is not a human
  }
}

export async function join(context) {
  const { request, env } = context;
  if (!sameOrigin(request)) return fail(403, "bad_origin", "Cross-site request refused.");
  const salt = ipSalt(env, request);
  if (!salt) return fail(503, "misconfigured", "The waitlist is not configured yet. Please try again later.");

  const ip = clientIp(request);
  const ipHash = await hmacHex(salt, hostKey(ip)); // the IPv4 address or the IPv6 /64
  const netHash = await hmacHex(salt, networkOf(ip)); // the /24 or /48

  for (const [bucket, limit, windowS] of [["join_ip_min", 5, 60], ["join_ip_hour", 20, 3600]]) {
    const rl = await rateLimit(env, bucket, ipHash, limit, windowS);
    if (!rl.ok) return tooMany(rl.retryAfter);
  }

  const data = await readBody(request);
  if (!data) return fail(400, "bad_request", "Send a JSON body with an email address.");
  if (data.tooLarge) return fail(413, "payload_too_large", "Request body too large.");

  // Honeypot: a field humans never see. Bots get a convincing "ok" and nothing is stored.
  if (typeof data.website === "string" ? data.website.trim() : data.website) {
    return json({ ok: true, already: false, position: null, referral_url: null });
  }
  const risk = [];
  const ts = Number(data.ts);
  if (!data.ts || !Number.isFinite(ts)) {
    risk.push("no_ts");
  } else {
    const renderedMs = ts < 1e11 ? ts * 1000 : ts; // tolerate seconds
    const fill = Date.now() - renderedMs;
    if (fill >= 0 && fill < MIN_FILL_MS) {
      return fail(400, "too_fast", "That was quick! Please wait a moment and submit again.");
    }
    if (fill > 24 * 3600 * 1000) risk.push("stale_form");
  }

  const email = normalise(data.email);
  if (!email) return fail(400, "invalid_email", "Enter a valid email address.");
  if (isDisposable(email, env.DISPOSABLE_EXTRA_DOMAINS || "", env.DISPOSABLE_ALLOW_DOMAINS || "")) {
    return fail(400, "disposable_email", "Please use a permanent email address, not a disposable one.");
  }
  const canon = canonical(email);
  const emailKey = await hmacHex(salt, canon);
  const rlEmail = await rateLimit(env, "join_email", emailKey, 5, 3600);
  if (!rlEmail.ok) return tooMany(rlEmail.retryAfter);

  const ts_ = await verifyTurnstile(env, request, data.cf_turnstile_response || data["cf-turnstile-response"], ip);
  if (!ts_.ok) return fail(400, "turnstile_failed", "Please complete the verification and try again.");

  const db = env.DB;
  const existing = await db.prepare(
    "SELECT * FROM waitlist WHERE (email = ?1 OR email_canonical = ?2) AND status != 'removed' ORDER BY seq LIMIT 1",
  ).bind(email, canon).first();
  if (existing) return already(request, db, existing, ipHash);

  // ---- risk flags (flagged entries keep their place but never earn referral credit) -----------
  const cf = request.cf || {};
  const ua = clip(request.headers.get("User-Agent"), 400);
  if (!ua) risk.push("no_user_agent");
  else if (TOOL_UA.test(ua)) return fail(403, "blocked", "Automated sign-ups are not accepted.");
  else if (CRAWLER_UA.test(ua)) risk.push("crawler_user_agent");
  if (!isLocal(request) && isHostingNetwork(cf.asn, cf.asOrganization)) risk.push("datacenter_or_vpn");
  const burst = await db.prepare(
    "SELECT COUNT(*) AS n FROM waitlist WHERE ip_hash = ?1 AND created_at > ?2",
  ).bind(ipHash, new Date(Date.now() - 3600e3).toISOString()).first();
  if ((burst?.n ?? 0) >= 2) risk.push("ip_burst");
  const netCrowd = await db.prepare(
    "SELECT COUNT(*) AS n FROM waitlist WHERE net_hash = ?1 AND created_at > ?2",
  ).bind(netHash, new Date(Date.now() - 86400e3).toISOString()).first();
  if ((netCrowd?.n ?? 0) >= 10) risk.push("network_crowd");
  const flagged = risk.some((r) => r !== "stale_form") ? 1 : 0;

  // ---- referral ------------------------------------------------------------------------------
  let referrer = null;
  let referralStatus = null;
  let referralReason = null;
  const ref = clip(data.ref, 16)?.toLowerCase();
  if (ref && /^[a-z0-9]{6,16}$/.test(ref)) {
    referrer = await db.prepare("SELECT * FROM waitlist WHERE referral_code = ?1 AND status != 'removed'")
      .bind(ref).first();
  }
  if (referrer) {
    if (referrer.email_canonical === canon) referralReason = "self";
    else if (flagged) referralReason = "flagged";
    else if (referrer.net_hash && referrer.net_hash === netHash) referralReason = "same_network";
    else {
      const sibling = await db.prepare(
        "SELECT 1 FROM waitlist WHERE referred_by_seq = ?1 AND referral_status = 'counted' AND net_hash = ?2 LIMIT 1",
      ).bind(referrer.seq, netHash).first();
      if (sibling) referralReason = "sibling_network";
      else {
        const today = await db.prepare(
          "SELECT COUNT(*) AS n FROM waitlist WHERE referred_by_seq = ?1 AND referral_status = 'counted' AND created_at > ?2",
        ).bind(referrer.seq, new Date(Date.now() - 86400e3).toISOString()).first();
        if ((today?.n ?? 0) >= DAILY_REFERRAL_CAP) referralReason = "daily_cap";
      }
    }
    referralStatus = referralReason ? "rejected" : "counted";
  }

  // ---- insert --------------------------------------------------------------------------------
  const origin = new URL(request.url).origin;
  const attr = attribution(clip(data.page_url, 2000) || request.headers.get("Referer") || "",
    clip(data.referrer, 2000) || "", origin);
  const role = clip(data.role, 64)?.toLowerCase() || null;
  const now = new Date().toISOString();
  const bump = Math.max(0, Number(env.REFERRAL_BUMP || 5));
  const id = randomId("wl_");

  let code = null;
  for (let attempt = 0; attempt < 4 && !code; attempt++) {
    const candidate = randomCode(attempt < 3 ? 8 : 12);
    const statements = [
      db.prepare(
        `INSERT INTO waitlist (id, email, email_canonical, role, channel_url, status, created_at, rank_key,
           referral_code, referred_by_seq, referral_status, referral_reason, ip_hash, net_hash, country, region,
           city, timezone, asn, as_org, colo, user_agent, referrer, referrer_host, landing_path, utm_source,
           utm_medium, utm_campaign, utm_term, utm_content, turnstile, flagged, risk)
         VALUES (?1, ?2, ?3, ?4, ?5, 'joined', ?6, 0, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19,
           ?20, ?21, ?22, ?23, ?24, ?25, ?26, ?27, ?28, ?29, ?30, ?31)`,
      ).bind(id, email, canon, role && ROLES.has(role) ? role : role ? "other" : null, cleanChannel(data.channel_url),
        now, candidate, referrer ? referrer.seq : null, referralStatus, referralReason, ipHash, netHash,
        clip(cf.country, 2), clip(cf.region, 100), clip(cf.city, 100), clip(cf.timezone, 64),
        Number.isFinite(Number(cf.asn)) ? Number(cf.asn) : null, clip(cf.asOrganization, 200), clip(cf.colo, 8),
        ua, attr.referrer || null, attr.referrer_host || null, attr.landing_path || null, attr.utm_source || null,
        attr.utm_medium || null, attr.utm_campaign || null, attr.utm_term || null, attr.utm_content || null,
        ts_.used ? 1 : 0, flagged, risk.length ? risk.join(",") : null),
      db.prepare("UPDATE waitlist SET rank_key = seq WHERE id = ?1").bind(id),
    ];
    if (referralStatus === "counted") {
      statements.push(db.prepare(
        "UPDATE waitlist SET referrals_counted = referrals_counted + 1, rank_key = seq - ?2 * (referrals_counted + 1) " +
        "WHERE seq = ?1",
      ).bind(referrer.seq, bump));
    }
    try {
      await db.batch(statements); // one transaction
      code = candidate;
    } catch (err) {
      const msg = String(err && err.message);
      if (msg.includes("waitlist.email")) {
        const row = await db.prepare("SELECT * FROM waitlist WHERE email = ?1").bind(email).first();
        if (row) return already(request, db, row, ipHash); // lost a race with the same address
      }
      if (!msg.includes("referral_code")) {
        console.error("waitlist insert failed", msg);
        return fail(500, "server_error", "Something went wrong on our side. Please try again.");
      }
    }
  }
  if (!code) return fail(500, "server_error", "Something went wrong on our side. Please try again.");
  const row = await db.prepare("SELECT seq, rank_key FROM waitlist WHERE id = ?1").bind(id).first();
  return json({
    ok: true, already: false, position: await position(db, row), referral_url: referralUrl(request, code),
    referral_code: code,
  });
}

async function already(request, db, row, ipHash) {
  // Position and the referral link only go back to the connection that joined; anyone else typing
  // the address learns nothing they could use (no referral code to farm, no place in line).
  const same = row.ip_hash && row.ip_hash === ipHash;
  return json({
    ok: true, already: true, position: same ? await position(db, row) : null,
    referral_url: same ? referralUrl(request, row.referral_code) : null,
    ...(same ? { referral_code: row.referral_code } : {}),
  });
}

export async function me(context) {
  const { request, env } = context;
  const salt = ipSalt(env, request);
  if (!salt) return fail(503, "misconfigured", "The waitlist is not configured yet.");
  const rl = await rateLimit(env, "me_ip", await hmacHex(salt, hostKey(clientIp(request))), 60, 60);
  if (!rl.ok) return tooMany(rl.retryAfter);
  const code = (new URL(request.url).searchParams.get("code") || "").trim().toLowerCase();
  if (!/^[a-z0-9]{6,16}$/.test(code)) return fail(404, "not_found", "No waitlist entry for that code.");
  const row = await env.DB.prepare("SELECT seq, rank_key, referrals_counted, referral_code, status FROM waitlist " +
    "WHERE referral_code = ?1 AND status != 'removed'").bind(code).first();
  if (!row) return fail(404, "not_found", "No waitlist entry for that code.");
  const total = await env.DB.prepare("SELECT COUNT(*) AS n FROM waitlist WHERE status != 'removed' AND flagged = 0")
    .first();
  return json({
    ok: true, status: row.status, position: await position(env.DB, row), total: total?.n ?? 0,
    referrals: row.referrals_counted, referral_url: referralUrl(request, row.referral_code),
    referral_code: row.referral_code,
  });
}

export { domainOf };
