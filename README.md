# Doomscroller

Autonomous YouTube Shorts factory. Scrapes viral short-form videos from
TikTok, Instagram Reels, and Reddit → human approves/rejects via dashboard →
app auto-transforms to vertical Shorts format → auto-generates SEO metadata
via LLM (preserving source language) → auto-publishes to your YouTube channel
on a conservative 3-uploads-per-day schedule.

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
│  └────────────────┘  │  - publish 8h│  └──────────────────┘  │
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

## Autonomous schedule (default)

| Job          | Interval | Purpose                                                    |
|--------------|----------|------------------------------------------------------------|
| `scrape`     | 6 hours  | Pull new viral videos from seed + discovered creators     |
| `discovery`  | 24 hours | Find new creators from the authors of top viral videos    |
| `recompute`  | 1 hour   | Refresh engagement + velocity scores                       |
| `publish`    | 8 hours  | Auto-publish next transformed video (3/day, quota-aware)  |
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
on the 8-hour cadence.

For background daemon mode (logs to `logs/`):
```bash
python scripts/run.py --daemon
python scripts/run.py --status
python scripts/run.py --stop
```

## Going to production (real scraping + real YouTube)

1. **Install ffmpeg**: `apt install ffmpeg` (or `brew install ffmpeg`)
2. **Edit `.env`**:
   ```env
   USE_MOCK=false
   USE_LLM_METADATA=true                 # uses free z-ai CLI
   TARGET_HANDLES=["@creator1","@creator2"]  # seed TikTok handles
   TARGET_HASHTAGS=["fyp","viral"]
   YOUTUBE_CLIENT_SECRETS=/abs/path/client_secret.json
   YOUTUBE_DEFAULT_PRIVACY=public        # or private until you trust it
   AUTO_PUBLISH=true                     # autonomous mode
   UPLOADS_PER_DAY=3
   UPLOAD_INTERVAL_HOURS=8
   ```
3. **Get YouTube OAuth credentials**:
   - Google Cloud Console → New Project
   - Enable **YouTube Data API v3**
   - OAuth consent screen → External → add your Google account as test user
   - Credentials → Create OAuth client ID → Desktop app
   - Download JSON, save path in `YOUTUBE_CLIENT_SECRETS`
4. **Run it**:
   ```bash
   python scripts/run.py --daemon
   ```
   First publish will pop a browser for OAuth consent; token is cached at
   `YOUTUBE_TOKEN_PATH` for subsequent runs.

That's it. The app will now:
- Scrape TikTok/Reddit/Instagram every 6h
- Discover new creators daily
- Show you pending videos in the dashboard
- When you approve, transform + generate metadata automatically
- Publish 3 videos per day, 8 hours apart, to your channel
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
│   ├── scrapers/           # yt-dlp + mock scrapers
│   ├── transformers/       # FFmpeg vertical Shorts transformer
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
- `UPLOADS_PER_DAY` is your self-imposed cap (default 3, conservative for new channels).
- The scheduler's `publish` job runs every `UPLOAD_INTERVAL_HOURS` (default 8h) and:
  1. Checks both quotas
  2. Picks the oldest transformed video
  3. Generates metadata if missing
  4. Publishes via YouTube API (or mock publisher in dev)
  5. Boosts the source creator's score for future discovery

## Free by default

Everything in the default config costs nothing:
- **yt-dlp** — open source, no API keys
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
