// HMAC, hashing, constant-time comparison and IP canonicalisation for the edge Worker.
//
// The laptop app checks what this file produces:
// * X-DS-Client-Sig = hex(HMAC-SHA256(key=EDGE_SECRET, msg=`${ip}|${floor(unix_seconds / 60)}`)), verified
//   by saas/auth/edge.py over `str(ipaddress.ip_address(ip))`. So `ip` must already be in that canonical
//   form, or the signature would be over different text and the app would answer 403 bad_client_ip.
// * Buy Me a Coffee's `x-signature-sha256` = hex HMAC-SHA256 of the raw body with the signing secret
//   (https://cdn.buymeacoffee.com/assets/integrations/bmc-webhooks-openapi.json), the same rule as
//   saas/billing/providers.verify_bmc_signature. The laptop re-verifies every relayed event with that
//   Python function; this copy only keeps unsigned junk out of D1.
// test/vectors.json is generated from the Python side and checked by both test suites.

const enc = new TextEncoder();

export function toHex(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (let i = 0; i < bytes.length; i++) out += bytes[i].toString(16).padStart(2, "0");
  return out;
}

function hexToBytes(hex) {
  if (typeof hex !== "string" || hex.length === 0 || hex.length % 2 !== 0 || !/^[0-9a-f]+$/.test(hex)) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function hmacKey(secret, usages) {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, usages);
}

export async function hmacHex(secret, message) {
  const key = await hmacKey(secret, ["sign"]);
  return toHex(await crypto.subtle.sign("HMAC", key, typeof message === "string" ? enc.encode(message) : message));
}

// True when `sigHex` (already trimmed and lower-cased by the caller) is the HMAC of `bytes`.
// crypto.subtle.verify compares in constant time.
export async function verifyHmacHex(secret, bytes, sigHex) {
  const sig = hexToBytes(sigHex);
  if (!secret || !sig || sig.length !== 32) return false;
  const key = await hmacKey(secret, ["verify"]);
  return crypto.subtle.verify("HMAC", key, sig, bytes);
}

export async function sha256Hex(bytes) {
  return toHex(await crypto.subtle.digest("SHA-256", bytes));
}

// Constant-time string comparison, following Cloudflare's "Protect against timing attacks" example
// (https://developers.cloudflare.com/workers/examples/protect-against-timing-attacks/): never return early
// on a length mismatch, compare the input with itself instead.
export function safeEqual(given, expected) {
  const a = enc.encode(String(given ?? ""));
  const b = enc.encode(String(expected ?? ""));
  return a.byteLength === b.byteLength ? crypto.subtle.timingSafeEqual(a, b) : !crypto.subtle.timingSafeEqual(a, a);
}

// ---- IP canonicalisation -------------------------------------------------------------------------
// Returns the address exactly as Python's `str(ipaddress.ip_address(x))` prints it, or null. IPv4 is
// dotted decimal without leading zeros (Python rejects them too). IPv6 follows RFC 5952: lower case, no
// leading zeros, the longest run of two or more zero groups compressed (the leftmost on a tie). An
// IPv4-mapped address (::ffff:a.b.c.d) is returned as the IPv4 client itself, because Python versions
// disagree on how to print the mapped form. Zone ids (fe80::1%eth0) are refused: a visitor never has one.

const V4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const HEXTET = /^[0-9a-f]{1,4}$/;

function parseV6(text) {
  let s = text.toLowerCase();
  if (!/^[0-9a-f:.]+$/.test(s) || !s.includes(":")) return null;
  if (s.includes(".")) {
    const cut = s.lastIndexOf(":");
    const v4 = s.slice(cut + 1);
    if (!V4.test(v4)) return null;
    const o = v4.split(".").map(Number);
    s = `${s.slice(0, cut + 1)}${((o[0] << 8) | o[1]).toString(16)}:${((o[2] << 8) | o[3]).toString(16)}`;
  }
  const halves = s.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] === "" ? [] : halves[0].split(":");
  const tail = halves.length === 2 ? (halves[1] === "" ? [] : halves[1].split(":")) : [];
  if (![...head, ...tail].every((g) => HEXTET.test(g))) return null;
  let groups;
  if (halves.length === 1) {
    if (head.length !== 8) return null;
    groups = head;
  } else {
    if (head.length + tail.length > 7) return null;
    groups = [...head, ...Array(8 - head.length - tail.length).fill("0"), ...tail];
  }
  return groups.map((g) => parseInt(g, 16));
}

export function canonicalIp(raw) {
  if (typeof raw !== "string") return null;
  const s = raw.trim();
  if (!s || s.length > 64) return null;
  if (V4.test(s)) return s;
  const g = parseV6(s);
  if (!g) return null;
  if (g.slice(0, 5).every((x) => x === 0) && g[5] === 0xffff) {
    return `${g[6] >> 8}.${g[6] & 255}.${g[7] >> 8}.${g[7] & 255}`;
  }
  let bestStart = -1, bestLen = 0, runStart = -1, runLen = 0;
  for (let i = 0; i < 8; i++) {
    if (g[i] === 0) {
      if (runStart < 0) { runStart = i; runLen = 0; }
      runLen++;
      if (runLen > bestLen) { bestLen = runLen; bestStart = runStart; }
    } else {
      runStart = -1;
      runLen = 0;
    }
  }
  const hex = g.map((x) => x.toString(16));
  if (bestLen > 1) {
    return `${hex.slice(0, bestStart).join(":")}::${hex.slice(bestStart + bestLen).join(":")}`;
  }
  return hex.join(":");
}

// Chunked base64 of raw bytes (String.fromCharCode(...bigArray) can overflow the call stack).
export function bytesToBase64(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
