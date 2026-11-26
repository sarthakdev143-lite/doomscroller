// /admin           read-only waitlist dashboard
// /admin/export.csv  every entry as a spreadsheet-safe CSV
// HTTP Basic (ADMIN_PASSWORD) with a per-IP lockout; meant to sit behind Cloudflare Access too.
import { dashboard, exportCsv, guard } from "../_lib/admin.js";

export async function onRequest(context) {
  const { request, env, params } = context;
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", { status: 405, headers: { Allow: "GET", "Cache-Control": "no-store" } });
  }
  const denied = await guard(request, env);
  if (denied) return denied;
  const path = (params.path || []).join("/");
  if (path === "") return dashboard(request, env);
  if (path === "export.csv") return exportCsv(env);
  return new Response("Not found", { status: 404, headers: { "Cache-Control": "no-store" } });
}
