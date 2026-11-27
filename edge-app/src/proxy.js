// App proxy: every request the Worker does not own goes to the product app on the owner's laptop through
// the Workers VPC service binding `STUDIO` (cloudflared tunnel -> 127.0.0.1:8700).
//
// What the laptop expects (saas/auth/edge.py, SAAS_TRUST_PROXY=edge; docs/saas/CONTRACT.md "Edge trust"):
//   X-DS-Edge-Auth:  EDGE_SECRET (= the laptop's SAAS_EDGE_SECRET)
//   X-DS-Client-IP:  the visitor's IP, from Cloudflare's CF-Connecting-IP, canonical as Python prints it
//   X-DS-Client-Sig: hex(HMAC-SHA256(EDGE_SECRET, `${ip}|${floor(unix_seconds/60)}`))
// and refuses duplicated X-DS-* headers, so every client-sent X-DS-* header is dropped first.
//
// Workers VPC (beta) facts this relies on, from https://developers.cloudflare.com/workers-vpc/configuration/vpc-services/
// and https://developers.cloudflare.com/workers-vpc/api/ (read 2026-11-27):
// * the VPC Service's configured host and port always decide where the request goes; the hostname in the
//   fetch() URL only fills the Host header, and the URL's port is ignored. So we fetch
//   http://<this Worker's own host>/<path>: the app sees its public hostname, the tunnel dials 127.0.0.1:8700.
// * with the http scheme the tunnel opens a plaintext connection to the service (here: loopback on the laptop).
// * when the service cannot be reached, fetch() throws (connection_refused, destination_unavailable, ...
//   https://developers.cloudflare.com/workers-vpc/reference/troubleshooting/). That rejection is the only
//   signal treated as "offline"; any status the app itself returns, 5xx included, is passed through untouched.
//
// Bodies are streamed both ways, never buffered: the request body is handed to fetch() as a stream and the
// response body is returned as the same stream (the passthrough pattern in
// https://developers.cloudflare.com/workers/runtime-apis/streams/). Video and downloads therefore flow through
// the Worker (the Developer Platform route that Cloudflare's CDN terms name for video), with no Cache API, no
// transformation, and Range / Content-Range / Accept-Ranges passed through for seeking. The app's own
// Cache-Control (private, no-store on clip media) is kept.

import { canonicalIp, hmacHex } from "./crypto.js";
import { unavailable, wantsJson, json } from "./pages.js";

// Client-controlled forwarding headers and Cloudflare's own headers are not forwarded: in edge mode the
// signed X-DS-Client-IP is the only statement about who the visitor is.
const DROP = new Set(["host", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port",
  "x-real-ip", "forwarded", "true-client-ip", "connection", "keep-alive", "proxy-connection", "te", "trailer",
  "upgrade", "proxy-authorization"]);

export function forwardHeaders(incoming) {
  const out = new Headers();
  for (const [name, value] of incoming) {
    const k = name.toLowerCase();
    if (k.startsWith("x-ds-") || k.startsWith("cf-") || DROP.has(k)) continue;
    out.append(name, value);
  }
  return out;
}

export async function edgeHeaders(secret, ip, nowMs = Date.now()) {
  const minute = Math.floor(nowMs / 60000);
  return {
    "X-DS-Edge-Auth": secret,
    "X-DS-Client-IP": ip,
    "X-DS-Client-Sig": await hmacHex(secret, `${ip}|${minute}`),
  };
}

// The app runs plain HTTP behind the tunnel, so an absolute redirect it builds from the request names
// http://<public host> (or its bind address). Point those at the public https origin; leave relative and
// third-party locations (Google sign-in, Buy Me a Coffee) alone.
export function rewriteLocation(location, publicUrl) {
  let target;
  try {
    target = new URL(location, publicUrl);
  } catch {
    return location;
  }
  if (!/^[a-z][a-z0-9+.-]*:/i.test(location) && !location.startsWith("//")) return location; // relative
  const internal = ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname);
  if ((target.host === publicUrl.host && target.protocol === "http:") || internal) {
    return `${publicUrl.protocol}//${publicUrl.host}${target.pathname}${target.search}${target.hash}`;
  }
  return location;
}

export async function proxy(request, env, url, path) {
  const secret = env.EDGE_SECRET || "";
  if (secret.length < 32 || !env.STUDIO || typeof env.STUDIO.fetch !== "function") {
    // Fail closed: never forward a request the app cannot authenticate.
    console.error(`edge misconfigured: ${secret.length < 32 ? "EDGE_SECRET missing or shorter than 32 characters" : "STUDIO VPC binding missing"}`);
    return unavailable(request, env, path, "offline");
  }
  const ip = canonicalIp(request.headers.get("cf-connecting-ip"));
  if (!ip) {
    const body = { error: "bad_client", message: "We could not tell where this request came from." };
    return wantsJson(request, path) ? json(400, body) : new Response(body.message, {
      status: 400, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  const headers = forwardHeaders(request.headers);
  for (const [k, v] of Object.entries(await edgeHeaders(secret, ip))) headers.set(k, v);

  const method = request.method.toUpperCase();
  const hasBody = method !== "GET" && method !== "HEAD" && request.body !== null;
  const init = { method, headers, redirect: "manual" };
  if (hasBody) init.body = request.body;

  // A deadline on time-to-headers for GET/HEAD only, cleared as soon as headers arrive so a long download
  // keeps streaming. Requests with bodies get none: an upload's response comes after the whole body.
  const limitMs = Number(env.UPSTREAM_HEADERS_TIMEOUT_MS || 60000);
  const controller = new AbortController();
  let timedOut = false;
  const timer = !hasBody && limitMs > 0 ? setTimeout(() => { timedOut = true; controller.abort(); }, limitMs) : null;
  if (timer) init.signal = controller.signal;

  let upstream;
  try {
    upstream = await env.STUDIO.fetch(`http://${url.host}${url.pathname}${url.search}`, init);
  } catch (err) {
    console.warn(`studio unreachable: ${timedOut ? "timeout" : (err && err.message) || err}`);
    return unavailable(request, env, path, timedOut ? "timeout" : "offline");
  } finally {
    if (timer) clearTimeout(timer);
  }

  const response = new Response(upstream.body, upstream);
  const location = response.headers.get("location");
  if (location) response.headers.set("location", rewriteLocation(location, url));
  return response;
}
