# Beta access edge: Worker + Workers VPC + BMC relay

How invited beta users reach the product app (`saas.main:app`, `127.0.0.1:8700`) running on the owner's
laptop, at a stable `https://doomscroller-app.<account>.workers.dev` URL with no domain and no spend, and how
Buy Me a Coffee payments survive the laptop being off.

```
browser / API / MCP client
        |  https://doomscroller-app.<account>.workers.dev
        v
Cloudflare Worker  edge-app/  ------------------------------+
  /webhooks/bmc        verify BMC signature, store in D1     |  D1 "doomscroller-relay"
  /_relay/bmc/*        laptop pulls + acks (Bearer token)  <-+
  everything else      + X-DS-Edge-Auth / Client-IP / Sig
        |  env.STUDIO.fetch()   (Workers VPC service, beta)
        v
cloudflared (Windows service, outbound only) -> http://127.0.0.1:8700 (saas_serve.py, SAAS_TRUST_PROXY=edge)
        ^
        +-- saas/billing/bmc_relay.py polls /_relay/bmc/events every 30 s and acks
```

The marketing site and live waitlist (`edge/`, Pages project `doomscroller-studio`) are separate and are not
touched by anything below.

## What it does

| Path on the Worker | Behaviour |
|---|---|
| `POST /webhooks/bmc` | Checks `x-signature-sha256` (hex HMAC-SHA256 of the raw body, BMC's signing secret). A bad signature gets 400. A signed `created` older than `BMC_MAX_EVENT_AGE_HOURS` gets 200 `ignored` and is not stored. Anything else is stored byte-exact in D1, once per distinct body, and answered 200 within the 15 s BMC allows. A D1 failure gets 503, so BMC retries. |
| `GET /_relay/bmc/events?after=&limit=` | `Authorization: Bearer <RELAY_TOKEN>`. Returns queued events oldest first. Does not consume them. |
| `POST /_relay/bmc/ack` | `{acks: [{seq, result, detail?}]}`. A result removes the event from the queue; `result: "error"` counts a failure and keeps it queued. Acked rows are deleted after `RELAY_RETENTION_DAYS` (90), because they hold supporter emails. |
| anything else | Proxied to the laptop through the VPC binding with the three `X-DS-*` headers `saas/auth/edge.py` expects. Client copies of `X-DS-*`, `X-Forwarded-*`, `Forwarded`, `X-Real-IP`, `True-Client-IP` and `CF-*` are dropped. Bodies stream both ways. Redirects go to the browser (`redirect: "manual"`), and `http://<host>` / `127.0.0.1` locations are rewritten to the https origin. |
| laptop unreachable | Browsers get a small branded 503 page that retries every 30 s. `/api`, `/mcp`, `/files` and `/webhooks` get `{"error": "studio_offline", "message": ...}`. A GET with no answer within `UPSTREAM_HEADERS_TIMEOUT_MS` (60 s) gets 504 `studio_timeout`. Requests with bodies have no deadline, because an upload's answer comes after the whole body. Errors the app returns itself, 5xx included, pass through unchanged. |

On the laptop, `saas/billing/bmc_relay.py` checks each pulled event again, with the same code as a direct
`POST /webhooks/bmc`: `providers.verify_bmc_signature`, the test-event rule, then `process_event` with
`bmc.stable_key`. A relay that altered an event therefore cannot credit anything, and BMC retries of one
payment are credited once. One rule differs on purpose. Age is measured from when the relay **received** the
event, not from now, so a laptop that was off for a week still credits last week's payments.
`scripts/saas_serve.py` starts the poller in the web process whenever `SAAS_BMC_RELAY_URL` and
`SAAS_BMC_RELAY_TOKEN` are set. To run it by hand: `.venv-saas\Scripts\python.exe -m saas.billing.bmc_relay --once`.

## Setup, in order

Run every `npx wrangler` command **from `edge-app\`**, so that `edge-app\wrangler.toml` (Worker
`doomscroller-app`, database `doomscroller-relay`) is the config in use. `edge-app\scripts\setup.ps1` walks
through the same steps one at a time. It asks before each command and writes the two ids into
`wrangler.toml`.

> **Live Pages project.** No step here touches the marketing site. These commands would, so do not run them as
> part of this setup: anything run from `edge\` (`npm run deploy`, `npm run db:migrate:remote`),
> `wrangler pages ...`, and any `wrangler d1 ...` naming `doomscroller-waitlist`. The Worker free quota (100,000
> requests a day) is **shared** with the live site's Pages Functions. See "Budget".

### 1. Tools and login

```powershell
cd edge-app
npm install                 # wrangler 4.161.0 + miniflare, pinned to the versions edge\ uses
npm test                    # 39 local tests on the real runtime (7 run the real app from .venv-saas); no account needed
npx wrangler login          # browser sign-in, once. The same login edge\ uses; changes nothing on its own
npx wrangler whoami         # check you are on the right account
```

### 2. Secrets for the laptop

```powershell
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"   # run twice: EDGE_SECRET, RELAY_TOKEN
```

Add to `.env.saas` at the repo root (the SaaS env file; never the owner's `.env`):

```
SAAS_EDGE_SECRET=<first value>
SAAS_BMC_RELAY_TOKEN=<second value>
```

Both values are uploaded to the Worker in step 6. They must match exactly.

### 3. D1 database for the relay (new, never the waitlist's)

```powershell
npx wrangler d1 create doomscroller-relay        # prints database_id
```

Paste the printed `database_id` into `edge-app\wrangler.toml` under `[[d1_databases]]`, replacing the
zero placeholder. Then create the table:

```powershell
npx wrangler d1 migrations apply DB --remote     # edge-app\migrations\0001_bmc_relay.sql -> doomscroller-relay
```

### 4. Tunnel and VPC service

**4a. Install cloudflared** (2025.7.0 or later). Download
`https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi` and install it.
On Windows, cloudflared does not update itself, so update it by hand. It needs outbound UDP 7844 (QUIC) through
the firewall.

**4b. Create the tunnel in the dashboard.** Go to Workers VPC → **Tunnels** → **Create**, name it
`doomscroller-laptop`, then save. The dashboard shows an install command with a token (`eyJ...`).
In an **Administrator** Command Prompt run:

```bat
cloudflared.exe service install <TUNNEL_TOKEN>
```

This installs cloudflared as a Windows service with that token, as the Workers VPC get-started page
describes. Wait for the tunnel to show **Healthy**, then copy its **Tunnel ID** (a UUID). No public hostname or
domain is needed: Workers VPC routes through the VPC service, and ingress rules are "only relevant when using
tunnels to expose services to the public internet".

**4c. Register the laptop app as a VPC service.**

```powershell
npx wrangler vpc service create doomscroller-laptop --type http --tunnel-id <TUNNEL_ID> --ipv4 127.0.0.1 --http-port 8700
```

- Only `--http-port` is given, so the service is plain HTTP. Per the VPC services page, traffic is
  encrypted until it reaches your network through the tunnel. Only the tunnel-to-app leg is plaintext, and
  that leg is loopback on the laptop.
- **Untested choice: `--ipv4 127.0.0.1`.** The app binds IPv4 loopback only (`SAAS_HOST=127.0.0.1`). The
  docs' example hosts include `localhost` and private IPs, but no page shows a loopback IP. If `npx wrangler tail`
  shows `studio unreachable: ... destination_ip_prohibited`, switch to the documented hostname form:

  ```powershell
  npx wrangler vpc service update <SERVICE_ID> --name doomscroller-laptop --type http --tunnel-id <TUNNEL_ID> --hostname localhost --http-port 8700
  ```

  If that then shows `connection_refused`, the tunnel probably resolved `localhost` to `::1`, where the app
  is not listening. Go back to `--ipv4 127.0.0.1` and report the first error. An update replaces the whole
  service configuration.

Paste the printed service id into `edge-app\wrangler.toml` under `[[vpc_services]]`, replacing the zero
placeholder. Creating services needs the **Connectivity Directory Admin** role. Deploy error `10196` means
the deploying member or API token lacks **Connectivity Directory Bind** or **Admin**; after a role change, run
`npx wrangler logout` and then `npx wrangler login`.

### 5. Check before deploying

```powershell
npm test
npx wrangler deploy --dry-run --outdir .wrangler\dry-run   # bundles and lists bindings; uploads nothing
```

The dry run must list `env.STUDIO (<service id>) VPC Service` and `env.DB (doomscroller-relay)`.

### 6. Deploy the Worker with its secrets

Create a JSON file **outside the repo**, for example `%TEMP%\edge-secrets.json`:

```json
{"EDGE_SECRET": "<SAAS_EDGE_SECRET>", "RELAY_TOKEN": "<SAAS_BMC_RELAY_TOKEN>"}
```

```powershell
npx wrangler deploy --secrets-file $env:TEMP\edge-secrets.json
Remove-Item $env:TEMP\edge-secrets.json
```

This creates the Worker `doomscroller-app` and prints its URL, `https://doomscroller-app.<account>.workers.dev`.
Until `BMC_WEBHOOK_SECRET` is set (step 8), `/webhooks/bmc` answers 503 `not_configured`.

### 7. Point the laptop app at the edge

Add to `.env.saas`:

```
SAAS_TRUST_PROXY=edge
SAAS_PUBLIC_URL=https://doomscroller-app.<account>.workers.dev
SAAS_BMC_RELAY_URL=https://doomscroller-app.<account>.workers.dev
SAAS_MAX_UPLOAD_MB=95
```

Then restart `.venv-saas\Scripts\python.exe scripts\saas_serve.py`.

- `SAAS_PUBLIC_URL` must be the exact https origin. The app sees plain `http` from the tunnel, so its CSRF
  origin check relies on this value. Without it, every sign-in and form post is refused as `bad_origin`.
- `SAAS_MAX_UPLOAD_MB=95`: the Workers limits page sets the maximum request body at **100 MB** on the Free
  plan, and larger requests get Cloudflare's own `413 Request entity too large`. Keeping the app's cap below
  that means users see the app's own message instead. Long videos should be sent as links.
- In edge mode the app refuses every request that did not come through the Worker, including
  `http://127.0.0.1:8700` in your own browser (403 `edge_only`). Use the workers.dev URL for everything,
  `/admin` included. The admin panel still needs an admin account with TOTP.

### 8. Buy Me a Coffee webhook

1. In BMC, open the webhooks settings and add a webhook with the URL
   `https://doomscroller-app.<account>.workers.dev/webhooks/bmc`. Choose the events `membership.*`,
   `extra_purchase.*` and `donation.*`. Copy the **Signing Secret**.
2. Put the secret on the Worker, using the same JSON-file method so no newline gets into it:

   ```powershell
   # %TEMP%\bmc-secret.json = {"BMC_WEBHOOK_SECRET": "<signing secret>"}
   npx wrangler secret bulk $env:TEMP\bmc-secret.json
   Remove-Item $env:TEMP\bmc-secret.json
   ```

3. Add `SAAS_BMC_WEBHOOK_SECRET=<the same signing secret>` to `.env.saas`, then restart `saas_serve.py`.
4. Click **Send test event** in BMC. The relay stores it, and the laptop pulls it and acks it as `ignored`
   (test events are never credited in prod).

Do steps 1 to 3 back to back. BMC disables a webhook after **10 consecutive failed deliveries**, and a delivery
that arrives before the secret is set is one of them. A wrong secret fails every delivery. Rolling the signing
secret in BMC invalidates the old one immediately, so update the Worker and `.env.saas` first.

### 9. Verify

```powershell
curl.exe -s -o NUL -w "%{http_code}\n" https://doomscroller-app.<account>.workers.dev/login    # 200 (laptop on) / 503 (off)
curl.exe -s https://doomscroller-app.<account>.workers.dev/api/me                             # app JSON, or studio_offline
curl.exe -s -H "Authorization: Bearer <SAAS_BMC_RELAY_TOKEN>" https://doomscroller-app.<account>.workers.dev/_relay/bmc/events
npx wrangler d1 execute DB --remote --command "SELECT seq, type, live_mode, result, failures, last_error FROM bmc_events ORDER BY seq DESC LIMIT 10"
npx wrangler tail                                                  # live Worker logs (Ctrl+C to stop)
cd ..; .venv-saas\Scripts\python.exe -m saas.billing.bmc_relay --once   # repo root: one pull, JSON summary
```

What the errors mean:

| You see | Cause |
|---|---|
| 403 `edge_only` through the Worker | `EDGE_SECRET` on the Worker differs from `SAAS_EDGE_SECRET` |
| 403 `bad_client_ip` through the Worker | the laptop clock is more than 2 minutes off (the IP signature is per minute, accepted ±2). Keep Windows time sync on |
| 403 `bad_origin` on sign-in | `SAAS_PUBLIC_URL` is not exactly the Worker's https origin |
| offline page while the app runs | the tunnel or VPC service. `npx wrangler tail` shows `studio unreachable: <reason>`; the reasons are listed on the Workers VPC troubleshooting page |
| `failures > 0`, `last_error` mentions `BMC_WEBHOOK_SECRET` | the Worker's and the laptop's BMC secrets differ. The event stays queued and is processed on the next poll after the fix |

## Day to day

- **Laptop off or asleep.** Users see the offline page. BMC payments queue in D1 and are credited within one
  poll (30 s) of the app starting again.
- **Rotate `EDGE_SECRET` or `RELAY_TOKEN`.** Change it in `.env.saas`, then `npx wrangler secret bulk <file>`,
  then restart the app. In between, requests are refused (403 `edge_only`) or polls fail; nothing is lost.
- **Take the beta down.** Stop `saas_serve.py`. Users get the offline page and BMC still queues.
- **Logs.** `npx wrangler tail` for the Worker. The poller prints `[saas] bmc relay: ...` lines in the
  `saas_serve.py` console when it handles events or when its error changes.

## Budget and limits (free plan)

- **Requests.** The Workers free plan allows 100,000 requests a day per account, reset at midnight UTC, and
  answers Error 1027 when that is exceeded. This Worker shares the allowance with the live site's Pages
  Functions. Every proxied request counts, including each CSS, JS and font file of an app page, each job-status
  poll, and each relay poll (2,880 a day at 30 s). If the quota runs out, BMC deliveries fail too and count
  toward the 10 failures that disable the webhook.
- **CPU.** 10 ms per request on the free plan. Proxying a stream and one HMAC use far less.
- **Request bodies.** 100 MB on the free plan (see step 7). Response bodies have no limit.
- **D1.** One small write per BMC delivery, a few row reads per poll, and an indexed delete on ack.
- **Workers VPC** is in beta: "available for free to all Workers plans" while in beta, and "Features and APIs may
  change before general availability". It allows 1,000 VPC services per account, and standard Workers limits apply.
- **Video.** Cloudflare's CDN terms (last updated 28 Sep 2026) say non-Enterprise customers must use paid
  services, naming "the Developer Platform, Images, and Stream", to serve video and other large files via the
  CDN. Clips here are streamed through a Worker, which is the Developer Platform. The terms do not say whether
  the free Workers plan counts. This is the most defensible $0 route, not a guaranteed one. Nothing is cached,
  and media keeps the app's `Cache-Control: private, no-store`.

## Sources (read 2026-11-27)

- Workers VPC: https://developers.cloudflare.com/workers-vpc/ ·
  get started https://developers.cloudflare.com/workers-vpc/get-started/ ·
  VPC services https://developers.cloudflare.com/workers-vpc/configuration/vpc-services/ ·
  tunnel https://developers.cloudflare.com/workers-vpc/configuration/tunnel/ ·
  binding API https://developers.cloudflare.com/workers-vpc/api/ ·
  wrangler commands https://developers.cloudflare.com/workers-vpc/reference/wrangler-commands/ ·
  troubleshooting https://developers.cloudflare.com/workers-vpc/reference/troubleshooting/ ·
  limits https://developers.cloudflare.com/workers-vpc/platform/limits/ ·
  pricing https://developers.cloudflare.com/workers-vpc/platform/pricing/
- Cloudflare Tunnel: https://developers.cloudflare.com/tunnel/get-started/ ·
  downloads https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/
- Workers limits https://developers.cloudflare.com/workers/platform/limits/ ·
  streams https://developers.cloudflare.com/workers/runtime-apis/streams/ ·
  timing-safe comparison https://developers.cloudflare.com/workers/examples/protect-against-timing-attacks/
- Cloudflare Service-Specific Terms (CDN, video) https://www.cloudflare.com/service-specific-terms-application-services/
- Buy Me a Coffee webhooks https://help.buymeacoffee.com/en/articles/15743173-how-to-setup-and-use-buy-me-a-coffee-webhooks ·
  OpenAPI spec v1.0.0 https://cdn.buymeacoffee.com/assets/integrations/bmc-webhooks-openapi.json
- The installed `wrangler 4.161.0`: `wrangler vpc service create --help`, `wrangler deploy --help`
  (`--secrets-file`, `--dry-run`), `wrangler secret bulk --help`.
