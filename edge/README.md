# Edge: static site + waitlist on Cloudflare Pages (free tier)

The launch stack: the static site in `public/` (exported by `scripts/saas_export_static.py`),
Pages Functions in `functions/` for the waitlist API and a small admin page, and one D1 (SQLite)
database. Everything here runs on Cloudflare's free plan. No Python runs on Cloudflare.

| Path | What |
|---|---|
| `POST /api/waitlist` | join: `{email, role?, channel_url?, ref?, ts, website, cf_turnstile_response?, page_url?, referrer?}` → `{ok, already, position, referral_url, referral_code}`; errors `{ok:false, error, message}` |
| `GET /api/waitlist/me?code=` | `{ok, status, position, total, referrals, referral_url}` (never the email) |
| `GET /admin` | read-only dashboard (HTTP Basic, `ADMIN_PASSWORD`) |
| `GET /admin/export.csv` | every entry, spreadsheet-safe CSV |
| `functions/_middleware.js` | security headers + per-response CSP nonce on every page |

## Deploy (first time)

All commands run from `edge/`. You need Node 20+ and a free Cloudflare account.

1. **Install the tools**

   ```sh
   cd edge
   npm install
   npx wrangler login          # opens the browser once
   ```

2. **Create the database** and put its id in `wrangler.toml`

   ```sh
   npx wrangler d1 create doomscroller-waitlist
   ```

   Copy the printed `database_id` into `wrangler.toml` (replace the `00000000-...` placeholder).

3. **Create the tables**

   ```sh
   npm run db:migrate:remote
   ```

4. **Create the Pages project** (name must match `wrangler.toml`)

   ```sh
   npx wrangler pages project create doomscroller --production-branch main
   ```

5. **Set the secrets** (never put these in `wrangler.toml` or git)

   ```sh
   # 64 random hex characters; keep it forever (changing it breaks duplicate/referral checks for old rows)
   node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
   npx wrangler pages secret put IP_HASH_SALT --project-name doomscroller

   # 20+ characters, from a password manager
   npx wrangler pages secret put ADMIN_PASSWORD --project-name doomscroller
   ```

   Optional: `ADMIN_USER` (default `admin`), `DISPOSABLE_EXTRA_DOMAINS` / `DISPOSABLE_ALLOW_DOMAINS`
   (comma-separated domains to block / let through on top of the bundled list).
   Without `IP_HASH_SALT` the live waitlist answers `503 misconfigured` on purpose; without
   `ADMIN_PASSWORD` (12+ chars) `/admin` is disabled.

6. **Turnstile** (free bot check, strongly recommended before you share the link)

   - Cloudflare dashboard → **Turnstile** → **Add widget** → hostnames `doomscroller.pages.dev`
     (plus any custom domain later), mode **Managed**.
   - The **site key** goes into the page (the web export reads `SAAS_TURNSTILE_SITE_KEY`).
   - The **secret key** goes here:

     ```sh
     npx wrangler pages secret put TURNSTILE_SECRET_KEY --project-name doomscroller
     ```

   With the secret set, a join without a valid token is refused (`turnstile_failed`); if Cloudflare's
   verify endpoint cannot be reached it also refuses (fails closed). Without it, Turnstile is skipped.

7. **Build the site and deploy**

   ```sh
   # from the repo root: writes edge/public/. All optional; an unset id leaves that snippet out.
   #   GA4_MEASUREMENT_ID=G-XXXXXXXXXX  CLARITY_PROJECT_ID=xxxxxxxxxx  SAAS_TURNSTILE_SITE_KEY=0x4AAA...
   .venv-saas/Scripts/python.exe scripts/saas_export_static.py
   cd edge
   npm run deploy
   ```

   The CSP that browsers enforce on pages comes from `functions/_middleware.js` (it already allows
   Turnstile, Google Analytics and Clarity, plus a per-response nonce for any inline snippet), so adding
   or removing those ids needs no header change.

   `npm run deploy` passes `--branch main`, so it is a **production** deploy whatever git branch you
   are on (secrets are per environment: a preview deploy would not see them and the waitlist would
   answer `503 misconfigured`).

   Then check the live site:

   ```sh
   curl -sI https://doomscroller.pages.dev/ | grep -ci "^content-security-policy"   # must print 1
   curl -s -X POST https://doomscroller.pages.dev/api/waitlist -H "Content-Type: application/json" -d "{}"
   #  -> {"ok":false,"error":"invalid_email",...}   (503 misconfigured = IP_HASH_SALT missing)
   ```

   If the first command prints `2`, `public/_headers` still carries a `Content-Security-Policy` line and
   Pages is sending both; browsers enforce both, which blocks Turnstile / GA / Clarity. Remove that line
   from the export (the middleware's CSP is the one that matters) and deploy again.

   Open `https://doomscroller.pages.dev`, join with your own email, then open `/admin`.

8. **Put Cloudflare Access in front of `/admin`** (free Zero Trust, up to 50 users). This is the
   first lock; the Basic password stays as the second.

   - Cloudflare dashboard → **Zero Trust** (pick a team name, Free plan).
   - **Access → Applications → Add an application → Self-hosted.**
   - Application domain: `doomscroller.pages.dev`, path `admin` (add a second entry with path
     `admin/*`).
   - Policy: **Allow**, *Include → Emails →* your email. Login method: **One-time PIN**.
   - Session duration: 24 hours.
   - Then `https://doomscroller.pages.dev/admin` first asks Access for an emailed code, then the Basic
     password.

9. **Preview deployments.** Every non-`main` branch deploy gets a `*.doomscroller.pages.dev` URL and
   uses the **same D1 database**. Either deploy only `main`, or in Pages → Settings → General turn on
   **Access policy for preview deployments** so strangers cannot write to production data through a
   preview URL.

## Free-tier budget

- Pages Functions share the Workers free quota: **100,000 requests/day**. The root middleware runs for
  every request it routes, including static files. Ask for `public/_routes.json` with
  `{"version": 1, "include": ["/*"], "exclude": ["/static/*", "/favicon.ico", "/og.png", "/robots.txt",
  "/sitemap.xml", "/site.webmanifest"]}` so CSS/JS/fonts/images are served directly (they keep the
  headers from `public/_headers`). A page view then costs one Function request, not ~10.
- D1 free: 5 GB, 5 million rows read and 100k rows written per day. A join reads a few hundred rows
  (position count) and writes ~3; the admin dashboard reads every row a few times per load.

## Day-to-day

```sh
npx wrangler d1 execute DB --remote --command "SELECT COUNT(*) FROM waitlist"
npx wrangler d1 execute DB --remote --command "UPDATE waitlist SET status='removed' WHERE email='spam@example.com'"
npx wrangler d1 execute DB --remote --command "UPDATE waitlist SET flagged=1, risk='manual' WHERE seq=123"
npx wrangler d1 export DB --remote --output backup-$(date +%F).sql     # full backup
npx wrangler pages deployment tail --project-name doomscroller         # live logs
```

D1 Time Travel can restore the database to any minute in the last 7 days on the free plan
(`npx wrangler d1 time-travel restore DB --timestamp=...`).

## How the waitlist defends itself

- **Honeypot** (`website` must stay empty): bots get a convincing `200 ok` and nothing is stored.
- **Form timing**: a submit less than 1.2 s after the page set `ts` is refused (`too_fast`).
- **Turnstile** when `TURNSTILE_SECRET_KEY` is set (fails closed).
- **Rate limits in D1**, shared by every Cloudflare location: 5 joins/minute and 20/hour per IP,
  5 attempts/hour per email address, 60 `/me` lookups/minute per IP, 10 wrong admin passwords per IP per
  15 minutes (then a lockout, checked before the password).
- **Email**: syntax validated, lower-cased, Gmail dots / `+tags` collapsed for duplicate detection,
  10,455 disposable domains blocked (CC0 list, `npm run gen:disposable` to refresh) with a never-block
  list for the big providers.
- **Scripted clients** (curl, python-requests, headless Chrome, Playwright/Puppeteer/Selenium ...) are
  refused; self-declared crawlers, datacenter/VPN networks, a third join from one IP within an hour, or
  10+ from one /24 in a day are **flagged**: they keep their place but never earn referral credit and
  they show up in `/admin`.
- **Referrals** count only when the new entry is not flagged, comes from a different /24 (IPv6: /48)
  than the referrer, is not an alias of the referrer's address, and no earlier counted referral of the
  same referrer came from that network; at most 25 per referrer per day. Each counts once, at join time,
  and moves the referrer up `REFERRAL_BUMP` (5) places.
- **Duplicates**: an address (or its alias) already on the list returns `already: true`; the position and
  referral link only go back to the same IP that joined, so typing someone else's email reveals nothing.
- **IPv6**: per-visitor checks (rate limits, bursts, "same person" on duplicates, admin lockout) key on
  the /64, because one connection owns a whole /64 and rotates addresses inside it; the referral network
  rule uses the /48. `::ffff:1.2.3.4` counts as `1.2.3.4`.
- **Same-origin** check on the join POST; body capped at 16 KB.
- **Headers**: per-response CSP nonce stamped on the page's own inline scripts (so a GA / Clarity
  snippet works while injected scripts do not), Turnstile + Google Analytics + Clarity hosts allowed,
  `frame-ancestors 'none'`, HSTS (2 years, subdomains), nosniff, strict referrer policy, Permissions-Policy.

## Privacy (for the privacy policy)

Stored per entry: email, optional role and channel URL, join time, referral code and who referred them,
**a keyed hash of the IP address and of its /24 network (no raw IP)**, country / region / city /
timezone / network (ASN and its name) / Cloudflare data centre as reported by Cloudflare, the browser's
user-agent string, the external referrer (origin + path, never its query string), landing path and UTM
tags. The salt for the hashes is a secret that never leaves Cloudflare. Nothing is shared with third
parties by this backend. Raw IPs stay in Cloudflare's own logs/analytics only.

## Not verifiable without a Cloudflare account

The local runtime (workerd + D1 via miniflare) covers the code paths, but these were not exercised:
the remote deploy itself, production secrets, Cloudflare Access on `/admin`, a real Turnstile widget
in a browser (server-side verify was tested against Cloudflare's live endpoint with its test secret),
and whether Pages adds `public/_headers` on top of the middleware (the curl check above).

## Tests

```sh
npm test
```

Runs `wrangler pages dev` (real workerd runtime + local D1) on the real `public/` site (or
`test/fixtures/public` if it is not exported yet), applies the migrations to a fresh database, and
checks joins, duplicates, every rejection, rate limits, referral rules, `/me`, admin auth + lockout,
CSV formula escaping and the security headers over plain HTTP. No Cloudflare account needed.
