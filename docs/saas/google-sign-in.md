# Google sign-in: setup checklist

The SaaS app (`saas.main:app`) signs people in with Google only, and sends no email. How it works and what each
route does is in CONTRACT.md under "Google-only sign-in". This page covers what the owner sets up once.

We ask Google for three scopes: `openid`, `email` and `profile`. All three are non-sensitive, so:

- users never see an "unverified app" warning;
- their grants don't expire;
- Google doesn't need to review the app before anyone can use it.

We keep only the name and the email address.

## 1. Google Cloud console

Use a project of its own, separate from the YouTube projects in `channels.json`. Go to
<https://console.cloud.google.com/auth/overview>, then **Google Auth Platform** → **Get started**.

1. **Branding**
   - **App name:** `doomscroller`.
   - **User support email** and **Developer contact email:** your address.
   - **Logo:** leave it empty (see "Why there is no logo" below).
   - Home page, privacy policy and terms links are optional. Any you add must be on an **Authorized domain**.
2. **Audience**
   - **User type:** External.
   - **Publish app**, so the status is **In production**.
   - In *Testing*, only listed test users (100 at most) can sign in, and their grants lapse after 7 days. With only
     these three scopes, publishing needs no review.
3. **Data Access**
   - **Add or remove scopes**: tick `openid`, `.../auth/userinfo.email` and `.../auth/userinfo.profile`.
   - Add nothing else. A sensitive scope would bring back review and the warning screen.
4. **Clients**
   - **Create client** → **Web application**. Name it `doomscroller web`.
   - **Authorized redirect URIs:**
     - `https://doomscroller-app.<account>.workers.dev/auth/google/callback`: where the app is reachable today,
       through the beta Worker (docs/saas/beta-edge.md).
     - `https://doomscroller-studio.pages.dev/auth/google/callback`: only for once that host forwards the app's
       routes (see below).
     - `http://127.0.0.1:8700/auth/google/callback` (local development)
   - **Authorized JavaScript origins:** none. The exchange happens on the server, with PKCE.
   - Copy the **Client ID** and **Client secret**.

## 2. The app's settings (`.env.saas`, never `.env`)

```ini
SAAS_GOOGLE_CLIENT_ID=<client id>.apps.googleusercontent.com
SAAS_GOOGLE_CLIENT_SECRET=<client secret>
SAAS_PUBLIC_URL=https://doomscroller-app.<account>.workers.dev   # exactly the origin of a registered redirect URI
# SAAS_AUTH_GOOGLE_ONLY=        # unset/auto: on, because Google is set and SMTP isn't. true/false to force it.
# SAAS_SMTP_HOST=               # leave empty: Google-only mode sends no email at all
SAAS_ADMIN_EMAILS=you@gmail.com # optional: this Google account becomes an admin and skips the invite gate
```

The app sends `SAAS_PUBLIC_URL + /auth/google/callback` as the redirect URI. Google refuses it with
`redirect_uri_mismatch` unless that exact string is registered, including the scheme, the host, the port and no
trailing slash.

Check the result with `.venv-saas\Scripts\python.exe scripts\saas_serve.py --check`. It should print
`sign-in=google-only mail=off`. In prod it refuses to start Google-only without a client ID and secret.

**Why workers.dev and not pages.dev (as of 2026-11-28).** `doomscroller-studio.pages.dev` is the static marketing
site (`edge/`). It has no `/auth`, `/login` or `/app` route, so a callback sent there would 404. The app runs
behind the `doomscroller-app` Worker on workers.dev.

Switch `SAAS_PUBLIC_URL` to the pages.dev origin only once that host forwards those routes to the app. Keep both
redirect URIs registered, since one client can hold several. The sign-in cookie is tied to the host, so the whole
round trip must stay on one origin.

## 3. Admin access (the owner can't be locked out)

```powershell
.venv-saas\Scripts\python.exe scripts\saas_manage.py grant-admin you@gmail.com
```

`grant-admin` works before the account exists, and while sign-ups are invite-only. Then:

1. Open `/login` and press **Sign in with Google** as that account. You land in `/app`, as an admin.
2. Go to **Settings** → **Turn on two-factor**, scan the QR code and save the recovery codes. The admin panel
   requires 2FA on every session.
3. Open `/admin`.

Each later sign-in goes Google, then the 6-digit code, then back to the page you asked for (`/admin` included).

If an admin session hasn't passed 2FA, `/admin` offers **Enter my 2FA code**, a step-up on the same session. If 2FA
isn't set up yet, it links to Settings.

`SAAS_ADMIN_EMAILS` does the same as `grant-admin` for any address on the list.

## 4. Inviting people (no email)

- `saas_manage.py invite friend@gmail.com --add` prints the person's link. Without `--add`, they must already be on
  the waitlist.
- In the admin panel, **Waitlist** → **Create link** (or **Create invite links** for a batch), then **Copy invite
  link** on their row.

Send the link yourself. It opens `/signup?invite=<code>`, which:

- shows who the invite is for, masked;
- has one button that goes to `/auth/google/start?invite=<code>`, with Google's chooser pre-selecting that address.

An invite works only with the Google account for the email it was issued to. Another account gets a page saying
which one to choose.

## Why there is no logo

Google shows an app's name and logo on its consent screen only after **brand verification**. Until then the
screen shows only the app's domain. Google's help page says: "Without verification, only your application domain
will be visible to users". Uploading a logo starts that verification.

Brand verification needs the home page and privacy policy on **authorized domains** you have proven you own in
**Google Search Console**. `pages.dev` and `workers.dev` are shared hosting suffixes, not a domain the owner
controls, so the practical route is your own domain: add one, verify it with a DNS record, then upload the logo
and submit the brand for review.

Until then, leave the logo empty. Sign-in works the same, and users see Google's standard "Sign in to continue to
<domain>" screen with no warning.

## The button

The sign-in pages use Google's own pre-approved button, unmodified. The files are the light, rectangular PNGs with
text from Google's asset pack, kept in `saas/web/static/vendor/google-signin/`; the source and checksum are in
`NOTICE.txt` there. They are drawn at 216×48 (only scaled, aspect ratio kept).

The pack's text variant reads "Sign in with Google", one of the approved labels. Its SVGs draw the text as outlines
and the "G" with an embedded gradient, so the PNGs are used as they are. Rule: don't recolour, crop or redraw them.
Rules: <https://developers.google.com/identity/branding-guidelines>.
