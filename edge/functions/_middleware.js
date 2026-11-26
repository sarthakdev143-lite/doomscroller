// Security headers for every response on the site: static pages from public/ and the Functions.
//
// HTML pages get a strict CSP with a fresh per-response nonce, and HTMLRewriter stamps that nonce on
// the page's own inline <script> tags (Google Analytics / Clarity bootstrap snippets) so they run
// while any injected script would not. Because a cached page would carry a stale nonce, HTML is
// served without validators (no ETag / Last-Modified, conditional headers stripped) and always 200.
// A response that already set its own CSP (the /admin dashboard) keeps it.

const TURNSTILE = "https://challenges.cloudflare.com";
const GA = ["https://www.googletagmanager.com", "https://*.google-analytics.com", "https://*.analytics.google.com",
  "https://*.googletagmanager.com"];
const CLARITY = ["https://*.clarity.ms", "https://c.bing.com"];

function csp(nonce, https) {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' ${TURNSTILE} https://www.googletagmanager.com ${CLARITY[0]}`,
    // Turnstile, GA and Clarity inject styles; style injection cannot run code.
    "style-src 'self' 'unsafe-inline'",
    `img-src 'self' data: blob: ${GA.join(" ")} ${CLARITY.join(" ")}`,
    "font-src 'self' data:",
    `connect-src 'self' ${TURNSTILE} ${GA.join(" ")} ${CLARITY.join(" ")}`,
    `frame-src ${TURNSTILE} https://www.googletagmanager.com`,
    "worker-src 'self' blob:",
    "manifest-src 'self'",
    "media-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    ...(https ? ["upgrade-insecure-requests"] : []),
  ].join("; ");
}

const BASE = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), " +
    "payment=(), usb=(), browsing-topics=()",
  "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
  "X-Permitted-Cross-Domain-Policies": "none",
};

class NonceScripts {
  constructor(nonce) { this.nonce = nonce; }
  element(el) {
    // Only the page's own inline scripts; external ones are governed by the host allowlist.
    if (!el.hasAttribute("src")) el.setAttribute("nonce", this.nonce);
  }
}

export async function onRequest(context) {
  const { request } = context;
  const url = new URL(request.url);
  const wantsHtml = request.method === "GET" && !url.pathname.startsWith("/api/") &&
    (request.headers.get("Accept") || "").includes("text/html");
  let forward = request;
  if (wantsHtml && (request.headers.has("If-None-Match") || request.headers.has("If-Modified-Since"))) {
    const headers = new Headers(request.headers);
    headers.delete("If-None-Match");
    headers.delete("If-Modified-Since");
    forward = new Request(request, { headers });
  }

  let response;
  try {
    response = await context.next(forward);
  } catch (err) {
    console.error("unhandled error", url.pathname, err && err.stack);
    response = new Response(JSON.stringify({ ok: false, error: "server_error", message: "Something went wrong." }),
      { status: 500, headers: { "Content-Type": "application/json; charset=utf-8" } });
  }

  const headers = new Headers(response.headers);
  for (const [k, v] of Object.entries(BASE)) if (!headers.has(k)) headers.set(k, v);
  const https = url.protocol === "https:";
  if (https) headers.set("Strict-Transport-Security", "max-age=63072000; includeSubDomains");
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/admin")) {
    headers.set("Cache-Control", "no-store");
    headers.set("X-Robots-Tag", "noindex, nofollow");
  }

  // Site pages always get this middleware's CSP, replacing any static fallback from public/_headers
  // (which cannot allow Turnstile / GA / Clarity safely without a nonce). Function pages that set
  // their own (the /admin dashboard) keep theirs.
  const isHtml = (headers.get("Content-Type") || "").includes("text/html");
  const ownsCsp = url.pathname.startsWith("/admin") || url.pathname.startsWith("/api/");
  if (isHtml && !(ownsCsp && headers.has("Content-Security-Policy"))) {
    const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
    headers.set("Content-Security-Policy", csp(nonce, https));
    headers.delete("ETag");
    headers.delete("Last-Modified");
    if (!headers.has("Cache-Control") || headers.get("Cache-Control").includes("max-age")) {
      headers.set("Cache-Control", "no-cache");
    }
    const out = new Response(response.body, { status: response.status, statusText: response.statusText, headers });
    return new HTMLRewriter().on("script", new NonceScripts(nonce)).transform(out);
  }
  // API responses opened directly as a document get a CSP that allows nothing at all. Static assets
  // (CSS/JS/images/fonts) need none of their own.
  if (!headers.has("Content-Security-Policy") &&
      (url.pathname.startsWith("/api/") || (headers.get("Content-Type") || "").includes("json"))) {
    headers.set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'");
  }
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}
