# Doomscroller

Autonomous YouTube Shorts factory. Scrapes viral short-form videos from
Snapchat Spotlight and creator profiles → human approves/rejects via dashboard →
app auto-transforms to vertical Shorts format (or chops long-form into clips) →
auto-generates SEO metadata via LLM (preserving source language) → auto-publishes
to your YouTube channel on a paced 8-uploads-per-day schedule.

**The user's only job: approve or reject videos. Everything else is automated.**

## How it runs

```
┌──────────────────────────────────────────────────────────────┐
│                  FastAPI process (port 8000)                  │
│  ┌────────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  REST endpoints│  │  Scheduler   │  │  Pipeline funcs  │  │
│  │  /videos/*     │  │  APScheduler │  │  scrape/transform│  │
│  │  /metadata/*   │  │  - scrape 6h │  │  /publish        │  │
│  │  /scheduler/*  │  │  - discover  │  │  + LLM metadata  │  │
│  │  /creators     │  │    24h       │  │  via z-ai CLI    │  │
│  └────────────────┘  │  - publish 3h│  └──────────────────┘  │
│                      │  - cleanup   │                         │
│                      │    24h       │                         │
│                      └──────────────┘                         │
└──────────────────────────────────────────────────────────────┘
                              ▲
                              │ HTTP
                              ▼
                 ┌────────────────────────┐
                 │ Streamlit dashboard    │
                 │ http://localhost:8501  │
                 │ Approve/reject, edit   │
                 │ metadata, watch quota  │
                 └────────────────────────┘
```

No Docker. No Redis. No Celery (optional). One command starts everything.

## Clip a long video

Paste a link into the dashboard's ✂️ Clip tab, or run:

```bash
venv/bin/python scripts/clip.py "https://www.youtube.com/watch?v=..."
```

English speech gets AI-picked moments, tightened pauses and word-by-word captions.
Video with no speech is ranked on motion, sound and scene cuts, and the AI judges
frames from the best candidates. Needs `ffmpeg` and, for captions, `DEEPGRAM_API_KEY`.
`PIXABAY_API_KEY` adds stock B-roll.
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

```bash
cd doomscroller
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # USE_MOCK=true, AUTO_PUBLISH=true by default

# Initialize DB + seed some fake viral videos
python scripts/init_db.py
python scripts/seed.py 20

# Start everything (API + scheduler + dashboard) in foreground
python scripts/run.py
```

Open http://localhost:8501 — you'll see ~20 pending mock videos. Approve a
few → the scheduler will transform them, generate LLM metadata, and publish
on the 3-hour cadence.

For background daemon mode (logs to `logs/`):
```bash
python scripts/run.py --daemon
python scripts/run.py --status
python scripts/run.py --stop
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
│   ├── scrapers/           # Snapchat (Playwright + CDN) + yt-dlp + mock scrapers
│   ├── transformers/       # FFmpeg vertical Shorts transformer
│   ├── clipper/            # Long video or link → finished Shorts
│   └── publishers/         # YouTube OAuth2 + mock publishers
├── tasks/
│   └── pipeline.py         # scrape/transform/publish/metadata functions
├── dashboard/
│   └── app.py              # Streamlit UI
├── scripts/
│   ├── init_db.py
│   ├── seed.py
│   └── run.py              # one-command launcher (foreground or daemon)
├── tests/                  # pytest
├── content/                # downloaded source videos (gitignore this)
├── secrets/                # OAuth token cache (gitignore this)
├── logs/                   # daemon logs (gitignore this)
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

- `QuotaUsage` table records uploads per UTC day. `MAX_UPLOADS_PER_DAY` is the
  hard cap (default 50, YouTube's daily quota).
- `UPLOADS_PER_DAY` is your self-imposed cap (default 8).
- The scheduler's `publish` job runs every `UPLOAD_INTERVAL_HOURS` (default 3h) and:
  1. Checks both quotas
  2. Picks the oldest transformed video
  3. Generates metadata if missing
  4. Publishes via YouTube API (or mock publisher in dev)
  5. Boosts the source creator's score for future discovery

## Free by default

Everything in the default config costs nothing:
- **Playwright + yt-dlp** — open source, no API keys
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
   ```
7. Add what it prints to `.env`, then check with `venv/bin/python scripts/growth.py doctor`. It refreshes the token, reads your channel name and asks the Analytics API for one row, so a dead token or an API you forgot to enable shows up now rather than after a missed post.

Lost the `.env` lines? Run the script again without `--force`: it reprints them and says what the existing token lacks. If a permission was left unticked or `doctor` reports `invalid_grant`, run it with `--force`. `invalid_grant` means the token was revoked, or expired because the app is still in Testing (step 4).

`secrets/` is gitignored. The token is saved readable only by you, and it gives upload access to your channel, so never share it. Each project gets 100 uploads a day by default.

## Growth loop

Set `GROWTH_ENABLED=true`, `YOUTUBE_DEFAULT_PRIVACY=public` and `GROWTH_TIMEZONE`, and give it sources: video files in `content/inbox/`, or your own or permitted channels in `GROWTH_FEEDS`. Clips from inbox files need OpenCode to write their titles, or they wait for review. `ENABLE_PUBLISH` must stay on. Then `scripts/growth.py doctor`, `scripts/run.py --daemon`, and watch `scripts/growth.py report` or the dashboard's Growth tab.

`doctor` asks Google and reads every feed; add `--offline` to skip that. The dashboard and `GET /growth/doctor` stay offline, and `GET /growth/doctor?live=true` runs the full check.

The report and the Growth tab warn when the shelf is empty or analytics has failed for 3 days, show when numbers were last collected, and list problems from the last 30 days apart from routine events: videos gone missing, strikes, pauses, account holds, failed or missed posts, failed collects, retracted findings, stopped clip jobs, empty feeds, clips held for review, and supply running out, along with whatever has since cleared them. Check them weekly and nothing should have scrolled away.

A copyright takedown, a legal, policy or account rejection from YouTube, or three videos going missing in 90 days, pauses posting until you have looked at it and run `scripts/growth.py resume`. A video that goes missing also stops that author's feed, including one you deleted yourself; `scripts/growth.py unblock <name>` lifts it.

It posts `GROWTH_UPLOADS_PER_DAY` clips in varied time windows and tests posting window, title style, format, length, source, pace, sound effects and B-roll. Half of all choices are tests for the first 30 days, then 10%.

## Approving clips from your phone

While `GROWTH_REQUIRE_APPROVAL=true`, every clip waits on the review page until you approve it.

1. Start everything as services that survive reboots: `scripts/install_services.sh`
2. The phone address is in `logs/tunnel_url.txt`. Open it once as `<address>/?key=<REVIEW_TOKEN from .env>`.
3. For alerts, install the ntfy app and subscribe to the `NTFY_TOPIC` from `.env`. New clips, posts, problems and
   any new page address arrive there.

