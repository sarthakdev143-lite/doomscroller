"""CLI to enqueue, run, retry, or inspect 16:9 long-form compilation jobs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import init_db  # noqa: E402
from app.longform import job as long_job  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="", help="Managed channel slug (default when omitted)")
    parser.add_argument("--format", dest="format_kind", default="best_of", choices=("best_of", "vs", "theme"))
    parser.add_argument("--creator", action="append", default=[], help="Creator @handle or feed link (repeatable)")
    parser.add_argument("--vs", default="", help="Two creators separated by 'vs' (e.g. '@A vs @B')")
    parser.add_argument("--theme", default="", help="Topic theme for theme compilations")
    parser.add_argument("--minutes", type=int, default=8, help="Target length in minutes (6-10)")
    parser.add_argument("--enqueue", action="store_true", help="Enqueue job and exit without waiting for render")
    parser.add_argument("--run-now", action="store_true", help="Enqueue (or retry) and build synchronously")
    parser.add_argument("--jobs", action="store_true", help="List recent long-form jobs")
    parser.add_argument("--retry", default="", help="Retry a failed long-form job id")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Print JSON output")
    args = parser.parse_args(argv)

    init_db()

    if args.jobs:
        jobs = long_job.list_long_jobs()
        if args.as_json:
            print(json.dumps({"jobs": jobs}, indent=2))
        else:
            for j in jobs:
                tgt = ", ".join(j["targets"]) or j["theme"] or "-"
                print(f"{j['id'][:8]}  {j['channel_slug'] or 'default':<12}  {j['format']:<8}  {j['status']:<8}  {tgt}")
            if not jobs:
                print("No long-form jobs.")
        return 0

    if args.retry:
        try:
            res = long_job.retry_long_job(args.retry)
        except (LookupError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if args.run_now:
            res = long_job.run_next_long_job() or res
        print(json.dumps(res, indent=2) if args.as_json else f"Retried {args.retry}: {res}")
        return 0

    targets = list(args.creator or [])
    target = args.vs.strip()
    if args.format_kind == "vs" and not target and len(targets) >= 2:
        target = f"{targets[0]} vs {targets[1]}"
    job_id = long_job.enqueue_long_job(
        args.channel,
        format_kind=args.format_kind,
        target=target,
        targets=targets,
        theme=args.theme,
        target_minutes=args.minutes,
    )
    if args.run_now:
        out = long_job.run_next_long_job() or {"ok": False, "job_id": job_id, "error": "worker returned None"}
        if args.as_json:
            print(json.dumps(out, indent=2))
        else:
            print(f"Built {job_id}: {out}")
        return 0 if out.get("ok") else 1

    payload = {"ok": True, "job_id": job_id, "status": "queued"}
    print(json.dumps(payload, indent=2) if args.as_json else f"Queued long-form job {job_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
