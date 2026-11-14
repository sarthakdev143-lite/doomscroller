# Contributing to Doomscroller

Thanks for contributing! Doomscroller is built to run unattended on modest hardware (~55 MB idle RAM, single SQLite file, zero Docker/Redis overhead) across Windows, Linux, and Android/Termux.

---

## 1. Quickstart (Mock Mode — Zero Credentials)

Prerequisites:
- **Python 3.10+** (`3.11` or `3.12` recommended)
- **`ffmpeg` & `ffprobe`** on `PATH` (`winget install Gyan.FFmpeg`, `apt install ffmpeg`, or `brew install ffmpeg`)
- **Node.js 20+** (optional; used for `node --check` UI validation and `yt-dlp` JS challenge solving)

```bash
# Create virtual environment & install dependencies
python -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows PowerShell:
# .\venv\Scripts\Activate.ps1

pip install -r requirements.txt

# Run preflight doctor & bootstrap .env + REVIEW_TOKEN + SQLite DB
python scripts/doctor.py --fix
python scripts/seed.py 20

# Start unified slim server in foreground (API :8000 + Control Panel :8600)
python scripts/run.py
```

On Windows, you can also use `start.bat` / `start.ps1` and `stop.bat` / `stop.ps1` directly.

---

## 2. Slim-Daemon Architecture

`scripts/serve_slim.py` hosts the entire runtime inside **one unified Python process** backed by a single WAL-mode SQLite file (`doomscroller.db`):

1. **Private Internal API & Scheduler (`app/main.py`, `127.0.0.1:8000`)** — internal FastAPI endpoints plus the in-process `APScheduler` autonomous loop (`app/scheduler.py`) and background clip worker (`app/clipper/job.py`). **Never** exposed publicly.
2. **Authenticated Web & Phone Control Panel (`app/review.py` + `app/review_ui.py`, `127.0.0.1:8600`)** — dark-mode mobile/desktop PWA gated behind `REVIEW_TOKEN` (`?key=<token>` sets an `HttpOnly` `review_key` cookie). When `CLOUDFLARE_TUNNEL=true`, `scripts/tunnel.py` exposes **only port `8600`** via Cloudflare Quick Tunnel (with `localhost.run` SSH fallback).

---

## 3. Hard Engineering & Security Invariants

Every pull request and CI run enforces these invariants. Do not break them:

1. **No `numpy` on idle server import**:
   - Importing `app.main`, `app.review`, `app.config`, `app.db`, or `app.logging_config` must **never** import `numpy` (`'numpy' not in sys.modules`). Keep `numpy` imports lazy inside functions in `app/clipper/*` and `app/growth/model.py` so idle daemon memory stays minimal.
2. **No `cv2` in `app/clipper/shots.py`**:
   - `import app.clipper.shots` must **never** load OpenCV (`'cv2' not in sys.modules`). Contact sheets and thumbnail inspection use Pillow (`PIL`).
3. **`_guard(request)` on every non-root Control Panel route & thin shell boundary**:
   - Every `APIRoute` on `app.review.review_app` except `GET /` (which renders the login screen when unauthenticated) **must** call `_guard(request)` first and return `401` without a valid session cookie.
   - `app/review.py` is a thin HTTP shell (`not hasattr(app.review, "ClipJob")`): put domain logic in `app/growth/*`, `app/clipper/job.py`, or `app/scheduler.py`.
4. **Windows `winproc` subprocess rules (`app/winproc.py`)**:
   - Every background `subprocess.run` or `subprocess.Popen` (`ffmpeg`, `ffprobe`, `yt-dlp`, `opencode`, `claude`, `z-ai`, `earshot`, `cloudflared`, `ssh`) **must** pass `stdin=subprocess.DEVNULL` (unless explicit `input=` bytes/text are piped) and `**background_subprocess_kwargs(low_priority=True)` from `app.winproc`. Without these, child processes hang on inherited `stdin` or flash `conhost.exe` console windows on Windows.
5. **Zero secret or token leaks**:
   - Never log, return, or commit `REVIEW_TOKEN`, API keys (`DEEPGRAM_API_KEY`, `SARVAM_API_KEY`, `PIXABAY_API_KEY`, `PEXELS_API_KEY`, `TELEGRAM_BOT_TOKEN`), OAuth client secrets/tokens (`secrets/`), cookies (`cookies*.txt`), `.env`, `channels.json`, or `doomscroller.db*`. Use `app.logging_config.scrub_secrets()` on diagnostic text.

---

## 4. TDD Workflow & Local Verification

Follow strict **RED → GREEN** test-driven development:

1. **RED**: Write a focused failing test in `tests/` first and run `pytest` to confirm it fails for the expected reason (`tests/conftest.py` automatically isolates tests with `DOOMSCROLLER_ENV_FILE=""`, `USE_MOCK=true`, and `sqlite:///:memory:`).
2. **GREEN**: Implement the minimal clean change and re-run targeted + regression tests.
3. **Pre-PR Gate** (mirrors `.github/workflows/ci.yml` on Ubuntu & Windows):

```bash
# Fast test suite (runs in CI on every push/PR)
pytest -q -m "not slow"

# Full test suite (includes real ffmpeg filtergraph integration renders; slow on a busy laptop,
# so run it in chunks, e.g. `pytest -q -m slow tests/test_render_integration.py tests/test_slow_integration_audit.py`)
pytest -q

# Offline preflight & repository surface audits
python scripts/doctor.py
python scripts/final_audit.py --offline
```

If you touch `app/review_ui.py`, verify inline JavaScript syntax with `node --check` (also covered by `tests/test_control_panel.py::test_review_ui_inline_javascript_has_valid_syntax`).

---

## 5. CLI Tools Overview

| Script | Purpose |
|--------|---------|
| `scripts/run.py` | Unified launcher (`--daemon`, `--ensure`, `--panel-url`, `--status`, `--stop`) |
| `scripts/doctor.py` | Offline system preflight inspector (`--fix` bootstraps dirs/`.env`/DB, `--json`) |
| `scripts/final_audit.py` | Platform routing, DB/storage summary & repo surface audit (`--offline`, `--json`) |
| `scripts/clip.py` | Cut long-form URLs or local files into 1080x1920 Shorts (`--mine`, `--queue`, `--jobs --json`, `--inbox`, `--batch FILE`, `--retry JOB_ID`, `--clear-finished`) |
| `scripts/channels.py` | Multi-channel management & portability (`add`, `connect`, `set`, `feed`, `disable`, `export`, `import`, `clone`) |
| `scripts/growth.py` | Growth loop diagnostics & controls (`status`, `doctor`, `report [--markdown] [--out FILE]`, `notify-test`, `pause`, `resume`, `unblock`, `reload`) |
| `scripts/youtube_oauth.py` | Authorize or inspect per-channel YouTube OAuth tokens (`--channel`, `--no-browser`, `--status --json`) |
