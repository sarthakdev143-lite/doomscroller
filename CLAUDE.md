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
venv/Scripts/python.exe -m pytest -q -m "not slow"   # fast tier
venv/Scripts/python.exe -m pytest -q                 # everything
venv/Scripts/python.exe -m pytest tests/test_selection.py::test_shape_rejects_landscape -q

# Trigger any scheduler job by id, without waiting for its interval
curl -X POST http://localhost:8000/scheduler/run/scrape

# Seed fake data (mock mode only)
venv/Scripts/python.exe scripts/seed.py 20

# Cut one long video or link into finished Shorts
venv/Scripts/python.exe scripts/clip.py "https://www.youtube.com/watch?v=..." --count 3

# YouTube OAuth (writes secrets/youtube_token.json; needs secrets/client_secret.json; steps in README.md)
venv/Scripts/python.exe scripts/youtube_oauth.py

# Growth loop: can it run, and what has it learned?
venv/Scripts/python.exe scripts/growth.py doctor   # live: asks Google and reads feeds; --offline skips that
venv/Scripts/python.exe scripts/growth.py report
venv/Scripts/python.exe scripts/growth.py resume   # post again after a takedown pause
venv/Scripts/python.exe scripts/growth.py unblock  # list takedown blocks; with a name, lift one
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

**Source branding and the crop** (`app/transformers/geometry.py`) — every Spotlight clip arrives with
Snapchat's ghost logo, the word "Snapchat", and the original creator's handle burned into the pixels at the
top-left. It is universal, not occasional. Measured across the downloaded library, a **22% top crop** clears
it; 12–19% leaves legible remnants.

Cropping changes the aspect ratio, so it has to be planned *with* the framing step, not before it — the
original bug was that `plan_transform` decided framing from pre-crop dimensions, so a cropped 3:4 frame took
the no-blur path and came out letterboxed. `plan_crop()` now returns the whole chain plus the resulting
dimensions, and the transformer derives `needs_blur` from those.

| setting | effect |
|---------|--------|
| `SOURCE_CROP_TOP_PCT` / `_BOTTOM_PCT` | band to remove, % of original height (22 / 0) |
| `SOURCE_CROP_MODE` | `fill` centre-crops the width back to 9:16 (full bleed); `fit` keeps everything and lets the blur filler pad — **`fit` looks bad**, the filler surfaces inverted ghosts of the cropped band |
| `SOURCE_CROP_KEEP` | which half of the width survives `fill`. That width is discarded either way, and the branding hugs the **left** edge, so `right` removes the remnant for free |

Cost at 22% fill: canvas upscale rises from 2.00x to 2.57x. In practice output looks *better*, because the
subject stops sitting small inside dead space.

**The crop removes the creator's visible credit**, so `settings.source_crop_active` forces
`metadata.compose(require_credit=True)`, which emits an attribution block even when the handle is unknown.
Keep those two coupled — that text credit is the only attribution left once the pixels are gone. None of
this makes the content original for monetisation purposes.

Three watermark *auto-detectors* were tried and all failed; do not spend time re-deriving them:
per-row temporal activity (too coarse — a corner logo cannot move a row average), per-cell temporal
variance (the overlay is semi-transparent, so its pixels change with the content behind it), and
per-pixel brightness frequency across clips (never reliably above the bright threshold, same reason).
The position is consistent enough that a fixed percentage beats detection.

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

### Clipper (`app/clipper/`)

A separate path from the scrape pipeline: one long video or link goes in, finished 1080x1920 clips come
out. `job.clip_source` runs `ingest` -> `transcribe` -> `signals` -> `moments` -> `render` and saves `Video`
rows, `PENDING` or, with `CLIP_AUTO_QUEUE`, straight to `TRANSFORMED`. `POST /clip` and the `clip_queue` job
drive it through the `ClipJob` table, so queued work survives a restart.

- **Speech vs no speech.** `Transcript.is_english_speech` decides. Speech goes to the AI as numbered sentences
  and comes back as sentence numbers, so a clip cannot start or end mid-sentence. Everything else, including
  non-English speech for now, is ranked on measured motion, loudness and cuts, and the AI re-ranks frame
  strips of the best candidates. Without AI both paths fall back to rules; without a Deepgram key every video
  takes the no-speech path.
- **Claude is the fallback** (`ai.py`). When OpenCode fails or times out, the call goes to the `claude` CLI and
  OpenCode is skipped for 30 minutes, since one timeout costs four minutes. Claude runs with `--tools ""`,
  `--strict-mcp-config`, `--safe-mode`, no session and its own system prompt, from an empty directory, and images travel
  inside a `stream-json` message, so it never needs a tool to read a file. A clip no AI titled waits for review, so
  `doctor` requires one of the two. `CLAUDE_FALLBACK=false` turns it off; tests set `CLAUDE_BIN` to nothing.
- **Model.** The owner wants `opencode-go/muse-spark-1.3-contributor` for every OpenCode call, text and images alike.
- **OpenCode is locked down on purpose** (`ai.py`). It is an agent with shell access and the user's MCP
  servers, and transcripts are untrusted text. Every call uses `--standalone` (the background service would
  ignore `OPENCODE_CONFIG_CONTENT`), all tools denied, an empty working directory, and stdin supplied or
  closed. With an inherited stdin the CLI hangs forever.
- **Time.** Pauses are cut from speech clips, so `timeline.py` maps source time to clip time for captions,
  crops and zooms. Boundaries sit on a 30fps grid so audio and video cut together.
- **The edit** (`edit.py`). Raw footage with captions reads as a clip of an interview, so each speech moment gets
  a second AI pass that drops weak sentences and places graphics on exact phrases. Stutters and filler are cut
  by rule. `render.py` then changes the framing every 1-2.4s, and `sfx.py` synthesises effects, so there is
  nothing to license. `faces.py` picks the face that moves, and never averages two people: that lands on the gap.
  Most emphasis is a `punch`: a flash, a shake and a thud, with no text. Big words over dimmed footage were tried and
  the owner rejected them, so only real numbers (`stat`) and one turning point per clip (`card`) get on-screen text.
- **B-roll** (`broll.py`). The AI names filmable search terms, and Pixabay or Pexels supplies clips, cached under
  `content/broll/` and credited in the description. It is optional at every step: no key, no results or a failed
  download just means fewer cutaways. A 429 stops further searches. Pixabay requires searches cached for 24h, and
  its key travels in the URL, so the httpx logger stays at WARNING and errors must never include the URL.
  Searches match tags rather than meaning, so `shots.py` shows the vision model the results, cropped as they will
  render, with the words spoken over them. If it rejects them all it can suggest one better search; if that fails
  too the cutaway is dropped. If it cannot judge at all, the job stops asking and uses no stock: unjudged matches put a sapphire ring and Alcatraz into a 1940 film.
  Files are test-decoded before use, because a CDN can answer 200 with an error page, and a render that still
  fails is retried once without B-roll.
- **Tidying speech is conservative.** A rule that removes words changes what someone said, so `droppable_indices`
  errs towards keeping them: "money. Money", "50 50" and "As you know, ..." all stay.
- **Audio order.** The voice is levelled first and effects mixed on top. Mixing first lets the effects drown a quiet
  recording.
- **Graphics.** ASS captions via libass rather than `drawtext`: the text lives outside the filter graph, so
  an apostrophe cannot break it. Run ffmpeg from a temp dir with relative names, for the same reason.
- **Layout.** `CLIP_LAYOUT=framed` (the default, chosen by the owner after watching cropped clips) centres a wide
  source at full width over a zoomed, dimmed copy of itself, because a crop kept losing the subject. Zooms are halved,
  the crop's face tracking is skipped (speech clips use speaker focus instead), and cutaways replace only the picture, so the shape holds. Tall sources still fill the
  canvas. `captions.framed_layout` keeps text in the bands and above the Shorts buttons, and moves captions onto the
  picture when a 4:3 or square source leaves no room below it.
- **Visual picks leave out the closing 15% of a long source.** Scrolling credits score as busy motion, and without AI
  nothing else rejects them. Any unattended clip no AI has titled stays `PENDING`: rule-based titles are a raw first
  sentence or "<source> - part N".
- **Speaker focus.** In the framed layout a speech clip's picture zooms to 1.35x on the face that moves most
  (`speaker_pieces`), changing only at the edit's own beats or a cut in the footage, so a switch reads as an edit.
- **Downloads.** YouTube serves formats only after a JavaScript challenge, so `network_options` gives yt-dlp any
  deno/node/bun on PATH and its official solver (`remote_components: ejs:github`). Without one only storyboard images
  come back ("Requested format is not available"). The original audio is preferred over dubs, and `COOKIES_FILE` takes
  a cookies.txt when the host gets YouTube's bot check.
- **Sources with their own captions** (`subtitles.py`). Big channels burn captions in, and ours on top showed every
  word twice. One vision call per speech source looks at eight frames (some channels caption only stretches) and, if
  any show speech captions, the clips keep the hook, graphics and credit but drop our word captions.
- **Sound.** Effects are on for every clip (no longer an A/B arm: the owner wants them). Music comes only from
  `content/music/` (`CLIP_MUSIC_DIR`), tracks the owner chose, because music is what draws claims. One is picked per
  clip from its id, looped, set `CLIP_MUSIC_DB` below the levelled voice and ducked by it with `sidechaincompress`. A
  `<track>.txt` beside a track is its credit line, added to the description. An empty folder means no music.
- **On-screen text.** `Credit: <name>` runs the whole clip whenever the source names a creator, and `BRAND_NAME`, when
  set, does the same for the channel.
- Changes to the filter graph need the `slow` tier in `tests/test_clipper_render.py`.

### Growth loop (`app/growth/`)

Off unless `GROWTH_ENABLED=true`. When on it owns publishing (`publish_job` no-ops), and `scrape` and
`analytics_pull` are not scheduled. It posts finished clips only (`meta.clip_rendered`), never scraped reposts.

| job | every | does |
|-----|-------|------|
| `growth_supply` | 1h | below `GROWTH_MIN_INVENTORY`, queues a waiting winner's source, else the next unseen inbox file or feed link |
| `growth_autopilot` | 10m | writes the day's plan, then posts whatever is due |
| `growth_collect` | 3h | `VideoSnapshot` per video from the Data API, retention from the Analytics API |
| `growth_learn` | 6h | diagnoses matured videos, updates the focus, mines winners, logs findings |

- **A/B testing here means between videos.** Each video changes several factors (`factors.py`) at once, so
  `model.py` fits them together: Bayesian linear regression on log views, with a trend term so a growing channel is
  not credited to whatever ran last, and 45-day recency weighting. Thompson sampling then chooses. Do not replace it
  with per-arm averages, which confound the factors.
- **Arms are read from the row, not the plan** (`published_arms`): the slot comes from `published_at`, and the title
  style and recipe record what the video really got. A plan is only an intention.
- **Fair comparison.** A video is judged on views interpolated at `GROWTH_HORIZON_HOURS`, because older videos
  have simply had longer. Only `yt_privacy == public` counts: a private upload says nothing about an audience.
- **Diagnosis is against the channel's recent median**: the 30 matured videos before this one, topped up with later
  ones only until the first few reach the 8 it needs. Until then a video stays undiagnosed, and it stops being eligible
  14 days past the horizon. Retention is compared within a length band. `weak_hold` focuses testing on
  the edit, `no_reach`/`low_views` on timing, title and source, and `winner` queues more clips from that source. Fixes
  apply to later videos: published ones are never edited, because that would corrupt their own measurement. A winner
  is mined only while stock plus clips still being cut is below `GROWTH_MIN_INVENTORY`, at most twice per source. One
  judged on a full shelf stays owed: `supply_job` takes it ahead of a new source once stock falls, until 14 days past
  the horizon.
- **Two measures per arm.** The reward is a blend (log views, retention within its length band, subscribers per 1000
  views) and drives choices. The summary's `multiplier`, `low`, `high` and `p_best` come from a separate views-only fit,
  so anything labelled views means views, and the blend sits under `score`. A finding needs P(best) >= 0.95 for views
  with n >= 8, held unbroken across 30 new matured videos, and is retracted below 0.7. It counts videos rather than runs
  because runs hours apart re-read nearly the same data, and a fixed seed gives identical data an identical answer.
- **Recipes** (`recipes.py`) override `settings.clip_*` under a lock for one render. Clipper code must read those
  settings at call time, and a recipe may never switch on what the owner switched off.
- **The token's `scopes` field holds what was granted**, because `youtube_oauth.py` rewrites it: Google's library
  otherwise stores what was requested, which hides an unticked box from `doctor`. The publisher loads scopes from
  the file, so a token from before analytics was added can still upload.
- **Failing to read analytics never stops the loop.** Views alone still drive it; `retention` just stays `None`. That
  also reads exactly like YouTube's two-day reporting delay ("no watch data yet"), so `collect.health` tracks
  `analytics_failing_since` in `GrowthState`. A failure logs `analytics_failed` once a day, and from 3 days on the job returns
  `error` too, so `instrumented` marks it failing and the report warns. Nothing gates on that `error`.
- **`doctor()` is offline by default because the dashboard runs it on every refresh.** `doctor(live=True)` (the CLI's
  default, `GET /growth/doctor?live=true`) adds a forced token refresh, `channels.list`, a one-row Analytics query and a
  first link from each feed. The refresh is forced because an access token with time left hides a dead refresh token for
  up to an hour. Any failed call is a required failure, including Analytics, even though the loop can run without it;
  `explain_api_error` supplies the cause and next step. A feed whose videos all sit under a takedown block is only a
  warning, because the block is working, but it stops counting towards `content source`.
- **Problems stay apart from routine events.** `report.py`'s `_PROBLEM_KINDS` are shown for 30 days, while routine events
  arrive about seven a day and would push a once-only `strike` off the page within days. A new failure kind belongs on
  that list. What ended a problem (`strike_cleared`, `resumed`, `unblocked`) is listed beside it, or last week's strike
  reads as still open. A feed that lists videos again deletes its `feed_empty`, or doctor keeps failing it. The CLI caps rows per kind, so a long run of `missed` cannot hide a takedown. Anything that repeats every run
  goes through `note_once`.
- **An empty shelf is not an error to the scheduler.** `{"skipped": ...}` and `{"starved": ...}` record as ok, which is
  right for a job and useless to an owner, so `missed` (with `last_error`, the last reason the post could not go) and a
  daily `starved` event carry it, and the report warns at zero stock. The warning gives the step that fits: approve held
  clips, wait for clip jobs, fix what supply recorded in `supply_gap` (an unreadable feed, clip jobs failing), and only
  then add sources. Off when `GROWTH_ENABLED` is off, since the normal pipeline uses no clip stock.
- **Takedowns stop the loop** (`safety.py`). Nobody watches an unattended channel, and YouTube can end one at three
  copyright strikes in 90 days, while a wrong pause costs one `resume`. So it stops rather than skipping the clip, and
  only a person restarts it. `record_strike` quarantines the unposted clips of that `clip_parent`, and
  supply drops that source and that author's feed links. One hard strike (`copyright`, `claim`, `termsOfUse`, `legal`,
  `inappropriate`, `trademark`, or a suspended or closed account) or three soft ones in 90 days (`missing`, or a
  technical rejection such as `duplicate` or `length`) sets `paused`, and only `scripts/growth.py resume` clears it.
  Strikes keep counting after a resume, so the next problem inside the window pauses again. Collect keeps asking about
  videos for the whole 90 days, though snapshots stop at 30, and only judges ids it really sent. Each struck video holds
  one block: a `missing` video that comes back takes its strike and block with it, and otherwise
  `scripts/growth.py unblock <name>` lifts it. Doctor, report and a starved supply all name what is blocked.
- **An account failure is a hold, not a strike.** `account_level` from `publish_task` stops posting for an hour with one
  event, and clears itself on the next good upload, because the next clip would only fail the same way. A missing or
  revoked token (`TokenProblem`) is one too, and `check_ready` finds it before anything is spent on a title. Other retryable
  failures retry each tick without counting an attempt, and `_LATE_GRACE` ends the window. An exception in the publish
  step counts against the post and the clip, so `least_failed` moves on to the next clip.
- **Last gate before an upload.** Auto-queued clips never sit in `PENDING`, so `_save_clip` flags (`meta.growth_flagged`,
  left `PENDING`) and `_publish_next` rejects (`meta.auto_rejected`) on `clip_gate_reason`. Both judge the same text, the
  source's words included (`meta.clip_source_text`), or a clip could pass one and fail the other. A clip someone approved
  (`curated_at`) skips the upload gate, since a re-run never undoes a decision. `ALLOW_UNKNOWN_LANGUAGE` does not apply to
  rendered clips: no language means no speech to judge, and every clip has none without a Deepgram key.
- **A post cannot go out twice.** The upload stamps `meta.growth_post` before it starts, and a tick fills any waiting
  post that a published row already names, so a plan save lost after an upload is repaired rather than repeated.
- **A rehearsal leaves no trace.** `MOCK_` rows are left out of `tried`, and out of the gap and the last source once
  uploads are real, and the clipper can cut them again. `started` is set by the loop's first real post only, not by a
  clip published by hand before it; until then the policy is in its testing phase.

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
| `clip_queue` | 5 min | cuts queued long videos into clips, one at a time |
| `cleanup` | 24 h | deletes source files of PUBLISHED/REJECTED videos older than `SOURCE_VIDEO_MAX_AGE_DAYS` |
| `recompute` | 1 h | refreshes all four scores |
| `analytics_pull` | 6 h | pulls YouTube stats, boosts creator scores |
| `growth_*` | see Growth loop | only registered when `GROWTH_ENABLED=true` |

Every job returns a dict and swallows its own exceptions (returning `{"error": ...}`) so one failure
never kills the scheduler.

### Observability and durability

| endpoint | purpose |
|----------|---------|
| `GET /scheduler/history` | recent job runs (timing, result, ok) plus per-job run/failure counts |
| `GET /scheduler/overdue` | jobs whose last success is older than their interval |
| `GET /storage` | disk used under `content/`, including orphaned files |
| `POST /storage/cleanup` | reclaim now instead of waiting for the daily job |

**Sleep drops work.** APScheduler discards a fire it missed by more than `misfire_grace_time` rather than
running it late, so a suspended laptop silently skips jobs — observed missing cleanup by 10h49m. The grace
is now 6h, and `schedule_catchup()` runs at startup: it reads the last successful run per job from the
history file and queues anything overdue (staggered, and only if less than 48h late, beyond which the work
is stale).

**Cleanup reclaims three things**, only the first of which the original handled: source files of finished
videos past `SOURCE_VIDEO_MAX_AGE_DAYS`, transformed outputs of published videos (never deleted before, and
the larger files), and **orphans** — files on disk no row references, which a row-driven cleanup can never
see. Orphans need a 6h grace so an in-flight download is never deleted, and files whose name starts with
`_` are internal scaffolding (the mock master clip) and exempt. `cleanup_job(dry_run=True)` reports without
deleting.

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
- **Publish failures are classified.** `classify_publish_error` takes the exception. Only an HTTP error with a reason on
  the `_VIDEO_REASONS` allowlist marks a video `FAILED`; everything else (network, credentials, quota, a reason nobody
  listed) rolls it back to `TRANSFORMED`, and 401/403 and account reasons also return `account_level=True`. Do not
  collapse this back into one branch: a missing credential must never permanently kill a video. Once `upload()` has
  returned, the video is live, so nothing after it reaches the classifier; quota and `boost_creator` cannot change its status.
- **Publishing is claimed.** `publish_task` moves a row to `PUBLISHING` with `UPDATE ... WHERE status='transformed'` and
  stamps `meta.publish_claimed_at` and `meta.publish_title`. The `UPDATE` is what decides, not the status read before it:
  writing a title can take minutes, and the dashboard is another process, so a reject or a manual publish can land in that
  gap, and acting on the stale read would upload a rejected clip or post one twice. A crash leaves the row there, and YouTube may or may not
  have the file, so it is never just re-uploaded: `recover_stuck_publishes` (at startup and every autopilot tick) searches
  the channel's uploads for that title once the claim is 2h old. Found means `PUBLISHED`, not found means `TRANSFORMED`,
  and a failed lookup leaves the row and tries again.
- **Only `scripts/youtube_oauth.py` opens a browser.** The publisher runs on the scheduler thread, where a login would
  hang forever, so a bad token raises a retryable error instead.
- **Windows process control.** `os.killpg`/`os.getpgid` do not exist on Windows, and `os.kill(pid, 0)`
  there *terminates* rather than probes. `scripts/run.py` therefore branches on `os.name` and uses
  `tasklist`/`taskkill` on Windows. Keep that split if you touch it.
- **Every scheduled job must go through `scheduler.instrumented()`.** It times the run, logs it, and
  appends a line to `logs/job_history.jsonl`. The previous code had a `_run_job` wrapper that
  `_make_scheduler` never used — it registered the raw functions — so no history was ever written and a
  full day of operation produced 57 log lines. `test_every_registered_job_is_wrapped_when_scheduled`
  guards against that regressing. `configure_logging()` likewise must be called once per process (it is,
  from the FastAPI lifespan and the dashboard's cached init); without it the root logger sits at WARNING
  and all app-level INFO is silently dropped.
- **`dashboard/app.py` starts with a sys.path/`sys.modules` fixup** so the `dashboard/` directory does
  not shadow the `app` package. `dashboard/_launch.py` is the cleaner entry point, but `scripts/run.py`
  launches `dashboard/app.py` directly — keep the fixup if you edit the top of that file.
- **Spotlight sources carry burned-in branding.** The CDN serves the "SpotlightSharing" asset variant,
  with Snapchat's ghost logo and the original creator's handle rendered into the pixels at the top-left.
  The 22% crop removes it, but overlay positions (`_WATERMARK_POSITIONS`, the end-card `y`) still assume
  the middle safe band, because the crop is a setting that can be turned off. Removing a watermark does
  not make content original — factor that into any judgement about monetisation review.
- Files under `content/`, `secrets/`, `logs/` and `doomscroller.db` are gitignored runtime state.
