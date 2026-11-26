// POST /api/waitlist
//   body (JSON or form): {email, role?, channel_url?, ref?, ts, website, cf_turnstile_response?,
//                         page_url?, referrer?}
//   200 {ok: true, already, position, referral_url, referral_code?}
//   4xx {ok: false, error, message}: invalid_email | disposable_email | too_fast | turnstile_failed |
//        bad_origin | blocked | rate_limited (429, Retry-After) | payload_too_large
import { fail } from "../_lib/http.js";
import { join } from "../_lib/waitlist.js";

export async function onRequest(context) {
  if (context.request.method === "POST") return join(context);
  return fail(405, "method_not_allowed", "Use POST.", { Allow: "POST" });
}
