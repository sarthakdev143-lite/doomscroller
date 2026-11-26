// GET /api/waitlist/me?code=<referral code> -> {ok, status, position, total, referrals, referral_url}
// No email in the response: the code is public (it is in everyone's referral link).
import { fail } from "../../_lib/http.js";
import { me } from "../../_lib/waitlist.js";

export async function onRequest(context) {
  if (context.request.method === "GET" || context.request.method === "HEAD") return me(context);
  return fail(405, "method_not_allowed", "Use GET.", { Allow: "GET" });
}
