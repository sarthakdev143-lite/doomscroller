# Changelog

All notable changes to Doomscroller are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Unified slim runtime (`scripts/serve_slim.py`)** hosting the private FastAPI API (`:8000`), APScheduler loop, and authenticated Control Panel PWA (`:8600`) in one ~55 MB idle process (`app/winproc.py` priority & subprocess guards).
- **Preflight & portability tooling**: `scripts/doctor.py` (`--fix`, `--json`), `scripts/final_audit.py` (`--offline`, `--json`), `scripts/windows_autostart.ps1`, `stop.ps1`, and `scripts/channels.py export|import|clone`.
- **Control Panel power-ups (`app/review.py`, `app/review_ui.py`)**: clip queue pinning (`/api/clips/{video_id}/pin`), two-tap `approve-clean` & flagged batch rejection, inline description/tags editing, `Save MP4` (`?download=1`), secret-scrubbed `/api/logs`, channel clone/import, and system doctor strip.
- **Batch & inbox clipping (`scripts/clip.py`)**: `--inbox`, `--batch FILE`, `--enqueue`, `--retry`, `--clear-finished`, and `--jobs --json`.
- **Trending meme overlays & SFX pack overrides**: `scripts/fetch_memes.py` (`TENOR_API_KEY` / `GIPHY_API_KEY`, `CLIP_MEMES`) for punch-beat corner overlays plus `content/sfx/` (`CLIP_SFX_PACK_DIR`) custom one-shot cues.
- **GitHub Actions CI & contributor docs**: Ubuntu + Windows matrix (`.github/workflows/ci.yml`), `CONTRIBUTING.md`, `SECURITY.md`, and issue/PR templates.

### Removed
- Legacy Streamlit `dashboard/`, Celery/Redis task broker plumbing (`tasks/celery_app.py`), `scripts/launch_uvicorn.py`, `moviepy`, and `loguru` dependencies; Playwright is now strictly optional.

## [2026-11-10] — Hindi Speech, Audio QA & Mobile Control Panel

### Added
- **Hindi speech captioning (`app/clipper/sarvam.py`)**: Sarvam STT chunked transcription when `SARVAM_API_KEY` is set and `"hi"` is in `TARGET_LANGUAGES`, paired with script-aware Devanagari font resolution (`app/transformers/fonts.py`). (#15)
- **Optional Earshot audio QA (`app/clipper/audio_qa.py`)**: per-clip `earshot critique --json` scorecard (`EARSHOT_QA`) displayed on the review card without blocking renders. (#14)
- **Fast-forward Linux auto-deploy (`scripts/autodeploy.sh`, `scripts/install_services.sh`)**: 1-minute `systemd --user` timer pulling `master` cleanly and restarting `doomscroller.service`. (#13)
- **Six-tab Web & Phone Control Panel PWA**: Review, Clip, Channels, Status, Report, and Controls tabs with touch swipe gestures, keyboard shortcuts, Undo, and `no-store` cache headers. (#11, #12)

### Fixed
- **True stereo clipper audio mix**: preserve stereo width on music beds while keeping speech centred dual-mono (`-ac 2`). (#10)

## [2026-11-09] — Multi-Channel Growth Fleet & Long-Form Clipper

### Added
- **Multi-channel growth fleet (`app/growth/`, `scripts/channels.py`, `scripts/growth.py`)**: per-channel OAuth project/token isolation, `channels.json` hot-reload & validation, Bayesian linear regression A/B factor learning, 5-minute YouTube RSS watch sensor, ntfy/Telegram digests, and per-channel takedown pause/quarantine safety. (#7, #9)
- **Long-to-Shorts AI clipper (`app/clipper/`)**: Deepgram speech + motion/loudness visual moment selection, tightened pauses, word-by-word ASS captions, `framed` & `fill` layouts, vision-judged stock B-roll, and burned-in subtitle detection. (#6, #8)
