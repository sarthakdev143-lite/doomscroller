// Shared helpers for Pages Functions: JSON responses, client identity, hashing, D1 rate limits.
// Modules under functions/_lib export no onRequest* handlers, so Pages creates no routes for them.

const enc = new TextEncoder();

export function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...headers,
    },
  });
}

/** The contract's error envelope: {ok:false, error, message}. */
export function fail(status, error, message, headers = {}) {
  return json({ ok: false, error, message }, status, headers);
}

/**
 * The visitor's IP. On Cloudflare, CF-Connecting-IP is set by the edge and overwrites anything the
 * client sent, so it can be trusted there (and only there: never behind another proxy).
 */
export function clientIp(request) {
  return (request.headers.get("CF-Connecting-IP") || "").trim() || "0.0.0.0";
}

/** IPv4-mapped IPv6 (::ffff:1.2.3.4) is the same client as 1.2.3.4. */
function unmap(ip) {
  const m = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/i.exec(ip || "");
  return m ? m[1] : (ip || "");
}

/** The eight hextets of an IPv6 address (no leading zeros), or null if it is not one. */
export function expand6(ip) {
  if (!ip || !ip.includes(":")) return null;
  const parts = ip.toLowerCase().split("::");
  if (parts.length > 2) return null;
  const h = parts[0] ? parts[0].split(":") : [];
  const t = parts.length === 2 && parts[1] ? parts[1].split(":") : [];
  const fill = parts.length === 2 ? Array(Math.max(0, 8 - h.length - t.length)).fill("0") : [];
  const full = [...h, ...fill, ...t];
  if (full.length !== 8 || !full.every((x) => /^[0-9a-f]{1,4}$/.test(x))) return null;
  return full.map((x) => x.replace(/^0+(?=.)/, ""));
}

/**
 * One "host" for per-visitor checks (rate limits, bursts, duplicate ownership, admin lockout):
 * the IPv4 address, or the IPv6 /64. A single IPv6 connection owns a whole /64 and privacy extensions
 * rotate within it, so keying on the full v6 address would let one visitor walk past every limit.
 */
export function hostKey(ip) {
  ip = unmap(ip);
  const v6 = expand6(ip);
  return v6 ? `${v6.slice(0, 4).join(":")}::/64` : ip;
}

/** The /24 of an IPv4 address or /48 of an IPv6 address: one household / office / small ISP block. */
export function networkOf(ip) {
  ip = unmap(ip);
  if (!ip) return "";
  const v6 = expand6(ip);
  if (v6) return `${v6.slice(0, 3).join(":")}::/48`;
  const parts = ip.split(".");
  return parts.length === 4 ? `${parts[0]}.${parts[1]}.${parts[2]}.0/24` : ip;
}

function hex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const keyCache = new Map();
async function hmacKey(secret) {
  let k = keyCache.get(secret);
  if (!k) {
    k = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    keyCache.set(secret, k);
  }
  return k;
}

/** Keyed hash: without the secret salt nobody can brute-force the IPv4 space back out of it. */
export async function hmacHex(secret, value, length = 32) {
  const sig = await crypto.subtle.sign("HMAC", await hmacKey(secret), enc.encode(String(value)));
  return hex(sig).slice(0, length);
}

export async function sha256Hex(value) {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(String(value))));
}

/** Constant-time string comparison (hashes both sides first, so lengths do not leak either). */
export async function safeEqual(a, b) {
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(String(a))),
    crypto.subtle.digest("SHA-256", enc.encode(String(b))),
  ]);
  if (typeof crypto.subtle.timingSafeEqual === "function") return crypto.subtle.timingSafeEqual(x, y);
  const u = new Uint8Array(x), v = new Uint8Array(y);
  let diff = 0;
  for (let i = 0; i < u.length; i++) diff |= u[i] ^ v[i];
  return diff === 0;
}

export function randomCode(length, alphabet = "23456789abcdefghjkmnpqrstuvwxyz") {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  let out = "";
  for (const b of bytes) out += alphabet[b % alphabet.length];
  return out;
}

export function randomId(prefix) {
  return prefix + hex(crypto.getRandomValues(new Uint8Array(12)));
}

export function isLocal(request) {
  const host = new URL(request.url).hostname;
  return host === "localhost" || host === "127.0.0.1" || host === "[::1]";
}

/**
 * The secret salt for IP hashes. Required in production: an unsalted (or default-salted) hash of an
 * IPv4 address is reversible by brute force. Local dev gets a fixed throwaway salt.
 */
export function ipSalt(env, request) {
  if (env.IP_HASH_SALT && env.IP_HASH_SALT.length >= 16) return env.IP_HASH_SALT;
  return isLocal(request) ? "local-dev-only-salt-not-for-production" : null;
}

/**
 * Sliding-window rate limit backed by D1 (two fixed windows, the previous one weighted by overlap),
 * shared by every edge location. Returns {ok, retryAfter}. Fails open if D1 errors: a limiter outage
 * must not take the waitlist down (Cloudflare's own DDoS protection still sits in front).
 */
export async function rateLimit(env, bucket, key, limit, windowS) {
  const now = Math.floor(Date.now() / 1000);
  const ws = now - (now % windowS);
  try {
    const [cur, prev] = await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO rate_limits (bucket, key, window_start, count) VALUES (?1, ?2, ?3, 1) " +
        "ON CONFLICT (bucket, key, window_start) DO UPDATE SET count = count + 1 RETURNING count",
      ).bind(bucket, key, ws),
      env.DB.prepare("SELECT count FROM rate_limits WHERE bucket = ?1 AND key = ?2 AND window_start = ?3")
        .bind(bucket, key, ws - windowS),
    ]);
    const count = cur.results?.[0]?.count ?? 1;
    const prevCount = prev.results?.[0]?.count ?? 0;
    const weight = (windowS - (now - ws)) / windowS;
    const estimate = prevCount * weight + count;
    if (Math.random() < 0.02) {
      // Opportunistic cleanup of windows nobody can read any more.
      await env.DB.prepare("DELETE FROM rate_limits WHERE window_start < ?1").bind(now - 2 * 86400).run();
    }
    if (estimate <= limit) return { ok: true, retryAfter: 0 };
    let wait = ws + windowS - now;
    if (count <= limit && prevCount > 0) {
      wait = Math.ceil(windowS * (1 - (limit - count) / prevCount) - (now - ws));
    } else if (count > limit) {
      wait = ws + windowS - now + Math.ceil(windowS * (1 - limit / count));
    }
    return { ok: false, retryAfter: Math.max(1, Math.min(2 * windowS, wait)) };
  } catch (err) {
    console.error("rate limiter unavailable", err && err.message);
    return { ok: true, retryAfter: 0 };
  }
}

export function tooMany(retryAfter) {
  return fail(429, "rate_limited", "Too many attempts. Please wait a moment and try again.", {
    "Retry-After": String(retryAfter),
  });
}

/** Parse a JSON or form-encoded body into a plain object of strings. Returns null when unreadable. */
export async function readBody(request, maxBytes = 16 * 1024) {
  const len = Number(request.headers.get("Content-Length") || 0);
  if (len > maxBytes) return { tooLarge: true };
  const raw = await request.text();
  if (raw.length > maxBytes) return { tooLarge: true };
  const type = request.headers.get("Content-Type") || "";
  try {
    if (type.includes("application/json")) {
      const data = JSON.parse(raw || "{}");
      return data && typeof data === "object" && !Array.isArray(data) ? data : null;
    }
    const out = {};
    for (const [k, v] of new URLSearchParams(raw)) out[k] = v;
    return out;
  } catch {
    return null;
  }
}

/** Same-origin check for state-changing browser requests (CSRF / cross-site form posting). */
export function sameOrigin(request) {
  const url = new URL(request.url);
  const origin = request.headers.get("Origin");
  if (origin) return origin === url.origin;
  const ref = request.headers.get("Referer");
  if (ref) {
    try { return new URL(ref).origin === url.origin; } catch { return false; }
  }
  return true; // non-browser clients send neither; they still face Turnstile, limits and timing checks
}
