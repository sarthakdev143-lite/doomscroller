# SaaS build contract

Single source of truth for the parallel build. If you change an interface you own, update this file
in the same change. If you need an interface you don't own, code against what is written here and use
`app.dependency_overrides` / monkeypatch in tests until the owner lands it.

## Product scope

The SaaS sells the **clipper**: a creator pastes a link to (or uploads) a long video they have the
rights to, and gets finished 1080x1920 Shorts back: sentence-accurate cuts, word-level captions
(English via Deepgram, Hindi via Sarvam), speaker-focus framing, B-roll, sound effects, music bed,
memes, AI titles/descriptions/hashtags, and (later) scheduled posting to their own YouTube channel.

**Out of scope, never exposed to customers:** the Snapchat Spotlight scrape/crop/repost pipeline, the
owner's growth loop, `app/main.py` (:8000) and `app/review.py` (:8600). The SaaS is its own process
(`saas.main:app`, :8700) with its own database (`SAAS_DATABASE_URL`, default `saas.db`). It never reads
the owner's `.env` (it reads `SAAS_*` / `.env.saas`) and never writes `doomscroller.db`.

## Ground rules for every agent

- Worktree: `D:\Sarthak\Coding\My Codes\Vibe-coding\doomscroller\.claude\worktrees\saas-launch`.
  Work only there. The main checkout (`D:\Sarthak\Coding\My Codes\Vibe-coding\doomscroller`) is
  **read-only** for you: its `content/`, `logs/` and `doomscroller.db` hold the owner's live data.
- Python: SaaS code and tests run on `.venv-saas\Scripts\python.exe` (in the worktree). Pipeline-only
  tests may use `D:\Sarthak\Coding\My Codes\Vibe-coding\doomscroller\venv\Scripts\python.exe`.
- **No git writes.** `git status` / `git diff` / `git log` only. The integrator commits.
- **No new dependencies** without asking the integrator in your final report (the venv is shared).
  Already available: fastapi, sqlalchemy 2, pydantic 2, httpx, jinja2, argon2-cffi, pyotp, segno (QR),
  user-agents, geoip2, email-validator, mcp, pillow, numpy, opencv-headless, yt-dlp.
- Never run `scripts/run.py`, `scripts/clip.py`, `scripts/growth.py` or anything that loads the owner's
  `.env` (it turns on real YouTube uploads). Never print secrets.
- Edit only files you own (table below). Integrator-owned shared files: `saas/config.py` (you may
  append fields inside your own marked section with Edit, never Write), `saas/main.py`, `saas/db.py`,
  `saas/common.py`, `saas/templating.py`, `tests/saas/conftest.py`, `requirements*.txt`, `CLAUDE.md`.
- Tests: `.venv-saas\Scripts\python.exe -m pytest tests/saas -q`. Put yours in `tests/saas/test_<area>_*.py`.
  Fixtures `saas_db` (fresh in-memory schema) and `client` (TestClient) are in `tests/saas/conftest.py`.
- Datetimes are timezone-aware UTC (`saas.common.utcnow()`). Public ids are opaque
  (`saas.common.new_id(prefix)`), never autoincrement ints in URLs.
- JSON errors: `{"error": "<machine_code>", "message": "<human sentence>"}` with a correct status.

## Ownership

| Area | Owner | Paths |
|------|-------|-------|
| Skeleton, config, contract, integration | integrator | `saas/{config,db,common,templating,main}.py`, `saas/models/__init__.py`, `tests/saas/conftest.py`, this file |
| Video pipeline | pipeline agent | `app/clipper/**` (incl. `metering.py` instrumentation), `tests/test_clipper*.py`, `tests/test_memes.py`, `scripts/fetch_memes.py`, `scripts/fetch_sfx.py` |
| Design system + all customer pages | web agent | `saas/web/**`, `saas/models/` none |
| Auth, billing, credits, jobs, API keys, public API v1, MCP | core agent | `saas/auth/**`, `saas/billing/**`, `saas/jobs/**`, `saas/mcp/**`, `saas/models/{core,billing,jobs}.py`, `scripts/saas_*.py` |
| Tracking, consent, fingerprinting, security/abuse, waitlist backend, admin panel | tracking agent | `saas/tracking/**`, `saas/security/**`, `saas/waitlist/**`, `saas/admin/**`, `saas/models/{tracking,security,waitlist}.py` |
| Costs & pricing research | research agent | `docs/saas/costs-and-pricing.md` |

## Cross-module interfaces

### Auth (core agent) — `saas.auth.deps`
- `optional_user(request, db) -> User | None` — from the `sid` session cookie.
- `require_user` → 401 JSON for `/api/*`, redirect to `/login?next=` for pages.
- `require_verified_user` → as `require_user`, plus 403 `email_unverified` until the email is verified.
- `require_admin` → user.role == "admin" **and** TOTP enrolled + verified this session; else 403
  (`mfa_required` when the admin has no TOTP or this session has not passed it).
- `require_recent_auth` → 403 `reauth_required` unless the session re-authenticated in the last
  `SAAS_REAUTH_MINUTES` (10). The page then calls `POST /auth/reauth` and retries.
- `require_api_key(scopes)` → for `/api/v1/*` and `/mcp`; `Authorization: Bearer dsk_live_...`.
  Returns `ApiPrincipal(user, key)`.
- `current_session(request) -> AuthSession | None` (set by the deps on `request.state.auth_session`).
- `User` (`saas.models.core.User`, table `users`): `id` (str), `email`, `email_verified_at`, `name`,
  `role` ("user"|"admin"), `status` ("active"|"suspended"|"banned"|"deleted"), `plan_slug`, `created_at`,
  `signup_ip`, `signup_fingerprint`, `signup_country`, `visitor_id`, `referral_code`, `referred_by`,
  `billing_hold` (bool: refund/chargeback; blocks new jobs), `flags` (JSON dict, admin-visible notes),
  `signup_risk` (JSON: the `risk.assess_signup` verdict stored at signup).
- Core tables (all prefixed so no agent collides): `users`, `auth_sessions`, `auth_email_tokens`,
  `auth_totp`, `auth_recovery_codes`, `auth_identities` (Google), `auth_oauth_states`,
  `auth_mfa_challenges`, `api_keys`.
- Errors from every core route use the envelope `{"error", "message"}`; the core routers carry a route
  class that also turns 422s into `{"error": "invalid_request", "message", "fields": [...]}`.

**Auth endpoints** (JSON bodies; when a `sid` cookie is present every POST/PATCH/DELETE needs
`X-CSRF-Token`, per the security middleware). Identity-creating forms also send `fp`, `ts`, `website`,
`cf_turnstile_response`.

| Method & path | Body / query | Response |
|---|---|---|
| `POST /auth/signup` | `{email, password, name?, invite?, ref?, fp, ts, website, cf_turnstile_response?}` | 201 `{ok: true, verify_required: true}` for a new **and** an existing email (no enumeration; an existing owner gets a "you already have an account" mail). Never logs in. Sets an HttpOnly `su` cookie binding this browser to the verify link. `launch_mode=waitlist` needs `invite` (403 `invite_required`). 400 `weak_password` / `breached_password`, 403 `signup_blocked` (risk), 400 `captcha_failed`. The page should say "check your inbox". |
| `POST /auth/login` | `{email, password, fp, ts, website, cf_turnstile_response?}` | `{ok: true, user}` + `sid`, or `{ok: false, mfa_required: true, mfa_token}` when TOTP is on. 401 `invalid_credentials` (same for unknown email). 429 `rate_limited`. |
| `POST /auth/login/mfa` | `{mfa_token, code}` (6-digit TOTP or a recovery code) | `{ok: true, user}` + `sid`. 401 `invalid_code`. |
| `POST /auth/logout` | – | `{ok: true}`, cookie cleared |
| `GET /auth/verify/{token}` | – | verifies, grants the free tier, then 303 → `/app?verified=1` with a session **only in the browser that signed up** (`su` cookie) or one already signed in as that user; otherwise 303 → `/login?verified=1`. Bad/expired link → `/verify-email?status=invalid`. |
| `POST /auth/verify/resend` | – (signed in) | `{ok: true}` |
| `POST /auth/magic` | `{email, next?, fp, ts, website, cf_turnstile_response?}` | always `{ok: true}` |
| `GET /auth/magic/{token}` | – | small confirm page with a POST button (mail scanners prefetch GETs) |
| `POST /auth/magic/{token}` | form post | 303 → `next` or `/app`; with TOTP → 303 `/login?mfa=<mfa_token>` (login page then posts `/auth/login/mfa`) |
| `POST /auth/forgot` | `{email, fp, ts, website, cf_turnstile_response?}` | always `{ok: true}` |
| `POST /auth/reset` | `{token, password}` | `{ok: true}`; every session revoked, user signs in again |
| `GET /auth/google/start?next=/app` | – | 303 → Google (404 `google_disabled` when unconfigured) |
| `GET /auth/google/callback` | Google's query | 303 → `next`, `/login?mfa=...`, or `/login?error=<code>` |
| `POST /auth/reauth` | `{password}` or `{code}` | `{ok: true, until}` |
| `POST /auth/mfa/verify` | `{code}` | step-up for the current session (admins) `{ok: true}` |

**Account endpoints** (signed in):

| Method & path | Body | Response |
|---|---|---|
| `GET /api/me` | – | `{id, email, email_verified, name, role, status, plan: {slug, name, features}, balance, free: {minutes, shorts}, timezone, mfa_enabled, has_password, google_linked, referral_code, referral_url, created_at, free_credits_granted, billing_hold}` |
| `PATCH /api/me` | `{name?, timezone?}` (IANA, e.g. `Asia/Kolkata`; 400 `invalid_timezone`) | user |
| `POST /api/me/password` | `{current_password?, new_password}` | `{ok}`; other sessions revoked, this one rotated |
| `GET /api/me/sessions` | – | `{sessions: [{id, current, ip, user_agent, created_at, last_seen_at}]}` |
| `DELETE /api/me/sessions/{id}` | – | `{ok}` |
| `POST /api/me/sessions/revoke-others` | – | `{ok, revoked}` |
| `POST /api/me/mfa/totp/setup` | – (recent auth) | `{secret, otpauth_uri, qr_svg}` |
| `POST /api/me/mfa/totp/confirm` | `{code}` | `{ok, recovery_codes: [10]}` (shown once) |
| `POST /api/me/mfa/totp/disable` | `{code}` (recent auth; admins cannot) | `{ok}` |
| `POST /api/me/mfa/recovery-codes` | – (recent auth) | `{recovery_codes}` |
| `GET /api/me/export` | – | JSON attachment of everything we hold about the user |
| `POST /api/me/delete` | `{confirm: "DELETE"}` (recent auth) | `{ok}`; files deleted, row anonymised, sessions/keys revoked |
| `GET /api/keys` | – | `{keys: [{id, name, prefix, scopes, created_at, last_used_at, revoked}]}` |
| `POST /api/keys` | `{name, scopes?}` (verified; plan must include `api`) | 201 `{id, key, prefix, scopes}`; `key` is shown once |
| `DELETE /api/keys/{id}` | – | `{ok}` |

API key scopes: `jobs:read`, `jobs:write`, `clips:read`, `clips:write`, `billing:read`. Default: all.

**Edge waitlist -> app invites** (core agent, `saas/auth/waitlist_bridge.py`). The live waitlist is D1
(edge/); app signup redeems invites against the app's `waitlist_entries`. The owner bridges them with
`scripts/saas_manage.py import-waitlist <export.csv> [--invite N] [--include-flagged] [--dry-run] [--out invites.csv]`
(and `--outstanding` to re-export every issued, unredeemed invite):
- Reads exactly what edge `/admin/export.csv` writes (admin.js `toCsv`: BOM, CRLF, quoting, and the
  `safeCell` formula apostrophe, which is undone).
- Upserts by normalised email, keeping `seq` (join order), position (`rank_key = seq -
  waitlist_referral_bump x referrals_counted`, the formula both sides use), referral code/count and
  referrer, `created_at`, country, role, channel, first-touch source and risk reasons. Imported waiting
  entries are `confirmed`. Flagged rows are skipped unless `--include-flagged`; an entry flagged later is
  held from invites. Edge `removed` removes (and voids an unused invite); a `joined` entry is never touched.
  Idempotent: re-importing the same file changes nothing and never undoes issued invites.
- `--invite N` invites the top N un-invited, unflagged, waiting entries by position (`saas.waitlist.service.invite`,
  no email sent) and prints/writes `email,invite_code,signup_url,expires_at` with
  `signup_url = SAAS_PUBLIC_URL/signup?invite=<code>`. The owner mails them.

### Security (tracking agent) — `saas.security`
- `limits.enforce(request, bucket: str, limit: int, window_s: int, key: str | None = None)` — raises
  429 `{"error":"rate_limited"}`; key defaults to client IP. `limits.hit(...) -> bool` non-raising.
- `turnstile.verify(token: str | None, ip: str) -> bool` — True when no secret is configured (dev).
- `risk.assess_signup(db, request, email, fingerprint) -> Verdict(score: int, reasons: list[str],
  block: bool, grant_free_credits: bool)` — disposable email, IP/fingerprint clusters, Tor/datacenter,
  velocity, honeypot/timing. Core agent calls it on signup and only grants free credits when allowed.
- `events.log(db, kind, severity, *, ip=None, user_id=None, fingerprint=None, detail=None)`.
- `middleware.install(app)` — security headers (CSP, HSTS in prod, frame-deny, nosniff, referrer,
  permissions), request-size caps, ban checks, global per-IP limits, **CSRF**: double-submit cookie
  `csrf` set on every HTML response; every state-changing `/api/*`, `/auth/*`, `/admin/*` request
  with a session cookie must send header `X-CSRF-Token` equal to it. Exempt: `/webhooks/*`,
  `/api/t*` (beacon; origin-checked), `/api/v1/*` and `/mcp` (API key, no cookies).
- Forms that create identities (waitlist, signup, login, magic link) post: `fp` (fingerprint hash
  from the tracker), `ts` (ms timestamp the form rendered), `website` (honeypot, must be empty),
  `cf_turnstile_response` when Turnstile is configured.
- As built (Python, parked after the edge pivot): state-changing `/api/*`, `/auth/*`, `/admin/*`
  requests with a cross-site `Origin`/`Referer` get 403 `bad_origin` (paths ending `/callback` exempt);
  CSRF compares any non-empty `csrf` cookie with `X-CSRF-Token`. Templates get `request.state.csp_nonce`
  (use `<script nonce="{{ request.state.csp_nonce }}">`; the CSP has no `'unsafe-inline'` for scripts)
  and `request.state.csrf_token`. `events.log(...)` commits the caller's session by default
  (`commit=False` to defer). `risk.assess_signup` also takes optional `honeypot=`, `form_ts=`,
  `turnstile_ok=`; call `risk.record_signup(db, user_id, email, ip, fingerprint)` after creating a user.
  `/api/uploads` gets a body-cap backstop of `max_upload_mb + 64` MB so the route's own 413 fires first.
  `saas.security.maintenance.run_once()` (retention + Tor list) is not wired to any scheduler yet.
  The waitlist mailer call adapts to the core's `mailer.send(to, subject, paragraphs, button)`.

### Tracking (tracking agent) — `saas.tracking`
- Pages include `<script src="{{ asset('/ts/t.js') }}" defer></script>` (web agent adds it to the
  base template). t.js handles consent, the `vid` first-party cookie, fingerprinting, batching.
- `window.dsTrack(name, props)` for custom events; `window.dsFingerprint()` → `Promise<string>`;
  t.js fills every `input[name=fp]` automatically.
- Markup hooks the web agent must add: `data-track-section="<name>"` on every landing/pricing section
  (dwell + scroll attribution), `data-track="<name>"` on every CTA/link/toggle worth counting,
  `data-track-mask` on anything sensitive (replay masks all inputs anyway).
- `identity.visitor_id(request) -> str | None`; `identity.link(db, visitor_id, *, user_id=None,
  waitlist_id=None)` — core agent calls `link` on signup/login so a user's pre-signup journey shows.
- Endpoints: `POST /api/t` (batched events), `POST /api/t/replay` (rrweb chunks), `POST /api/consent`.

### Waitlist (tracking agent) — `saas.waitlist`
- `POST /api/waitlist` body `{email, role?, channel_url?, ref?, fp, ts, website, cf_turnstile_response?}`
  → `{ok, position, referral_url, already}`. Double opt-in email; referrals move people up.
- `GET /waitlist/confirm/{token}`; `GET /api/waitlist/me?code=<referral_code>` → position/referrals.
- `waitlist.redeem_invite(db, code, email) -> WaitlistEntry | None` — core agent's signup calls it in
  `launch_mode=waitlist`.
- **Launch implementation: `edge/` (Cloudflare Pages Functions + D1), not Python.** Same request/response
  shape, with these differences: no double opt-in (entries are `joined`, `confirmed_at` reserved), no `fp`
  (ignored), optional extra body fields `page_url` (location.href, for UTM) and `referrer`
  (document.referrer); without them UTM comes from the `Referer` header. Responses also carry
  `referral_code`. Errors: 400 `invalid_email` / `disposable_email` / `too_fast` / `turnstile_failed` /
  `bad_request`, 403 `bad_origin` / `blocked`, 413 `payload_too_large`, 429 `rate_limited` (+`Retry-After`),
  503 `misconfigured`. A filled honeypot gets a fake `200 {ok: true, position: null}`. `GET
  /api/waitlist/me?code=` → `{ok, status, position, total, referrals, referral_url, referral_code}`.
  Admin: `GET /admin`, `GET /admin/export.csv` (HTTP Basic `ADMIN_PASSWORD`, behind Cloudflare Access).
  Site-wide security headers and the page CSP (per-response nonce, Turnstile + GA4 + Clarity hosts)
  come from `edge/functions/_middleware.js`, which replaces any CSP in `public/_headers` on HTML pages.

### Billing (core agent) — `saas.billing`
- Credits are **minutes of source video**, rounded up per job; 1 credit = 1 source minute.
- **Free tier**: once per verified identity (one inbox after Gmail dot/+tag folding, one device
  fingerprint, and the risk verdict must allow it), **10 shorts OR 10 source minutes, whichever runs out
  first**, watermarked. Two meters on the account (`free_minutes`, `free_shorts`), separate from paid
  credits; numbers live in the free plan's features (`free_grant_minutes`, `free_grant_shorts`), so
  they are admin-editable. A job is funded entirely by the free meters when they cover its minutes
  *and* its requested clip count, otherwise entirely by paid credits; settling a free job charges the
  minutes used and the clips actually produced and returns the rest. Granted when the email is verified.
- `catalog.public_catalog(db, country: str | None) -> {"currency", "plans": [...], "packs": [...],
  "free": {minutes, shorts, rule, features}}` —
  plans and packs are DB rows editable in admin (seeded from `saas/billing/seed_catalog.json`), with
  per-currency prices and optional regional (PPP) price tiers.
- `ledger.balance(db, user_id) -> int`; `ledger.grant/reserve/settle/release(...)` — append-only ledger,
  idempotency keys, never negative.
  - `ledger.grant(db, user_id, amount, *, kind, idempotency_key, note=None, actor_id=None, payment_id=None,
    bucket="other") -> CreditEntry | None` (None = that key was already applied). Admin adjustments use
    `kind="admin_adjust"` with `actor_id`; a negative adjustment goes through `ledger.debit(...)`, which
    clamps at the balance and records the shortfall in `meta`.
  - `ledger.reserve(db, user_id, job_id, amount, *, shorts=0)` raises `ledger.InsufficientCredits`
    (with `shorts>0` the free meters are tried first; `entry.meta["source"]` is `"free"` or `"credits"`);
    `ledger.settle(db, job_id, actual, *, shorts=None)` / `ledger.release(db, job_id)` share one
    finalisation key per job, so a second settle, a settle after a release, or a release after a settle
    are no-ops. `ledger.grant_free(db, user_id, minutes, shorts, *, idempotency_key)` fills the free
    meters; `ledger.free_balance(db, user_id) -> (minutes, shorts)`. Ledger rows carry the meter changes
    in `free_minutes` / `free_shorts` (`amount` is only ever paid credits).
  - Functions flush, never commit: the caller commits, so a webhook's event row, order transition and
    grant land in one transaction.
- Tables: `billing_plans`, `billing_packs`, `billing_prices`, `billing_region_tiers`,
  `billing_subscriptions`, `billing_payments`, `billing_webhook_events`, `credit_accounts`,
  `credit_ledger`, `billing_promo_codes`, `billing_promo_redemptions`, `billing_referrals`,
  `billing_pending_claims`.
- **Payment providers.** Primary: **Buy Me a Coffee** (`bmc`; no company needed). Memberships = plans,
  shop extras / one-off support = credit packs. Checkout redirects to the item's BMC page
  (`Price.provider_refs["bmc_url"]`, else `SAAS_BMC_PAGE_URL`); the customer must pay with their account
  email. `POST /webhooks/bmc` verifies `x-signature-sha256` (hex HMAC-SHA256 of the raw body with the
  webhook signing secret, `SAAS_BMC_WEBHOOK_SECRET`), ignores dashboard test events (`live_mode=false`)
  and events whose signed `created` is older than `SAAS_BMC_MAX_EVENT_AGE_HOURS`, and is idempotent on
  stable ids (BMC's `event_id` is per delivery attempt): payment `data.id`, membership `data.id` +
  `current_period_start`. Mapping is catalog data on `Price.provider_refs`: `bmc_level_id` /
  `bmc_level_name` (membership level -> plan price, same `duration_type`), `bmc_extra_id` (extra -> pack
  price, x quantity); fallback is an exact amount+currency match. Amounts are BMC major units; paid amount
  must cover our price or the payment is rejected and flagged. Events handled: `donation.created`,
  `extra_purchase.created` (credit a pack), `membership.started`/`membership.updated` (an active
  membership with a new `current_period_start` = a paid period: monthly credits with the rollover cap),
  `membership.cancelled` (ends at period end), `membership.paused`, `donation.refunded`,
  `extra_purchase.refunded` (reverse). The supporter email must belong to a **verified** account;
  otherwise the payment becomes a `billing_pending_claims` row and is credited automatically when that
  email is verified (`saas.billing.bmc.claim_pending`), or by an operator (`scripts/saas_manage.py
  claims` / `claim`). Cancelling happens on BMC (`POST /api/billing/subscription/cancel` answers 409
  `cancel_on_provider` for BMC subscriptions). Stripe, Razorpay and Dodo stay dormant: selected only via
  `SAAS_PAYMENT_PROVIDER_DEFAULT` / `SAAS_PAYMENT_PROVIDER_INR` and configured keys (Dodo is a placeholder).
  BMC pages charge one fixed price, so while BMC is the provider the catalog and checkout use the
  default tier only (no INR/PPP prices), BMC payments are only matched against default-tier prices, and
  percent-off codes are refused at checkout (catalog field `promo_codes_at_checkout: false`; credit
  codes via `POST /api/billing/promo` still work). The catalog also reports `provider`.
- `GET /api/billing/catalog?country=` (public) → the catalog. `POST /api/billing/checkout
  {kind: "plan"|"pack", slug, interval?: "month"|"year", promo?}` → `{redirect_url, payment_id, provider,
  amount}`.
  The price tier is chosen from the server-side country (Cloudflare header / GeoIP), never from
  `?country=`, which only changes what the catalog displays.
  `GET /api/billing/ledger?limit=&before=` → `{balance, entries: [{id, kind, amount, balance_after,
  free_minutes, free_shorts, note, job_id, created_at}], next}`, `GET /api/billing/subscription` →
  `{subscription|null}`,
  `POST /api/billing/subscription/cancel` → `{ok}` (cancels at period end),
  `POST /api/billing/promo {code}` → `{ok, credits_granted?, percent_off?}`,
  `GET /api/billing/payments` → `{payments: [...]}`, `GET /api/billing/referrals` → `{code, url, rewarded, pending}`.
  `GET /billing/return?payment_id=` → 303 `/app/billing?status=paid|pending|failed`.
  Dev only (`SAAS_ENV=dev`, 404 in prod): `GET /billing/dev/complete/{payment_id}?sig=` credits instantly.
  Webhooks: `POST /webhooks/bmc` (primary), `POST /webhooks/stripe`, `POST /webhooks/razorpay`
  (signature-verified, idempotent, amounts re-checked against our own records).
- Refund/chargeback rule: the credits that payment granted are taken back up to the current balance
  (never below zero); any shortfall is recorded on the ledger entry, the user gets `billing_hold=true`
  and a `flags["chargeback"|"refund"]` note, and new jobs are refused until an admin clears the hold.

### Jobs (core agent) — `saas.jobs`
- `POST /api/jobs/estimate {url}` → `{ok, title, duration_s, thumbnail, credits, balance, free: {minutes,
  shorts}, funding: "free"|"credits", reason?, message?}` (a refused link answers 200 with `ok:false`).
- `POST /api/uploads` (multipart, streamed, size-capped, ffprobe-validated) → `{upload_id, duration_s, credits}`.
- `POST /api/jobs {source_url | upload_id, options}` with options
  `{clips: 1-20, language: "auto"|"en"|"hi", layout: "framed"|"fill", captions: bool, broll: bool,
  memes: bool, sfx: bool, music: bool, max_clip_seconds: 15-90, logo: bool (default true: use the
  brand-kit logo), custom_memes: bool (default true: offer the brand-kit memes)}` → job. Reserves
  credits (or the free meters, see Billing). Unknown option keys are a 400.
- `GET /api/jobs?status=&limit=&before=`, `GET /api/jobs/{id}` → `{id, status, stage, progress (0-1),
  source: {kind, url, upload_id, title, duration_s, thumbnail}, options, credits_reserved, credits_charged,
  funding: "free"|"credits", shorts_reserved, shorts_charged, watermark, via,
  created_at, started_at, finished_at, cancel_requested, notes: [str], clips: [...], error: {code, message} | null}`;
  `notes` are customer-safe engine warnings, led by "Only N of the M clips you asked for met our quality
  bar" when the engine finds fewer strong moments than requested (credits are still per source minute).
  `POST /api/jobs/{id}/cancel` → job.
- Job creation errors: 402 `insufficient_credits`, 403 `email_unverified` / `billing_hold`,
  429 `too_many_active_jobs` / `daily_limit`, 400 `source_not_allowed` / `source_too_long` / `invalid_options`.
- Clip object: `{id, job_id, index, title, description, hashtags, duration_s, start_s, end_s, score,
  language, created_at, video_url, download_url, thumbnail_url}`.
- Clips: `GET /api/clips?job_id=&limit=&before=`, `GET/PATCH/DELETE /api/clips/{id}` (`title`, `description`, `hashtags`),
  `GET /api/clips/{id}/video` (inline, ranged), `GET /api/clips/{id}/download`, `.../thumbnail`.
- **No auto-posting.** Each finished clip has a publish-ready package:
  `GET /api/clips/{id}/package?tz=` → `{clip_id, job_id, title, description, hashtags, duration_s, format,
  timezone, schedule_model: "rules-v1", schedule_note, posting, files: {video_url, download_url,
  thumbnail_url, zip_url}, platforms: {youtube_shorts: {label, title (≤100), description (≤5000, with
  #Shorts), tags (≤15 / 500 chars), suggested_times}, instagram_reels: {label, caption (≤2200, ≤30
  hashtags), suggested_times}, tiktok: {label, caption (≤2200, ≤8 hashtags), suggested_times}}}` where
  each `suggested_times` is 3 × `{local (ISO with offset), utc, day, part_of_day}`.
  `GET /api/clips/{id}/package.zip?tz=` → zip of the MP4, thumbnail, `package.json`,
  `youtube_shorts.txt`, `instagram_reels.txt`, `tiktok.txt`, `README.txt`. The time zone is `?tz=`, else
  the account's `timezone` (`PATCH /api/me`), else UTC. Posting-time model (`saas/jobs/schedule.py`,
  rules-v1, fixed local times, 30-minute lead, clip N of a job starts N slots later): YouTube Shorts
  weekdays 12:00/15:00/19:00, weekends 10:00/14:00/19:00; Instagram Reels weekdays 09:00/12:00/19:00,
  weekends 10:00/13:00/19:00; TikTok weekdays 07:00/12:00/19:00/21:00, weekends 11:00/19:00/21:00.
- **Brand kit** (per user, under `SAAS_DATA_DIR/<user>/brand/`): `GET /api/brand` → `{logo, memes, limits}`;
  `POST /api/brand/logo` (multipart `file`; PNG/JPEG/WebP, SVG only when a server-side renderer is
  installed else 415 `svg_unsupported`; ≤5 MB, 16-4096 px; decoded and re-encoded to a ≤1024 px PNG);
  `GET /api/brand/logo/file`; `DELETE /api/brand/logo`; `POST /api/brand/memes` (GIF/PNG re-encoded by
  Pillow, MP4/MOV/WebM ffprobe-checked and transcoded by ffmpeg; ≤15 MB, ≤10 s, ≤50 memes; 415
  `meme_too_long` / `unsupported_type`); `GET /api/brand/memes/{id}/file`; `DELETE /api/brand/memes/{id}`.
  The worker passes them to the engine as `logo_path` and `custom_memes`.
- Uploads: `POST /api/uploads` must be multipart with one file field `file`; the body is streamed and
  aborted at `SAAS_MAX_UPLOAD_MB` (413 `upload_too_large`). **The security middleware's request-size
  cap must exempt `POST /api/uploads`** (this route enforces its own, larger cap while streaming).
  `DELETE /api/uploads/{id}` → `{ok}`. An upload is single-use: once its job is final (done, failed or
  canceled) the source file and the job's scratch dir are deleted; unused uploads expire after
  `SAAS_UPLOAD_TTL_HOURS`; clips are kept `SAAS_CLIP_RETENTION_DAYS`.
- `GET/POST/DELETE /api/keys` (API keys; secret shown once).
- Public API: `/api/v1/*` mirrors jobs/clips/balance with Bearer keys. MCP at `/mcp` (streamable HTTP).
  - `GET /api/v1/balance` → `{balance, plan}`; `POST /api/v1/estimate {url}`; `POST /api/v1/jobs`;
    `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}`, `POST /api/v1/jobs/{id}/cancel`; `GET /api/v1/clips`,
    `GET /api/v1/clips/{id}`, `GET /api/v1/clips/{id}/links` → `{video_url, download_url,
    thumbnail_url, expires_at}` (signed, no key needed, `SAAS_SIGNED_URL_TTL_S`),
    `GET /api/v1/clips/{id}/package?tz=` (same JSON as above, signed links), `PATCH/DELETE /api/v1/clips/{id}`.
  - Signed file URLs: `GET /files/clips/{id}/{kind}?exp=&sig=` (`kind` = video|download|thumbnail|package).
  - MCP tools (docs/saas/MCP.md): get_balance, estimate_video, create_clips, get_job, list_jobs,
    list_clips, get_clip_links, get_publish_package, cancel_job.
  - Per-key rate limit `SAAS_API_RATE_PER_MINUTE`; 401 `invalid_api_key`, 403 `insufficient_scope` /
    `plan_required`.
- Status machine: `queued → fetching → transcribing → selecting → rendering → done | failed | canceled`.
- Engine: `saas.jobs.engine.Engine` protocol; `MockEngine` for tests, `ClipperEngine` wraps
  `app.clipper` in a per-job working directory and wraps the run in `app.clipper.metering.metered()`,
  storing `meter.summary()` on the job (admin shows cost per job). `SAAS_ENGINE=mock|clipper`.
  It passes `cache_dir=<job>/work` (the clipper puts the source download and transcript there, so it
  is private per job and deleted when the job ends), `watermark_text` (free tier), `recent_memes`
  (this customer's), `logo_path` / `custom_memes` (brand kit) and `brand_name=None`.
- Running it: `scripts/saas_serve.py` (uvicorn on `SAAS_HOST:SAAS_PORT` + `--workers N` processes, one
  job each). It forces `DOOMSCROLLER_ENV_FILE=""` before any `app.*` import and exports the non-`SAAS_`
  lines of `SAAS_ENV_FILE` (default `.env.saas`) to the workers: that is where the operator's AI/STT
  keys go (`CLIP_AI_*`, `CLIP_STT_*`, `DEEPGRAM_API_KEY`, `SARVAM_API_KEY`, ...). Never the owner's `.env`.
  `scripts/saas_manage.py`: seed-catalog, make-admin, grant, set-plan, promo, bmc-map, claims, claim.

### Clipper seam (pipeline agent) — `app.clipper.standalone`
The SaaS must not go through `job.clip_source` (it writes the growth DB and reads per-channel state).
The pipeline agent provides:
```python
def clip_to_directory(source: str, out_dir: Path, *, count: int = 3, language: str = "auto",
                      layout: str = "framed", captions: bool = True, broll: bool = True,
                      memes: bool = True, sfx: bool = True, music: bool = False,
                      max_clip_seconds: int = 60, brand_name: str | None = None,
                      watermark_text: str | None = None, recent_memes: Sequence[str] = (),
                      on_stage: Callable[[str, float], None] | None = None,
                      logo_path: str | None = None, custom_memes: Sequence[str] = (),
                      cache_dir: Path | None = None, sfx_pack_dir: Path | None = None,
                      music_dir: Path | None = None, shared_cache_dir: Path | None = None) -> dict
# -> {"source": {"title", "duration_s", "author"}, "clips": [{"path", "thumbnail_path", "duration_s",
#     "start_s", "end_s", "title", "description", "hashtags", "score", "language", "memes_used",
#     "kind", "hook"}], "warnings": [str], "meter": <metering summary>}
```
Landed (pipeline agent). No DB writes, no reads of channel/growth state, every file under `out_dir`
(downloads, stock/meme caches and transcripts go to `out_dir/.cache` unless `cache_dir` is given).
Pass `shared_cache_dir` (one folder for every job) so stock-footage and meme searches and downloads are
shared across jobs (Pixabay requires 24h search caching; Giphy's quota is small); the source and its
transcript always stay private under `cache_dir`.
`on_stage(stage, progress)` gets `fetching`, `transcribing`, `selecting`, `rendering`, `done` with
monotonic progress 0-1; an exception raised from it stops the run (use it to cancel). Raises
`app.clipper.standalone.ClipError(code, message)` when no clip is made; codes: `fetch_failed`,
`unreadable_source`, `no_moments`, `render_failed`, `media_error`. One run at a time per process (the
clipper's options are process-wide settings applied under a lock). Uses the active `metering` meter when
the caller opened one, else its own; `meter` is its summary.
- `memes_used` is a list of opaque meme identity keys (canonical ids and content hashes). Store them per
  customer and pass the recent ones back as `recent_memes`: those memes are never offered again.
- `brand_name=None` means no on-screen handle (never the owner's `BRAND_NAME`). `logo_path` shows an
  image (≤220x88) where the handle would be and replaces it. `watermark_text` is a small 36px line above
  the handle, inside the safe band. The source's creator is not credited on screen (the customer owns it).
- `custom_memes`: the customer's GIF/MP4/WebM/MOV/PNG/JPG/WebP files, offered before the meme library on
  each reaction beat, deduped and given a sound like library memes. Not credited in the description.
- `language`: `"en"`/`"hi"` is a hint (Hindi skips the Deepgram pass and goes to Sarvam); clips follow
  the detected speech. `sfx_pack_dir` defaults to the deployment's configured pack; `music_dir` defaults
  to an empty folder, i.e. the zero-copyright synthesised bed.
- AI calls in a SaaS process should use `CLIP_AI_BACKEND=api` with `CLIP_AI_API_PROVIDER=anthropic`
  (`ANTHROPIC_API_KEY`) or `openai_compatible` (`CLIP_AI_API_BASE_URL`, `CLIP_AI_API_KEY`,
  `CLIP_AI_API_MODEL`; Gemini and Groq free tiers work). Optional STT: `CLIP_STT_PROVIDER=openai_compatible`
  with `CLIP_STT_BASE_URL`/`CLIP_STT_API_KEY` (Groq `whisper-large-v3-turbo`).

### Customer pages (web agent) — `saas.web`
`/` landing · `/pricing` · `/waitlist/thanks` · `/login` · `/signup` · `/verify-email` · `/forgot` ·
`/reset/{token}` · `/privacy` · `/terms` · `/refunds` · `/cookies` · `/developers` (API + MCP docs) ·
`/app` · `/app/new` · `/app/jobs/{id}` · `/app/clips` · `/app/billing` · `/app/settings` ·
`/app/developers` (API keys, MCP connect). Pages call the JSON endpoints above with `fetch`, sending
`X-CSRF-Token` read from the `csrf` cookie. Design tokens live in `saas/web/static/css/tokens.css`;
the admin panel imports them.

## Preliminary pricing (placeholder until docs/saas/costs-and-pricing.md lands)

Seed only; every number is admin-editable data, not code.
- Free: 10 shorts OR 10 source minutes, whichever comes first, once per verified identity (not per
  email), watermark on output (seeded in the free plan's features; see Billing).
- Packs (never expire): 60 / 300 / 1000 credits.
- Plans: Starter, Pro, Studio monthly with included credits, 1-month rollover cap, annual = 2 months free.
  Studio includes API + MCP access and priority queue.
