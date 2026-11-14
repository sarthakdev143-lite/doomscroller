# Doomscroller

Autonomous YouTube Shorts factory. Scrapes viral short-form videos from
Snapchat Spotlight and creator profiles → human approves/rejects via dashboard →
app auto-transforms to vertical Shorts format (or chops long-form into clips) →
auto-generates SEO metadata via LLM (preserving source language) → auto-publishes
to your YouTube channel on a paced 8-uploads-per-day schedule.

**The user's only job: approve or reject videos. Everything else is automated.**

## How it runs

```
┌─────────────────────────────────────────────────────────────────────────┐
│             Unified Slim Process (scripts/serve_slim.py)                │
│                                                                         │
│  ┌────────────────────────────────────┐  ┌───────────────────────────┐  │
│  │ Private FastAPI API (:8000)        │  │ Control Panel (:8600)     │  │
│  │  • /videos/*, /metadata/*, /clip   │  │  • REVIEW_TOKEN cookie    │  │
│  │  • /scheduler/*, /storage          │  │  • Review / Up Next /     │  │
│  │  • APScheduler autonomous loop     │  │    Scraped / Posted       │  │
│  │  • Clip queue worker               │  │  • Clip, Channels, Status │  │
│  └────────────────────────────────────┘  │    Report, Controls, PWA  │  │
│                                          └─────────────▲─────────────┘  │
└────────────────────────────────────────────────────────┼────────────────┘
                                                         │ HTTPS (optional)
                                             ┌───────────┴───────────┐
                                             │  Cloudflare Quick     │
                                             │  Tunnel -> Phone UI   │
                                             └───────────────────────┘
```

No Docker. No Redis. No Celery. No Streamlit overhead (~55 MB idle RAM). One command starts everything.

## Clip a long video

Paste a link or drag-and-drop a video file into the Control Panel's **✂ Clip** tab (at `http://localhost:8600`), choose a target channel, or run:

```bash
venv/bin/python scripts/clip.py "https://www.youtube.com/watch?v=..." --count 3 --channel tech
venv/bin/python scripts/clip.py "https://www.youtube.com/watch?v=..." --mine --queue   # cut fresh unused moments
venv/bin/python scripts/clip.py --jobs --json                                          # inspect queued/running/done jobs
venv/bin/python scripts/clip.py --inbox                                                # clip every new video dropped in content/inbox/
venv/bin/python scripts/clip.py --batch links.txt --enqueue                            # one link/path per line (blank lines and # comments ignored)
venv/bin/python scripts/clip.py --retry <JOB_ID>                                       # re-run a failed job
venv/bin/python scripts/clip.py --clear-finished --json                                # hide finished jobs from the list
```

English speech gets AI-picked moments, tightened pauses and word-by-word captions.
Video with no speech is ranked on motion, sound and scene cuts, and the AI judges
frames from the best candidates. Needs `ffmpeg` and, for captions, `DEEPGRAM_API_KEY`.
`PIXABAY_API_KEY` adds stock B-roll. `TENOR_API_KEY` (or `GIPHY_API_KEY`) plus a weekly
`venv/bin/python scripts/fetch_memes.py` adds trending meme overlays on punch beats —
without any key the keyless MemeGen template pack still downloads. `FREESOUND_API_KEY`
plus `venv/bin/python scripts/fetch_sfx.py` downloads trending CC0 sound effects into the
SFX pack; one-shots dropped in `content/sfx/` (e.g. `whoosh.wav`) replace the matching synth cue.
The AI runs through your local `opencode` login, with its tools switched off.

## Autonomous schedule (default)

| Job          | Interval | Purpose                                                    |
|--------------|----------|------------------------------------------------------------|
| `scrape`     | 6 hours  | Pull new viral videos from seed + discovered creators     |
| `discovery`  | 24 hours | Find new creators from the authors of top viral videos    |
| `recompute`  | 1 hour   | Refresh engagement + velocity scores                       |
| `publish`    | 3 hours  | Auto-publish next transformed video (8/day, quota-aware)  |
| `cleanup`    | 24 hours | Delete downloaded source files older than 7 days          |

All configurable in `.env`. On startup, `scrape` runs immediately so you have
content to curate right away.

## Quickstart (mock mode — zero credentials)

On Windows, double-click `start.bat` (or run `.\start.ps1`; stop anytime with `stop.bat` or `.\stop.ps1`). It creates `venv`, installs requirements, auto-creates `.env` with a generated `REVIEW_TOKEN`, initializes SQLite, starts the background daemon, and opens your authenticated Control Panel in the browser.

Or from any terminal:

```bash
cd doomscroller
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run offline system preflight (bootstraps missing dirs, .env + REVIEW_TOKEN, and SQLite DB)
python scripts/doctor.py --fix
python scripts/seed.py 20

# Start everything (API :8000 + scheduler + Control Panel :8600) in foreground
# Automatically creates .env from .env.example and generates REVIEW_TOKEN on first run!
python scripts/run.py
```

Open the printed `http://localhost:8600/?key=...` URL — you'll see pending mock videos under **Review → Scraped**. Approve a few → the scheduler transforms them, generates metadata, and publishes on cadence.

For background daemon mode (logs to `logs/`):
```bash
python scripts/run.py --daemon     # start in background and print unlock link
python scripts/run.py --ensure     # idempotent watchdog check (start only if stopped)
python scripts/run.py --panel-url  # print your local one-click unlock URL
python scripts/run.py --status
python scripts/run.py --stop
```

On Windows, install silent logon + 10-minute watchdog auto-start (no admin needed):
```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows_autostart.ps1 install
powershell -ExecutionPolicy Bypass -File scripts\windows_autostart.ps1 status
```

## Going to production (real scraping + real YouTube)

1. **Install ffmpeg**: `apt install ffmpeg`, `brew install ffmpeg`, or on Windows `winget install Gyan.FFmpeg`
2. **Edit `.env`**:
   ```env
   USE_MOCK=false
   USE_LLM_METADATA=true                 # uses free z-ai CLI
   TARGET_HANDLES=["@creator1","@creator2"]  # seed Snapchat handles
   TARGET_HASHTAGS=["fyp","viral"]
   YOUTUBE_CLIENT_SECRETS=/abs/path/client_secret.json
   YOUTUBE_DEFAULT_PRIVACY=public        # or private until you trust it
   AUTO_PUBLISH=true                     # autonomous mode
   UPLOADS_PER_DAY=8
   UPLOAD_INTERVAL_HOURS=3
   ```
3. **Get the YouTube token**: see [YouTube token](#youtube-token).
4. **Run it**:
   ```bash
   python scripts/run.py --daemon
   ```
   Uploads use the token saved at `YOUTUBE_TOKEN_PATH` and refresh it on their own.

That's it. The app will now:
- Scrape Snapchat profiles + Spotlight every 6h (parallel per-URL browser contexts)
- Download videos directly from Snapchat's CDN
- Discover new creators daily
- Show you pending videos in the dashboard
- When you approve, transform + generate metadata automatically (or chop long-form into Shorts clips)
- Publish 8 videos per day, 3 hours apart, to your channel
- Track quota, clean up old source files, boost creators whose videos perform well

## Project layout

```
doomscroller/
├── app/
│   ├── main.py             # FastAPI app + lifespan-started scheduler
│   ├── config.py           # Pydantic settings
│   ├── db.py               # engine, session, init_db
│   ├── models.py           # Video, TransformJob, QuotaUsage, Creator, MetadataDraft
│   ├── schemas.py
│   ├── analytics.py        # engagement + velocity scoring
│   ├── quota.py            # daily upload quota tracker
│   ├── metadata.py         # LLM metadata generation (z-ai CLI + fallback)
│   ├── discovery.py        # creator auto-discovery from engagement graphs
│   ├── scheduler.py        # APScheduler autonomous loop
│   ├── review.py           # Authenticated Web & Phone Control Panel (:8600)
│   ├── review_ui.py        # Self-contained dark-mode PWA UI (touch + keyboard)
│   ├── winproc.py          # Windows silent background process & RAM-trimming helpers
│   ├── growth/             # Autonomous multi-channel growth loop, A/B model, safety
│   ├── scrapers/           # Snapchat (HTTP + CDN) + yt-dlp + mock scrapers
│   ├── transformers/       # FFmpeg vertical Shorts transformer
│   ├── clipper/            # Long video or link → finished Shorts
│   └── publishers/         # YouTube OAuth2 + mock publishers
├── tasks/
│   └── pipeline.py         # scrape/transform/publish/metadata functions
├── scripts/
│   ├── init_db.py
│   ├── seed.py
│   ├── doctor.py           # Offline system preflight inspector (--fix, --json)
│   ├── final_audit.py      # Platform routing, DB/storage & repo surface audit (--offline, --json)
│   ├── channels.py         # Multi-channel CLI (add, connect, set, feed, export, import, clone)
│   ├── growth.py           # Growth loop CLI (status, doctor, report, pause, resume, unblock)
│   ├── clip.py             # Long video/URL -> finished Shorts CLI
│   ├── serve_slim.py       # Single-process uvicorn host (:8000 + :8600 + tunnel)
│   ├── windows_autostart.ps1 # Windows Task Scheduler logon + 10m watchdog
│   └── run.py              # One-command launcher (foreground, daemon, --ensure)
├── .github/workflows/ci.yml # Ubuntu + Windows CI (JS check, final_audit, fast pytest)
├── tests/                  # pytest
├── content/                # downloaded source videos (gitignore this)
├── secrets/                # OAuth token cache (gitignore this)
├── logs/                   # daemon logs (gitignore this)
├── start.bat / start.ps1   # Windows one-click starter
├── stop.bat / stop.ps1     # Windows one-click stopper
├── CONTRIBUTING.md         # Architecture invariants, TDD workflow & CLI reference
├── requirements.txt
├── .env.example
└── README.md
```

## API reference

### Videos
| Method | Path                              | Purpose                          |
|--------|-----------------------------------|----------------------------------|
| GET    | `/videos/pending`                 | Top uncurated videos             |
| GET    | `/videos`                         | All videos (filter by `?status=`)| 
| GET    | `/videos/{id}`                    | Single video                     |
| POST   | `/videos/{id}/approve`            | Approve / reject (body: `{approved: bool, note?: str}`) |
| POST   | `/videos/{id}/transform`          | Queue transformation             |
| POST   | `/videos/{id}/publish`            | Queue YouTube publish            |
| POST   | `/clip`                           | Queue a link or file for clipping |
| GET    | `/clip/jobs`                      | Clip job status and results |
| POST   | `/videos/{id}/chop`               | Clip a video already on disk |

### Metadata
| Method | Path                              | Purpose                          |
|--------|-----------------------------------|----------------------------------|
| GET    | `/metadata/{video_id}`            | Get current metadata draft       |
| POST   | `/metadata/{video_id}/generate`   | Generate/refresh LLM metadata    |
| PUT    | `/metadata/{video_id}`            | Edit metadata (title/desc/tags)  |

### Scheduler control
| Method | Path                              | Purpose                          |
|--------|-----------------------------------|----------------------------------|
| GET    | `/scheduler/jobs`                 | List scheduled jobs + next runs  |
| POST   | `/scheduler/run/{job_id}`         | Trigger a job immediately        |

### Other
| Method | Path                              | Purpose                          |
|--------|-----------------------------------|----------------------------------|
| GET    | `/health`                         | Liveness + pending count         |
| GET    | `/stats`                          | Counts by status + config        |
| GET    | `/quota`                          | Today's quota snapshot           |
| GET    | `/creators`                       | List discovered + seed creators  |
| POST   | `/scrape`                         | Trigger a scrape cycle           |
| POST   | `/scores/recompute`               | Refresh engagement/velocity      |

## Scoring

- `engagement_score = (likes + 2*comments + 3*shares) / max(views, 1)`, log-compressed to `[0, 1]`
- `velocity_score = engagement_score * 0.5^(hours_old / 48)` — recent viral content surfaces first
- Dashboard's Pending tab is sorted by velocity → engagement

## Quota & cadence

- `QuotaUsage` table records uploads per UTC day. `MAX_UPLOADS_PER_DAY` is a local
  safety cap (default 50), **not** YouTube's limit. YouTube's real limit is the API
  quota: 10,000 units/day per Cloud project and ~1,600 per upload, so **about 6
  uploads/day per project** — which is why each managed channel uses its own project.
- `UPLOADS_PER_DAY` is your self-imposed cap (default 8).
- The scheduler's `publish` job runs every `UPLOAD_INTERVAL_HOURS` (default 3h) and:
  1. Checks both quotas
  2. Picks the oldest transformed video
  3. Generates metadata if missing
  4. Publishes via YouTube API (or mock publisher in dev)
  5. Boosts the source creator's score for future discovery

## Free by default

Everything in the default config costs nothing:
- **yt-dlp + plain HTTP scraping** — open source, no API keys (Playwright is an optional fallback for
  Snapchat: `pip install playwright && playwright install chromium`)
- **z-ai CLI** for LLM metadata — in-house, free
- **SQLite** — local file, no server
- **APScheduler** — in-process, no Redis
- **FFmpeg** — open source binary
- **YouTube Data API** — free quota (10,000 units/day, each upload costs ~1600)

The only thing you need to provide is a YouTube OAuth client secret (free).

## Disclaimer

Respect platform Terms of Service. Only scrape and republish content you have
rights to, or that is licensed for reuse. This codebase provides plumbing
only — you are responsible for how it is used.

## YouTube token

The token lets the app upload to, and read the analytics of, one channel. It takes about 10 minutes, once.

1. Open [Google Cloud Console](https://console.cloud.google.com/) signed in as the account that owns the channel, and create a project.
2. Enable **YouTube Data API v3** and **YouTube Analytics API** under *APIs & Services > Library*.
3. In **Google Auth Platform**, go to *Branding* and enter an app name and your email. Under *Audience* choose **External**.
4. On *Audience*, press **Publish app** so its status reads **In production**. Skip this and the token dies every 7 days, which stops an unattended channel. Verification is not needed for your own channel.
5. Under *Clients*, create a client of type **Desktop app**. Download its JSON and save it as `secrets/client_secret.json`.
6. Run this, pick the channel, and tick every permission. Google warns the app is unverified: choose *Advanced*, then *Go to (your app)*.
   ```bash
   venv/bin/python scripts/youtube_oauth.py
   # On a machine with no screen, add --no-browser and follow what it prints.
   # Check an existing token offline anytime:
   venv/bin/python scripts/youtube_oauth.py --status --json
   ```
7. Add what it prints to `.env`, then check with `venv/bin/python scripts/growth.py doctor`. It refreshes the token, reads your channel name and asks the Analytics API for one row, so a dead token or an API you forgot to enable shows up now rather than after a missed post.

Lost the `.env` lines? Run the script again without `--force`: it reprints them and says what the existing token lacks. If a permission was left unticked or `doctor` reports `invalid_grant`, run it with `--force`. `invalid_grant` means the token was revoked, or expired because the app is still in Testing (step 4).

`secrets/` is gitignored. The token is saved readable only by you, and it gives upload access to your channel, so never share it. A project's quota allows roughly 6 uploads a day (see [Quota & cadence](#quota--cadence)).

## Running multiple channels

The growth loop can run several channels at once, one niche each. Every channel needs **its own Google Cloud project**: YouTube allows about 6 uploads a day per project (see [Quota & cadence](#quota--cadence)), so channels cannot share one. The channels live in `channels.json` at the repo root (`CHANNELS_FILE` moves it). With no such file the loop runs one channel from the `GROWTH_*` settings, exactly as before.

**Add a channel: two commands.**

```bash
venv/bin/python scripts/channels.py add tech --name "Tech & Science" \
    --feed https://www.youtube.com/@mkbhd --feed https://www.youtube.com/@Mrwhosetheboss
```

That prints Google's part for this channel: six steps in a browser, about ten minutes, once. **Step 4 (Publish app → In production) matters most**: a project left in *Testing* kills that channel's sign-in after 7 days, and the channel quietly stops. Save the file it asks for, then:

```bash
venv/bin/python scripts/channels.py connect tech
```

`connect` signs you in (pick THIS channel on Google's screen), asks Google whether the sign-in, both APIs and every source link really work, and only then switches the channel on. If something is wrong it says what, leaves the channel off, and can simply be run again. Over SSH it prints the sign-in link instead of trying to open a browser.

Lost track of where a channel is? Run it with nothing after it:

```bash
venv/bin/python scripts/channels.py
# tech           Tech & Science           running  3/day  2 sources
# comedy         Comedy                   off      3/day  0 sources
#                next: save its Google OAuth client file as secrets/comedy_client_secret.json
```

(`channels.example.json` is there if you would rather start from a file with four niche channels filled in.)

**Day to day.** None of these needs a restart; the loop reads its channels on every tick.

```bash
venv/bin/python scripts/channels.py feed tech --add <link> --remove <link>
venv/bin/python scripts/channels.py set tech --uploads 4 --brand @techclips   # also --name --niche --timezone --require-approval/--no-require-approval --auto-approve-score --cadence-backstop/--no-cadence-backstop
venv/bin/python scripts/channels.py disable tech          # switch one channel off completely
venv/bin/python scripts/channels.py export --out channels_backup.json         # sanitized portable snapshot (never OAuth tokens)
venv/bin/python scripts/channels.py import channels_backup.json --dry-run     # validate & preview upsert (drop --dry-run to apply)
venv/bin/python scripts/channels.py clone tech tech-clips --name "Tech Clips" # clone niche/cadence/feeds into a fresh unlinked channel
venv/bin/python scripts/growth.py status                  # quick snapshot: stock, pauses/blocks, next post (--channel <slug>, --json)
venv/bin/python scripts/growth.py report --channel tech   # totals or one channel's plan and results (--json)
venv/bin/python scripts/growth.py report --markdown --out weekly.md   # shareable Markdown performance digest (secrets never included)
venv/bin/python scripts/growth.py notify-test             # send a test alert to ntfy/Telegram and print per-channel delivery
venv/bin/python scripts/growth.py pause tech              # pause posting on one channel (supply & learning continue)
venv/bin/python scripts/growth.py resume tech             # resume posting
venv/bin/python scripts/growth.py reload                  # only after editing channels.json by hand
```

**From your phone.** On the review page each channel has a **Pause posting** button. It stops that channel posting while it keeps cutting clips and reading its numbers, and **Resume posting** starts it again. A channel paused by a takedown shows the reason and takes a second tap to resume. `scripts/growth.py resume tech` does the same from a terminal.

A change that would break the file (bad JSON, an unknown timezone, a slug used twice) is refused and the running channels are kept; `doctor` says what was wrong. Only `.env` changes still need a restart.

**What is separate per channel.** Its token, pace, timezone, plan, test results and upload count, and its takedown pause: a strike on one channel pauses that channel only. A blocked source or author is skipped by every channel.

**Your existing channel.** A channel that is not listed in `channels.json` is retired. To keep the one running from `.env` alongside the new ones, list it as `{"slug": "default"}`; `channels.py add` does this for you the first time. Files in `content/inbox/` go to that channel only.

**One creator, one channel.** A source link can feed only one channel, or both would cut the same video into the same clips. To move a creator from your existing channel to a new one, take the link out of `GROWTH_FEEDS` in `.env` and restart, then add it to the new channel. The commands and `doctor` refuse a shared link and say which channel already has it.

**The right Google account.** `youtube_oauth.py --channel <slug>` remembers which YouTube channel it connected. If a later sign-in lands on another account, that channel stops posting and stops reading its numbers until you sign in again with the right one, rather than posting to the wrong channel.

**Optional, per channel, both off by default** (so a person approves every clip): `"auto_approve_score": 0.8` approves clips an AI titled that score at least that and pass the safety check, and `"cadence_backstop": true` approves the best waiting clip rather than miss a posting window.

**New uploads.** Every `GROWTH_WATCH_MINUTES` (5) the loop checks each feed's YouTube RSS and queues a new video at once, instead of waiting for the hourly supply run. Shorts, stream recordings and premieres that have not aired are left alone. `0` turns it off.

**Hindi creators.** Install a Devanagari font (`sudo apt install -y fonts-noto-core`) so Hindi titles and credits are drawn instead of dropped. For Hindi **speech** captions, set `SARVAM_API_KEY` (Sarvam's speech-to-text) and add `"hi"` to `TARGET_LANGUAGES`; the clipper then transcribes Hindi talking clips through Sarvam. Without the key, Hindi speech clips fall back to motion-and-sound picking. Caption timing on Hindi follows Sarvam's phrase-level timestamps, so review the first few before trusting it unattended.

## Growth loop

Set `GROWTH_ENABLED=true`, `YOUTUBE_DEFAULT_PRIVACY=public` and `GROWTH_TIMEZONE`, and give it sources: video files in `content/inbox/`, or your own or permitted channels in `GROWTH_FEEDS`. Clips from inbox files need OpenCode to write their titles, or they wait for review. `ENABLE_PUBLISH` must stay on. Then `scripts/growth.py doctor`, `scripts/run.py --daemon`, and watch `scripts/growth.py report` or the dashboard's Growth tab.

`doctor` asks Google and reads every feed; add `--offline` to skip that. The dashboard and `GET /growth/doctor` stay offline, and `GET /growth/doctor?live=true` runs the full check.

The report and the Growth tab warn when the shelf is empty or analytics has failed for 3 days, show when numbers were last collected, and list problems from the last 30 days apart from routine events: videos gone missing, strikes, pauses, account holds, failed or missed posts, failed collects, retracted findings, stopped clip jobs, empty feeds, clips held for review, and supply running out, along with whatever has since cleared them. Check them weekly and nothing should have scrolled away.

A copyright takedown, a legal, policy or account rejection from YouTube, or three videos going missing in 90 days, pauses posting until you have looked at it and run `scripts/growth.py resume` (with several channels only the struck channel pauses, and `resume <slug>` clears just that one). A video that goes missing also stops that author's feed, including one you deleted yourself; `scripts/growth.py unblock <name>` lifts it.

It posts `GROWTH_UPLOADS_PER_DAY` clips in varied time windows and tests posting window, title style, format, length, source, pace, sound effects and B-roll. Half of all choices are tests for the first 30 days, then 10%.

## The web & phone control panel

`app/review.py` (`http://localhost:8600`) is the unified desktop & phone control panel, installable as a fullscreen **PWA** ("Add to Home Screen" on iOS/Android). Everything sits behind the single `?key=<REVIEW_TOKEN>` cookie login, and only port `8600` is exposed when `CLOUDFLARE_TUNNEL=true` — never port `8000`.

1. Start everything as services that survive reboots:
   - **Linux**: `scripts/install_services.sh` (also installs the 1-minute `master` fast-forward auto-deploy timer).
   - **Windows**: `powershell -ExecutionPolicy Bypass -File scripts\windows_autostart.ps1 install` (registers a silent logon + 10-minute repeating watchdog task).
   - **Android / Termux**: copy `scripts/termux_boot.sh` to `~/.termux/boot/doomscroller.sh`.
2. Locally, `python scripts/run.py --panel-url` prints your one-click `http://localhost:8600/?key=...` link. Remotely, `scripts/tunnel.py` runs a Cloudflare Quick Tunnel (falling back automatically to `localhost.run` over SSH if `cloudflared` is missing or blocked), writes the public URL atomically to `logs/tunnel_url.txt`, and exposes a **Copy phone link** button in the **Controls** tab.
3. For push alerts, install the ntfy app and subscribe to `NTFY_TOPIC` from `.env` (or configure `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`), then verify delivery with **Send test alert** in **Controls**.

Tabs & shortcuts (press `?` on desktop for keyboard help: `J`/`K` navigate, `Space` play/pause, `S` cycle `1x`/`1.5x`/`2x` speed, `A` approve, `X`/`R` reject, `E` edit title, `/` search, `U` undo, `1`–`6` switch tabs, `Esc` close modal / clear search):
- **Review** — segmented into **To review**, **Scraped** (when classic scraper candidates exist), **Up next** (the `TRANSFORMED` upload shelf, where you can edit titles, pull clips back to review, or reject before upload), and **Posted**, with instant search/filter (`/`), playback speed toggle (`S`), clip resolution & AI score badges, collapsible **Preview description & credit** editor (live YouTube description + hashtags save), one-tap **Save MP4** (`GET /api/media/{video_id}?download=1`) on Review, Up next, and Posted cards to save Shorts straight to your phone camera roll for Reels/TikTok/X, two-tap **Approve clean** (`POST /api/clips/approve-clean`) for unflagged clips alongside one-tap **Reject all N flagged** batch cleanup, timestamped YouTube source links, swipe right/left on mobile, and race-safe **Undo**.
- **Clip** — paste a YouTube/video URL or server path, or **drag-and-drop** / upload a video file straight from your browser, pick a target channel, keep human review on by default (or opt in to skip review), watch live stage progress (`downloading` → `transcribing` → `rendering`), retry/cancel/dismiss individual jobs, or **Clear finished** jobs in one tap.
- **Channels** — view every channel's live status, `.env` or per-channel creator feeds, next setup step, pause/resume toggle, and full settings (`name`, `niche`, `brand_name`, `uploads_per_day`, `timezone`, `require_approval`, `auto_approve_score`, `cadence_backstop`).
- **Status** — offline and live Google/YouTube API doctor checks per channel.
- **Report** — per-channel daily plan, stock, Bayesian A/B factor multipliers, best/worst Shorts, 30-day problems, recent events, and one-tap **Copy summary** for notes/Telegram.
- **Controls** — copy phone tunnel link, **Send test alert** (per-channel ntfy/Telegram verification), collapsible **Daemon logs** viewer (`GET /api/logs`, secret-scrubbed with `All` / `Warnings & Errors` filter + `Refresh` for phone debugging without SSH), **Export channels & queue JSON** backup (`/api/backup`, secrets redacted), inspect APScheduler health & overnight job history, preview dry-run filter/cache cleanup before running, trigger one-off jobs (`supply`, `watch`, `collect`, `learn`, `scrape`, `transform`, `filter_queue`, `reload`, `cleanup`), resume channels, lift takedown blocks, or lock the browser session.

**Wave 12 panel extras.** On **Up next**, **Publish next** (`POST /api/clips/{id}/pin`, `{"pinned": true|false}`) moves a clip to the front of its channel's queue (a repeatedly failing clip never jumps the line; **Unpin** undoes it). **Channels** gets a per-channel **Clone** (`POST /api/channels/{slug}/clone`) and an **Import channels JSON** card (`POST /api/channels/import`, preview first, never switches a channel on or carries OAuth bindings). **Controls** shows a **System health** strip from the offline preflight (`GET /api/system/doctor`; `POST /api/system/doctor/fix` creates missing folders, `.env` and the DB), booleans only, never secret values.

## Contributing & CI

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the slim-daemon architecture, hard engineering/security invariants, TDD workflow, and CLI reference. GitHub Actions CI (`.github/workflows/ci.yml`) runs `node --check` on `app.review_ui.PAGE`, `python scripts/final_audit.py --offline`, and `pytest -q -m "not slow"` across Ubuntu and Windows on every push and pull request.


