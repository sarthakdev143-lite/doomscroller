#!/usr/bin/env python3
"""Cut long videos into finished Shorts from the command line, or manage the clip queue.

    venv/Scripts/python scripts/clip.py "https://www.youtube.com/watch?v=..."
    venv/Scripts/python scripts/clip.py ~/Videos/talk.mp4 --count 3 --channel tech --queue
    venv/Scripts/python scripts/clip.py --inbox --enqueue          # queue every new file in content/inbox/
    venv/Scripts/python scripts/clip.py --batch links.txt          # one link or path per line, # comments
    venv/Scripts/python scripts/clip.py --jobs                     # inspect the queue
    venv/Scripts/python scripts/clip.py --retry 1a2b3c4d           # re-queue a failed job (id or prefix)
    venv/Scripts/python scripts/clip.py --clear-finished           # drop done and failed jobs from the list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from sqlalchemy import or_, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clipper.job import (  # noqa: E402
    _channel_slug_map,
    clear_finished_jobs,
    clip_source,
    enqueue,
    job_view,
    retry_job,
    scan_inbox_sources,
    visible_job_rows,
)
from app.clipper.media import Interrupted  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import ClipJob  # noqa: E402

_JOB_STATUSES = ("queued", "running", "done", "failed")
_MAX_BATCH_SOURCES = 500


def _fail(message: str, *, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


def _resolve_cli_channel(slug: str) -> tuple[Optional[str], bool, str]:
    """Return (channel_id_for_clips, channel_requires_approval, canonical_slug) or raise ValueError."""
    from app.growth import channels

    cleaned = slug.strip()
    with session_scope() as db:
        channels._ensure_seeded(db)
        ch = channels.channel_by_slug(db, cleaned)
        if ch is None:
            known = ", ".join(c.slug for c in channels.all_channels(db)) or "default"
            raise ValueError(f"unknown channel {cleaned!r} (known: {known})")
        return channels.event_channel(ch), channels.require_approval(ch), ch.slug


def _inspect_jobs(*, limit: int, channel_slug: Optional[str], as_json: bool) -> int:
    if not 1 <= limit <= 100:
        return _fail("--limit must be between 1 and 100", as_json=as_json)

    from app.growth import channels

    with session_scope() as db:
        scope_clause = None
        resolved_slug = None
        if channel_slug and channel_slug.strip():
            channels._ensure_seeded(db)
            ch = channels.channel_by_slug(db, channel_slug.strip())
            if ch is None:
                known = ", ".join(c.slug for c in channels.all_channels(db)) or "default"
                return _fail(f"unknown channel {channel_slug.strip()!r} (known: {known})", as_json=as_json)
            resolved_slug = ch.slug
            if channels.is_default(ch):
                scope_clause = or_(ClipJob.channel_id == ch.id, ClipJob.channel_id.is_(None))
            else:
                scope_clause = ClipJob.channel_id == ch.id

        conditions = [scope_clause] if scope_clause is not None else []
        # Dismissed jobs stay in the table as history but are not part of the queue anyone looks at.
        raw_counts: dict[str, int] = {}
        for status, result in db.execute(select(ClipJob.status, ClipJob.result).where(*conditions)).all():
            if not (isinstance(result, dict) and result.get("dismissed") is True):
                raw_counts[status] = raw_counts.get(status, 0) + 1
        counts = {st: int(raw_counts.get(st, 0)) for st in _JOB_STATUSES}
        rows = visible_job_rows(db, limit, *conditions)
        slugs = _channel_slug_map(db, rows)
        views = [job_view(j, slugs) for j in rows]

    if as_json:
        payload: dict = {"ok": True, "counts": counts, "jobs": views}
        if resolved_slug:
            payload["channel"] = resolved_slug
        print(json.dumps(payload, indent=2))
        return 0

    header_chan = f" [{resolved_slug}]" if resolved_slug else ""
    summary = "  ".join(f"{st}={counts[st]}" for st in _JOB_STATUSES)
    print(f"Clip queue{header_chan}: {summary}")
    if not views:
        print("  No clip jobs recorded.")
        return 0

    for item in views:
        chan_label = item["channel_slug"] or "default"
        detail = item["stage"] or item["error"] or ""
        clips_made = (item.get("result") or {}).get("clips")
        if not detail and isinstance(clips_made, list) and item["status"] == "done":
            detail = f"{len(clips_made)} clip(s)"
        suffix = f" ({detail})" if detail else ""
        print(f"  {item['id'][:8]}  {item['status']:<7}  [{chan_label}]  {item['source']}{suffix}")
    return 0


def _read_batch_file(raw_path: str) -> list[str]:
    """Links and paths from a text file: one per line, blank lines and # comments skipped, repeats dropped."""
    path = Path(raw_path.strip().strip('"')).expanduser()
    if not path.is_file():
        raise ValueError(f"batch file not found: {raw_path}")
    try:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError as exc:
        raise ValueError(f"cannot read batch file {raw_path}: {exc}") from exc
    sources: list[str] = []
    for line in text.splitlines():
        entry = line.strip().lstrip("\ufeff").strip()
        if len(entry) >= 2 and entry[0] == entry[-1] and entry[0] in ("'", '"'):
            entry = entry[1:-1].strip()
        if not entry or entry.startswith("#") or entry in sources:
            continue
        sources.append(entry)
    if not sources:
        raise ValueError(f"no sources found in {raw_path} (one link or path per line; # starts a comment)")
    if len(sources) > _MAX_BATCH_SOURCES:
        raise ValueError(f"{len(sources)} sources is too many for one batch (limit {_MAX_BATCH_SOURCES})")
    return sources


def _enqueue_many(sources: list[str], *, count: Optional[int], auto_queue: Optional[bool],
                  channel_id: Optional[str], resolved_slug: Optional[str], as_json: bool) -> int:
    with session_scope() as db:
        active = {(src, cid) for src, cid in db.execute(
            select(ClipJob.source, ClipJob.channel_id).where(ClipJob.status.in_(("queued", "running")))
        ).all()}
    enqueued: list[dict] = []
    skipped: list[str] = []
    for source in sources:
        if (source, channel_id) in active:
            skipped.append(source)
            continue
        job_id = enqueue(source, count=count, auto_queue=auto_queue, channel_id=channel_id)
        enqueued.append({"id": job_id, "source": source})
        active.add((source, channel_id))
    if as_json:
        payload: dict = {"ok": True, "mode": "enqueue", "enqueued": enqueued, "skipped": skipped}
        if resolved_slug:
            payload["channel"] = resolved_slug
        print(json.dumps(payload, indent=2))
        return 0
    if not enqueued:
        print("Nothing new to queue." if not skipped else f"Nothing new to queue: {len(skipped)} already waiting.")
        return 0
    chan_note = f" [{resolved_slug}]" if resolved_slug else ""
    print(f"Queued {len(enqueued)} clip job(s){chan_note}:")
    for item in enqueued:
        print(f"  {item['id'][:8]}  {item['source']}")
    if skipped:
        print(f"Skipped {len(skipped)} already waiting.")
    print("The running Doomscroller daemon works through them; check with --jobs.")
    return 0


def _run_many(sources: list[str], *, count: Optional[int], auto_queue: Optional[bool], mine: bool,
              channel_id: Optional[str], resolved_slug: Optional[str], as_json: bool) -> int:
    """Clip each source in turn in this process. One failing source never stops the rest."""
    stream = sys.stderr if as_json else sys.stdout
    results: list[dict] = []
    total = len(sources)
    for number, source in enumerate(sources, 1):
        print(f"[{number}/{total}] {source}", file=stream, flush=True)
        try:
            result = clip_source(
                source, count=count, auto_queue=auto_queue,
                progress=lambda stage: print(f"... {stage}", file=stream, flush=True),
                mine=mine, channel_id=channel_id,
            )
        except Interrupted:
            results.append({"ok": False, "input": source, "error": "stopped before it finished"})
            break
        results.append({**result, "input": source})
    failed = sum(1 for r in results if not r.get("ok"))
    succeeded = len(results) - failed
    if as_json:
        payload: dict = {"ok": failed == 0, "mode": "foreground", "total": total,
                         "succeeded": succeeded, "failed": failed, "results": results}
        if resolved_slug:
            payload["channel"] = resolved_slug
        print(json.dumps(payload, indent=2))
        return 0 if failed == 0 else 1
    for item in results:
        if item.get("ok"):
            print(f"  ok    {item['input']}: {len(item.get('clips') or [])} {item.get('kind', '')} clip(s)")
        else:
            print(f"  FAIL  {item['input']}: {item.get('error')}")
        for warning in item.get("warnings") or []:
            print(f"        warning: {warning}")
    print(f"\n{succeeded} of {total} source(s) clipped" + (f", {failed} failed." if failed else "."))
    return 0 if failed == 0 else 1


def _retry(job_ref: str, *, as_json: bool) -> int:
    ref = job_ref.strip()
    if not ref:
        return _fail("--retry needs a job id (see --jobs)", as_json=as_json)
    with session_scope() as db:
        ids = db.execute(
            select(ClipJob.id).where(ClipJob.id.startswith(ref, autoescape=True)).limit(2)
        ).scalars().all()
    if not ids:
        return _fail(f"no clip job matches {ref!r}", as_json=as_json)
    if len(ids) > 1:
        return _fail(f"{ref!r} matches more than one job; use a longer id", as_json=as_json)
    try:
        view = retry_job(ids[0])
    except (LookupError, ValueError) as exc:
        return _fail(str(exc), as_json=as_json)
    if as_json:
        print(json.dumps({"ok": True, "job": view}, indent=2))
    else:
        print(f"Job {view['id'][:8]} is queued again.")
    return 0


def _clear_finished(*, as_json: bool) -> int:
    outcome = clear_finished_jobs()
    if as_json:
        print(json.dumps({"ok": True, **outcome}, indent=2))
    else:
        print(f"Cleared {outcome['cleared']} finished job(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", nargs="?", default=None, help="video link or local file")
    parser.add_argument("--count", type=int, default=None, help="clips to make (1..20, default: MAX_CLIPS_PER_VIDEO)")
    parser.add_argument("--channel", type=str, default=None, help="target managed channel slug (e.g. default, tech)")
    parser.add_argument("--queue", action="store_true", help="send clips straight to the publish queue")
    parser.add_argument("--mine", action="store_true", help="cut fresh clips from unused moments of this source")
    parser.add_argument("--jobs", "--status", dest="jobs", action="store_true", help="inspect clip queue jobs & exit")
    parser.add_argument("--limit", type=int, default=20, help="max jobs shown with --jobs/--status (default: 20)")
    parser.add_argument("--inbox", action="store_true", help="clip every new video file in the inbox folder")
    parser.add_argument("--batch", metavar="FILE", default=None,
                        help="clip every link or path in FILE (one per line, # comments)")
    parser.add_argument("--enqueue", action="store_true",
                        help="with a source, --inbox or --batch: add queue jobs for the daemon instead of clipping here")
    parser.add_argument("--retry", metavar="JOB_ID", default=None, help="queue a failed job again (id or id prefix)")
    parser.add_argument("--clear-finished", dest="clear_finished", action="store_true",
                        help="remove done and failed jobs from the queue list")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON to stdout")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    source = (args.source or "").strip()
    chosen = [name for name, active in (
        ("--jobs", args.jobs), ("--inbox", args.inbox), ("--batch", args.batch is not None),
        ("--retry", args.retry is not None), ("--clear-finished", args.clear_finished),
        ("a source", bool(source)),
    ) if active]
    if len(chosen) > 1:
        return _fail(f"choose one action, not several: {', '.join(chosen)}", as_json=args.json)
    if args.enqueue and not (source or args.inbox or args.batch is not None):
        return _fail("--enqueue goes with a source, --inbox or --batch", as_json=args.json)

    init_db()
    if args.jobs:
        return _inspect_jobs(limit=args.limit, channel_slug=args.channel, as_json=args.json)
    if args.retry is not None:
        return _retry(args.retry, as_json=args.json)
    if args.clear_finished:
        return _clear_finished(as_json=args.json)

    if not chosen:
        return _fail(
            "source link or file path is required (or pass --inbox, --batch FILE, --jobs, --retry, --clear-finished)",
            as_json=args.json,
        )
    if args.count is not None and not 1 <= args.count <= 20:
        return _fail("--count must be between 1 and 20", as_json=args.json)

    channel_id: Optional[str] = None
    resolved_slug: Optional[str] = None
    auto_queue: Optional[bool] = True if args.queue else None
    if args.channel and args.channel.strip():
        try:
            channel_id, req_approval, resolved_slug = _resolve_cli_channel(args.channel)
        except ValueError as exc:
            return _fail(str(exc), as_json=args.json)
        if not args.queue:
            auto_queue = not req_approval

    if args.inbox or args.batch is not None:
        try:
            if args.inbox:
                sources = scan_inbox_sources(resolved_slug or "")
            else:
                sources = _read_batch_file(args.batch)
        except ValueError as exc:
            return _fail(str(exc), as_json=args.json)
        if args.inbox and not sources:
            if args.json:
                payload = {"ok": True, "mode": "enqueue" if args.enqueue else "foreground", "found": 0,
                           "enqueued": [], "skipped": [], "total": 0, "succeeded": 0, "failed": 0, "results": []}
                print(json.dumps(payload, indent=2))
            else:
                print(f"Nothing new in the inbox ({settings.growth_inbox_path}).")
            return 0
        if args.enqueue:
            return _enqueue_many(sources, count=args.count, auto_queue=auto_queue, channel_id=channel_id,
                                 resolved_slug=resolved_slug, as_json=args.json)
        return _run_many(sources, count=args.count, auto_queue=auto_queue, mine=bool(args.mine),
                         channel_id=channel_id, resolved_slug=resolved_slug, as_json=args.json)

    if args.enqueue:
        return _enqueue_many([source], count=args.count, auto_queue=auto_queue, channel_id=channel_id,
                             resolved_slug=resolved_slug, as_json=args.json)

    progress_stream = sys.stderr if args.json else sys.stdout
    result = clip_source(
        source,
        count=args.count,
        auto_queue=auto_queue,
        progress=lambda stage: print(f"... {stage}", file=progress_stream, flush=True),
        mine=bool(args.mine),
        channel_id=channel_id,
    )

    if args.json:
        out_payload = dict(result)
        if resolved_slug:
            out_payload["channel"] = resolved_slug
        print(json.dumps(out_payload, indent=2))
        return 0 if result.get("ok") else 1

    for warning in result.get("warnings", []):
        print(f"warning: {warning}")
    if not result.get("ok"):
        print(f"failed: {result.get('error')}")
        return 1

    chan_note = f" [{resolved_slug}]" if resolved_slug else ""
    print(f"\n{len(result['clips'])} {result['kind']} clips{chan_note} from: {result['source']}")
    for clip in result["clips"]:
        score = clip.get("score")
        score_str = f"  score={score:.2f}" if isinstance(score, (int, float)) else ""
        print(f"  {clip['duration']:>5.1f}s  {clip['title']}{score_str}\n         {clip['path']}")
    print(
        "\nThey are in the publish queue."
        if result.get("queued")
        else "\nReview them in the Control Panel (Pending review)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
