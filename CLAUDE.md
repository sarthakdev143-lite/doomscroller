# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An autonomous YouTube Shorts pipeline: scrape Snapchat Spotlight → curate (human or auto) →
transform to 1080x1920 → generate YouTube metadata → publish on a paced schedule.
Python 3.12, SQLite, no Docker/Redis required. Primary dev platform is Windows; `scripts/termux_*.sh`
target an Android/Termux deployment.

## Commands

Everything below assumes the repo venv (`venv/Scripts/python.exe` on Windows, `venv/bin/python` on POSIX).

```bash
# One-time setup
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
venv/Scripts/python.exe scripts/init_db.py

# Run everything (API :8000 + Streamlit dashboard :8501)
venv/Scripts/python.exe scripts/run.py            # foreground
venv/Scripts/python.exe scripts/run.py --daemon   # background, logs to logs/
venv/Scripts/python.exe scripts/run.py --status
venv/Scripts/python.exe scripts/run.py --stop

# Windows one-click (creates venv, installs deps, inits db, then run.py)
start.bat        # or start.ps1
stop.bat

# Tests. The fast tier is ~5s; the `slow` tier shells out to real ffmpeg (~3min).
# conftest forces USE_MOCK=true + in-memory SQLite.
venv/Scripts/python.exe -m pytest -q -m "not slow"   # 124 fast tests
venv/Scripts/python.exe -m pytest -q                 # all 138
venv/Scripts/python.exe -m pytest tests/test_selection.py::test_shape_rejects_landscape -q

# Trigger any scheduler job by id, without waiting for its interval
curl -X POST http://localhost:8000/scheduler/run/scrape

# Seed fake data (mock mode only)
venv/Scripts/python.exe scripts/seed.py 20

# YouTube OAuth (writes secrets/youtube_token.json; needs secrets/client_secret.json)
venv/Scripts/python.exe scripts/youtube_oauth.py
```

`ffmpeg` and `ffprobe` must be on PATH for real operation. Playwright/Chromium is **optional** —
only a fallback if the plain HTTP scrape is ever challenged. There is no linter or formatter configured.

## Architecture

### Two processes, one SQLite file

- **FastAPI (`app/main.py`, port 8000)** — REST API *and* host of the APScheduler `BackgroundScheduler`,
  started/stopped in the `lifespan` context manager. No separate worker process by default.
- **Streamlit (`dashboard/app.py`, port 8501)** — reads/writes the SQLite DB **directly** (imports
  `app.db`, `app.models`) for listing and approve/reject, but calls the **HTTP API** for scrape,
  transform, publish, recompute and scheduler control. Both patterns are in use; match the surrounding code.
- Celery (`tasks/celery_app.py`) is legacy/optional and not on the default path.

### Pipeline and status machine

`VideoStatus` (`app/models.py`) is the spine:

```
PENDING → APPROVED → TRANSFORMING → TRANSFORMED → PUBLISHING → PUBLISHED
   ↓ REJECTED                    ↘ FAILED       ↘ FAILED
```

Work lives in `tasks/pipeline.py` as plain synchronous functions (`scrape_task`, `transform_task`,
`publish_task`, `generate_metadata_for_video`, `long_to_shorts_task`). Scheduler jobs and API endpoints
both call these directly. API endpoints first try `*_task_async.delay(...)` and fall back to the sync
function in an `except` — with no Celery broker running, transforms/publishes execute **inline in the
request thread**, which is why the dashboard uses long httpx timeouts.

### Snapchat acquisition (`app/scrapers/snapchat_scraper.py`)

This is the part worth understanding before changing anything.

Snapchat's public pages are a Next.js app that embeds a fully hydrated payload in
`<script id="__NEXT_DATA__">`, served **on a plain HTTP GET**. One request to `/spotlight` yields 25
stories, each carrying title, description, view/boost/comment/share counts, creator handle and display
name, duration, dimensions, detected language, SEO keywords, and a direct CDN MP4 URL. The useful paths:

```
props.pageProps.spotlightFeed.spotlightStories[]
  .story.storyId.value                              stable id
  .story.snapList[0].snapUrls.mediaUrl              direct MP4
  .metadata.videoMetadata.contentUrl                direct MP4 (preferred)
  .metadata.videoMetadata.creator.personCreator     handle + display name
  .metadata.engagementStats.*                       views/boosts/comments/shares
  .metadata.llmTitle / .llmDescription              Snapchat's own SEO copy
  .metadata.cuSignals[]                             keywords + detected language
```

Two entry points:
- **Spotlight crawl** (primary, `scrape_spotlight`) — BFS. `/spotlight` seeds it; each story id then
  acts as a cursor via `/spotlight/<id>`, which returns another 25 related stories.
- **Profile crawl** (`scrape_profile`) — `/@<handle>` gives `spotlightHighlights` (ids only, no stats)
  plus `subscriberCount`. Each id is hydrated through `/spotlight/<id>`, costing one request per video.

Snapchat mixes ints, numeric strings and `None` in the same fields and wraps many scalars in
`{"value": x}` boxes — hence the `_num`/`_text`/`_unwrap` helpers. Parsing is defensive on purpose:
`_parse_story` returns `None` rather than raising on anything unexpected.

Snapchat has no "like" counter; `likes` is stored as `boostCount + recommendCount`.

### Selection happens before download

`scrape_task` gathers metadata first, filters and ranks it while it is still free, and only then
downloads the winners. The order in `select_candidates`:

1. `content_safety_reason` — blocklist over title/description/keywords/hashtags. This is what keeps
   adult-adjacent and gambling content off an automated channel.
2. `channel_focus_reason` — language allowlist (`TARGET_LANGUAGES`) plus optional topic allow/blocklists.
   Spotlight serves six-plus languages; a channel that posts all of them gives YouTube's recommender
   nothing to learn. `POST /queue/apply-filters` re-runs safety + focus over the existing PENDING backlog
   (add `?dry_run=true` to preview), since filters change after videos are already queued.
3. `_shorts_shape_reason` — duration and aspect, from platform metadata.
4. `_is_viral_candidate` — for Snapchat an absolute `VIRAL_MIN_VIEWS` floor (Spotlight is already
   algorithmically filtered, so reach beats engagement rate there); other sources use rate gates.
5. Rank by `viral_score`, cap to `max_per_run`, download.

`_is_quality_short` then runs *after* download as an on-disk check via ffprobe — that the bytes are a
real, playable, vertical video rather than an error page.

`persist_scraped_items` **refreshes** stats for videos it has seen before rather than skipping them;
view counts move fast and yesterday's clip may have gone viral since.

### Scoring (`app/analytics.py`)

Four scores, all normalised to `[0, 1]`:

| score | meaning |
|-------|---------|
| `engagement_score` | weighted reaction *rate* — `(likes + 2*comments + 3*shares) / views` |
| `reach_score` | log-compressed absolute views (10k → 0.5, 1M → 0.75, 100M → 1.0) |
| `velocity_score` | engagement decayed by age (48h half-life) |
| `viral_score` | the ranking blend: 0.50 reach + 0.30 engagement + 0.20 freshness |

Age is measured from `source_published_at` (the platform's publish time) when known, falling back to
`scraped_at`. `viral_score` is what the review queue, the pipeline tab and auto-approve all rank by.

### Transform

**Framing** (`plan_transform`) — three outcomes, and the distinction matters for quality and CPU:
- **copy** — already 9:16 at target resolution. Only taken when `TRANSFORM_VARIATION` is off; otherwise
  everything is re-rendered, because a verbatim copy is the thing we are trying to avoid.
- **scale** — 9:16 but smaller than the canvas (Snapchat serves 540x960). Upscale with lanczos, *no* blur:
  a blurred backdrop is invisible behind a foreground that already fills the frame.
- **letterbox** — not 9:16, so it gets the blurred-background fill.

**Variation** (`app/transformers/variation.py`) — YouTube's reused-content policy is the main risk to an
automated republishing channel, so each output is reframed (zoom), retimed (speed), sharpened, watermarked
and end-carded rather than re-encoded verbatim. Parameters are drawn from configured ranges and seeded from
the video id, so the treatment is *varied across videos* but *deterministic per video* — a retry renders
the same output, and a surprising result is reproducible.

Two deliberate omissions, both worth preserving:
- **No intro bumper.** On Shorts the opening moment decides retention; spending it on branding costs more
  than the distinctiveness is worth. The end card is *overlaid* over the closing seconds, never appended,
  so it adds no duration.
- **Captions default off** (`CAPTION_ENABLED=false`). Spotlight clips normally arrive with the creator's
  own burned-in captions *and* Snapchat's attribution baked into the pixels; a third text layer reads as
  clutter. `_WATERMARK_POSITIONS` and the end-card `y` are all inside the middle safe band for the same
  reason — overlays in the top/bottom bands landed on top of Snapchat's branding.

`SOURCE_CROP_TOP_PCT` / `SOURCE_CROP_BOTTOM_PCT` (default 0) trim those branding bands. They are off by
default on purpose: cropping produces a cleaner Short but strips the platform's visible attribution,
leaving only the description credit. Note the bands run ~20% deep, so a crop that fully removes them costs
a lot of frame.

**Text rendering is the sharp edge here.** All `drawtext` strings go through `textfile=` with
`expansion=none`, never inline `text=`. An inline value has to survive ffmpeg's own filtergraph parser, and
a single apostrophe in a scraped title ("Yadav's") ends the quoted argument early and corrupts the entire
graph — backslash-escaping does *not* help, because the parser closes a quoted run at the first bare quote.
`expansion=none` additionally stops `%` being read as a strftime directive. `is_renderable()` skips text
whose script the resolved font has no glyphs for, so non-Latin titles are dropped rather than drawn as
empty boxes.

Fonts resolve through `app/transformers/fonts.py`. The historical `TRANSFORM_CAPTION_FONT` default was a
Debian DejaVu path that does not exist on Windows or macOS, and a missing font file fails the whole render,
so a non-existent configured path falls back to autodetecting a bold system font.

Because filter graphs only fail at ffmpeg's parser, `tests/test_render_integration.py` renders through the
real binary (marked `slow`). Any change to the filter chain needs to run that tier.

### Metadata (`app/metadata.py`)

Sources, in priority order: **platform** (Snapchat's own `llmTitle`/`llmDescription`/keywords — already
SEO-written, in the source language, free) → **llm** (the `z-ai` CLI, only if that binary is on PATH) →
**fallback** (rule-based). Whichever wins, everything goes through `compose()`, which enforces YouTube's
limits (100-char title, 5000-char description, 15 tags / ~500 chars), builds hashtags, and appends a
creator credit with a link back. `z-ai` is usually absent — that is fine, the platform path covers
Spotlight. `GET /stats` reports `llm_metadata` so you can see which path is live.

### Pluggable backends

Each of `app/scrapers/`, `app/transformers/`, `app/publishers/` has a `base.py` interface,
implementations, and a factory in `__init__.py`. `get_scraper()` returns `MockScraper` under
`USE_MOCK`, else `SnapchatScraper` (or `YtDlpScraper` for non-Snapchat platforms).
`get_publisher()` returns `MockPublisher` under `USE_MOCK` **or** when `YOUTUBE_CLIENT_SECRETS` is empty.

### Scheduler jobs (`app/scheduler.py`)

Job ids are the contract for `POST /scheduler/run/{job_id}` and the dashboard sidebar:

| id | interval | notes |
|----|----------|-------|
| `scrape` | `SCRAPE_INTERVAL_HOURS` | loops up to 5 batches until `SCRAPE_TARGET_PENDING` pending exist |
| `discovery` | `DISCOVERY_INTERVAL_HOURS` | only registered when `AUTO_DISCOVERY=true` |
| `auto_approve` | 15 min | no-ops unless `AUTO_APPROVE=true`; honours the veto window |
| `transform` | 1 h | batches 3 APPROVED videos |
| `publish` | `24 / UPLOADS_PER_DAY` h | only registered when `ENABLE_PUBLISH=true`; no-ops unless `AUTO_PUBLISH=true` |
| `cleanup` | 24 h | deletes source files of PUBLISHED/REJECTED videos older than `SOURCE_VIDEO_MAX_AGE_DAYS` |
| `recompute` | 1 h | refreshes all four scores |
| `analytics_pull` | 6 h | pulls YouTube stats, boosts creator scores |

Every job returns a dict and swallows its own exceptions (returning `{"error": ...}`) so one failure
never kills the scheduler.

### Database

SQLite at the repo root. `init_db()` runs `create_all` and then `_sqlite_add_missing_columns()`, an
**additive auto-migration**: it diffs the models against the file, `ALTER TABLE ... ADD COLUMN`s
anything missing, and backfills the model's declared default so existing rows do not come back `NULL`
(comparing `NULL` to a float raises). Adding a field to a model is therefore safe. Dropping or retyping
a column is not handled and still needs manual work.

### Configuration

`app/config.py` exposes a single `lru_cache`d `settings` object. List-valued settings
(`TARGET_HANDLES`, `TARGET_HASHTAGS`, `ENABLED_PLATFORMS`, `YOUTUBE_DEFAULT_TAGS`) are stored as **JSON
strings** and read through `*_list` properties — add new list settings the same way.
`POST /config/publish` mutates `settings` in memory *and* rewrites `.env` with regex, then calls
`scheduler.reschedule_publish()`. That mutation is per-process: the Streamlit process keeps its own
`settings` loaded at its own startup.

## Gotchas

- **Two quota caps.** `MAX_UPLOADS_PER_DAY` is the hard cap persisted in the `QuotaUsage` table and
  checked by `publish_task`; `UPLOADS_PER_DAY` is a soft daily pace enforced by `publish_job` counting
  today's PUBLISHED rows. Publishing also applies `UPLOAD_JITTER_MINUTES` of spread.
- **Publish failures are classified.** `_is_retryable_publish_error` decides whether a failure rolls the
  video back to `TRANSFORMED` (missing OAuth token, quota, network — it will publish once fixed) or
  marks it `FAILED` (a real problem with the content). Do not collapse this back into one branch: a
  missing credential must never permanently kill a video.
- **Windows process control.** `os.killpg`/`os.getpgid` do not exist on Windows, and `os.kill(pid, 0)`
  there *terminates* rather than probes. `scripts/run.py` therefore branches on `os.name` and uses
  `tasklist`/`taskkill` on Windows. Keep that split if you touch it.
- **`app/logging_config.py` defines `configure_logging()` but nothing calls it** — log setup is whatever
  uvicorn/Streamlit provide. `log_job_execution()` *is* used and appends to `logs/job_history.jsonl`.
- **`POST /scheduler/run/chop_long` maps to `cleanup_job`** because no `chop_long_job` exists.
  `app/clips.py` is only reachable through `POST /videos/{id}/chop`.
- **`dashboard/app.py` starts with a sys.path/`sys.modules` fixup** so the `dashboard/` directory does
  not shadow the `app` package. `dashboard/_launch.py` is the cleaner entry point, but `scripts/run.py`
  launches `dashboard/app.py` directly — keep the fixup if you edit the top of that file.
- **Spotlight sources carry burned-in branding.** The CDN serves the "SpotlightSharing" asset variant,
  which has Snapchat's ghost logo and the original creator's handle rendered into the pixels, top and
  bottom, roughly 20% deep at each end. Output overlays must stay in the middle band, and no amount of
  variation hides the fact that the frame says "Snapchat". Factor this into any judgement about whether
  the channel can pass monetisation review.
- Files under `content/`, `secrets/`, `logs/` and `doomscroller.db` are gitignored runtime state.
