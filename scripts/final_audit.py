"""Portable system, configuration & repository surface audit for Doomscroller."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _read_text(path: Path) -> str:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def check_surface(root: Path = PROJECT_ROOT) -> dict[str, bool]:
    """Verify key CLI scripts, Control Panel routes, CI workflow, and docs exist."""
    doctor_src = _read_text(root / "scripts" / "doctor.py")
    stop_ps1_src = _read_text(root / "stop.ps1")
    channels_cli_src = _read_text(root / "scripts" / "channels.py")
    channels_mod_src = _read_text(root / "app" / "growth" / "channels.py")
    approval_src = _read_text(root / "app" / "growth" / "approval.py")
    review_src = _read_text(root / "app" / "review.py")
    review_ui_src = _read_text(root / "app" / "review_ui.py")

    return {
        "doctor_cli": bool(doctor_src and "def run_doctor" in doctor_src),
        "stop_ps1": bool(stop_ps1_src and "--stop" in stop_ps1_src),
        "channels_portability": bool(
            channels_cli_src
            and channels_mod_src
            and all(cmd in channels_cli_src for cmd in ("export", "import", "clone"))
            and all(
                fn in channels_mod_src
                for fn in ("def export_channels_snapshot", "def import_channels_snapshot", "def clone_channel")
            )
        ),
        "review_logs_route": bool("/api/logs" in review_src and "/api/logs" in review_ui_src),
        "review_save_mp4": bool(
            "/api/media/{video_id}" in review_src
            and "download" in review_src
            and "content-disposition" in review_src.lower()
            and "download=1" in review_ui_src
        ),
        "review_approve_clean": bool(
            "/api/clips/approve-clean" in review_src and "def approve_clean" in approval_src
        ),
        "ci_workflow": (root / ".github" / "workflows" / "ci.yml").is_file(),
        "contributing_guide": (root / "CONTRIBUTING.md").is_file(),
    }


def _check_live_review_guards() -> bool:
    """Verify app.review stays a thin shell and every non-root route calls _guard."""
    from fastapi.routing import APIRoute

    from app import review

    if hasattr(review, "ClipJob"):
        return False
    api_routes = [r for r in review.review_app.routes if isinstance(r, APIRoute) and r.path != "/"]
    if not api_routes:
        return False
    return all("_guard" in r.endpoint.__code__.co_names for r in api_routes)


def run_audit(*, live: bool = True) -> dict:
    from sqlalchemy import select

    from app import scheduler
    from app.config import settings
    from app.db import init_db, session_scope
    from app.models import Video
    from app.scrapers.yt_dlp_scraper import YtDlpScraper

    scraper = YtDlpScraper()
    test_handles = [
        ("@charlidamelio", "instagram"),
        ("snapchat.com/add/test", "snapchat"),
        ("https://www.youtube.com/@test", "youtube"),
        ("https://www.reddit.com/r/videos", "reddit"),
    ]
    platform_checks: list[dict] = []
    for handle, expected in test_handles:
        actual = scraper._platform_for_handle(handle)
        platform_checks.append(
            {"handle": handle, "expected": expected, "actual": actual, "ok": actual == expected}
        )

    init_db()
    with session_scope() as db:
        rows = db.execute(select(Video.source, Video.status)).all()
    source_counts = Counter(src for src, _ in rows)
    status_counts = Counter(st.value if hasattr(st, "value") else str(st) for _, st in rows)

    surface_checks = check_surface(PROJECT_ROOT)
    surface_checks["review_routes_guarded"] = _check_live_review_guards()
    platforms_ok = all(c["ok"] for c in platform_checks)
    surface_ok = all(surface_checks.values())

    out: dict = {
        "platforms_ok": platforms_ok,
        "surface_ok": surface_ok,
        "all_ok": platforms_ok and surface_ok,
        "platform_checks": platform_checks,
        "surface_checks": surface_checks,
        "enabled_platforms": settings.enabled_platforms_list,
        "hashtags": settings.hashtags_list,
        "use_mock": settings.use_mock,
        "growth_enabled": settings.growth_enabled,
        "review_token_set": bool(settings.review_token),
        "sources": dict(source_counts),
        "statuses": dict(status_counts),
        "storage": scheduler.storage_report(),
    }

    if live:
        import httpx

        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get("http://127.0.0.1:8000/health")
                out["api_health"] = resp.json() if resp.status_code == 200 else {"status_code": resp.status_code}
        except Exception as exc:
            out["api_health"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip live HTTP check against 127.0.0.1:8000")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit audit report as JSON")
    args = parser.parse_args(argv)

    report = run_audit(live=not args.offline)
    if args.as_json:
        print(json.dumps(report, indent=2))
        return 0 if report["all_ok"] else 1

    print("=== PLATFORM DETECTION ===")
    for chk in report["platform_checks"]:
        mark = "OK" if chk["ok"] else f"FAIL (got {chk['actual']})"
        print(f"  {chk['handle']:<35} -> {chk['actual']:<12} {mark}")

    print("\n=== REPO & WAVE 11 SURFACE ===")
    for name, ok in report["surface_checks"].items():
        print(f"  {name:<26} : {'OK' if ok else 'MISSING'}")

    print("\n=== CONFIG ===")
    print(f"  enabled_platforms : {report['enabled_platforms']}")
    print(f"  growth_enabled    : {report['growth_enabled']}")
    print(f"  use_mock          : {report['use_mock']}")
    print(f"  review_token_set  : {report['review_token_set']}")

    print("\n=== DB SUMMARY ===")
    print(f"  by source : {report['sources'] or 'empty'}")
    print(f"  by status : {report['statuses'] or 'empty'}")
    print(f"  storage   : {report['storage']['total_mb']} MB ({report['storage']['files']} files)")

    if "api_health" in report:
        print(f"\n=== API HEALTH ===\n  {report['api_health']}")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
