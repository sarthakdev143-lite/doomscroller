## Summary

<!-- What changed and why? Link any related issue, e.g. `Fixes #123`. -->

## Verification

- [ ] Targeted tests added/updated (RED → GREEN)
- [ ] `pytest -q -m "not slow"` passes
- [ ] `python scripts/doctor.py` and `python scripts/final_audit.py --offline` pass
- [ ] If `app/review_ui.py` changed: `node --check` passes on `app.review_ui.PAGE`

## Hard Invariants Checklist (`CONTRIBUTING.md`)

- [ ] No `numpy` imported at idle startup (`app.main`, `app.review`, `app.config`, `app.db`, `app.logging_config`)
- [ ] No `cv2` imported in `app/clipper/shots.py`
- [ ] Every non-root route on `app.review.review_app` calls `_guard(request)` first; `app/review.py` stays a thin shell
- [ ] Every background `subprocess.run` / `Popen` passes `stdin=subprocess.DEVNULL` and `**background_subprocess_kwargs(low_priority=True)` from `app.winproc`
- [ ] SQLite schema changes (if any) are strictly additive and nullable
- [ ] Zero secrets, tokens, `.env`, `channels.json`, `cookies*.txt`, `logs/`, `content/`, or `*.db*` files staged
