// Responses the Worker writes itself: the "studio is offline" page and JSON errors.
// A visitor never sees a raw Cloudflare error from this Worker: browsers get a small branded page,
// API and MCP clients get the app's own error envelope {"error", "message"} (docs/saas/CONTRACT.md).

const JSON_PREFIXES = ["/api/", "/mcp", "/files/", "/webhooks/", "/_relay/"];

export function json(status, body, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...extraHeaders,
    },
  });
}

export function wantsJson(request, path) {
  if (path === "/api" || path === "/mcp" || JSON_PREFIXES.some((p) => path.startsWith(p))) return true;
  const accept = (request.headers.get("accept") || "").toLowerCase();
  return !accept.includes("text/html") && accept.includes("application/json");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const COPY = {
  offline: {
    code: "studio_offline",
    title: "The studio is offline",
    message: "The studio is offline right now. Try again in a few minutes.",
    detail: "Your clips and credits are safe. This page checks again by itself every 30 seconds.",
  },
  timeout: {
    code: "studio_timeout",
    title: "The studio is not answering",
    message: "The studio took too long to answer. Try again in a minute.",
    detail: "Your clips and credits are safe. This page checks again by itself every 30 seconds.",
  },
  error: {
    code: "edge_error",
    title: "Something went wrong on our side",
    message: "Something went wrong on our side. Try again in a minute.",
    detail: "Your clips and credits are safe.",
  },
};

function page(env, copy) {
  const brand = escapeHtml(env.BRAND_NAME || "doomscroller");
  const site = /^https:\/\/[^\s"'<>]+$/.test(env.PUBLIC_SITE_URL || "") ? env.PUBLIC_SITE_URL : "";
  const link = site ? `<p class="small"><a href="${escapeHtml(site)}">${escapeHtml(site.replace(/^https:\/\//, ""))}</a></p>` : "";
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<meta name="robots" content="noindex">
<title>${escapeHtml(copy.title)} · ${brand}</title>
<style>
:root{color-scheme:dark light;--bg:#0b0b0d;--fg:#f2f2f0;--muted:#9a9a96;--accent:#d7ff3a}
@media (prefers-color-scheme:light){:root{--bg:#f6f6f2;--fg:#111;--muted:#5d5d58;--accent:#3b4cff}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:24px 16px}
main{max-width:30rem}
.brand{font-weight:800;letter-spacing:-.02em;font-size:1.05rem}
.brand::after{content:"";display:inline-block;width:.5em;height:.5em;margin-left:.35em;border-radius:50%;background:var(--accent)}
h1{font-size:clamp(1.6rem,6vw,2.2rem);line-height:1.15;margin:1.2rem 0 .6rem;letter-spacing:-.02em}
p{margin:.4rem 0;color:var(--muted)}
.small{font-size:.9rem;margin-top:1.4rem}
a{color:var(--fg)}
</style>
</head>
<body>
<main>
<div class="brand">${brand}</div>
<h1>${escapeHtml(copy.title)}</h1>
<p>${escapeHtml(copy.message)}</p>
<p>${escapeHtml(copy.detail)}</p>
${link}
</main>
</body>
</html>
`;
}

// kind: "offline" (the laptop is unreachable), "timeout" (no answer in time), "error" (a bug in the Worker).
export function unavailable(request, env, path, kind = "offline") {
  const copy = COPY[kind] || COPY.offline;
  const status = kind === "timeout" ? 504 : 503;
  const common = { "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Retry-After": "60" };
  if (wantsJson(request, path)) {
    const res = json(status, { error: copy.code, message: copy.message }, common);
    return request.method === "HEAD" ? new Response(null, res) : res;
  }
  const headers = {
    ...common,
    "Content-Type": "text/html; charset=utf-8",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
  };
  return new Response(request.method === "HEAD" ? null : page(env, copy), { status, headers });
}
