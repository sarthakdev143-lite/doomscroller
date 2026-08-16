# Doomscroller Prototype Fixes

## Issues diagnosed
1. **Missing APScheduler** – ImportError: No module named 'apscheduler' prevented `app.main` import.
2. **Windows SQLite URL** – `sqlite:///D:\Path\...` contains backslashes and spaces, causing SQLAlchemy URL parsing issues.
3. **Unicode console errors** – Emoji prints in `scripts/init_db.py` / `scripts/seed.py` cause `UnicodeEncodeError` on cp1252 Windows console.
4. **Mock mode not seeded** – Without seed data dashboard appears empty.

## Fixes applied
* Created minimal local stub for `apscheduler`:
  `apscheduler/__init__.py`, `apscheduler/schedulers/background.py`, `apscheduler/triggers/interval.py`
  Allows the app to import and run without installing the real package.
* Fixed `app/config.py` `db_url` property to use `Path.as_posix()` so SQLite URLs use forward slashes on Windows.
* Removed emoji prints from `scripts/init_db.py` and `scripts/seed.py`.
* Verified mock pipeline: scrape → transform → metadata fallback → mock publish.

## Quick start – working prototype
```bash
cd D:\Sarthak\Coding\My Codes\Vibe-coding\doomscroller
python scripts/init_db.py
python scripts/seed.py 20
# Start API
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Health check:
```bash
curl http://127.0.0.1:8000/health
```
Pending videos:
```bash
curl http://127.0.0.1:8000/videos/pending
```

Approve a video:
```bash
curl -X POST http://127.0.0.1:8000/videos/<id>/approve -H "Content-Type: application/json" -d '{"approved": true}'
```
Transform:
```bash
curl -X POST http://127.0.0.1:8000/videos/<id>/transform
```
Publish:
```bash
curl -X POST http://127.0.0.1:8000/videos/<id>/publish
```

Scheduler is stubbed – jobs are registered but run synchronously via API. For full autonomous mode install the real `apscheduler` package.

All changes are in workspace; no external installs required for the prototype.
