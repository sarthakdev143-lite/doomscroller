// doomscroller beta edge: one Worker on workers.dev in front of the product app on the owner's laptop.
//
//   /webhooks/bmc       Buy Me a Coffee relay (D1), answered here even when the laptop is off   -> relay.js
//   /_relay/bmc/*       the laptop's authenticated pull/ack of relayed events                     -> relay.js
//   everything else     the app (saas.main:app on 127.0.0.1:8700) through Workers VPC           -> proxy.js
//
// Setup, secrets and the docs this is built from: docs/saas/beta-edge.md.

import { proxy } from "./proxy.js";
import { receiveBmc, relayApi } from "./relay.js";
import { json, unavailable } from "./pages.js";

// Route on the decoded, slash-collapsed path, so /webhooks/%62mc or //webhooks/bmc cannot sneak past the
// relay. (The app's own /webhooks/bmc also verifies signatures, so this is tidiness, not a security gate.)
function routePath(pathname) {
  let p = pathname;
  try {
    p = decodeURIComponent(pathname);
  } catch {
    /* keep the raw path */
  }
  p = p.replace(/\/{2,}/g, "/");
  return p.length > 1 ? p.replace(/\/$/, "") : p;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = routePath(url.pathname);
    try {
      if (path === "/webhooks/bmc") return await receiveBmc(request, env);
      if (path === "/_relay" || path.startsWith("/_relay/")) return await relayApi(request, env, url, path);
      return await proxy(request, env, url, path);
    } catch (err) {
      console.error(`edge worker error on ${request.method} ${url.pathname}: ${(err && err.stack) || err}`);
      if (path === "/webhooks/bmc" || path.startsWith("/_relay")) {
        return json(500, { error: "internal_error", message: "Processing failed." });
      }
      return unavailable(request, env, path, "error");
    }
  },
};
