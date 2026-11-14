"""Download the trending meme/GIF pack the clipper overlays on punch beats.

Keyless MemeGen templates always work; Tenor or Giphy keys (see .env.example)
unlock their fresher trending packs. Files land in ``content/memes/`` and are
reused from cache, so running this weekly keeps the pack fresh.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.clipper import memes  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="trending memes to fetch (1..50)")
    parser.add_argument("--search", action="append", default=[],
                        help="extra search term to fetch top result for (repeatable)")
    parser.add_argument("--json", action="store_true", help="print result as JSON")
    return parser


def fetch_packs(limit: int, searches: list[str]) -> dict:
    if not 1 <= limit <= 50:
        raise ValueError(f"limit must be 1..50, got {limit}")
    downloaded: list[str] = []
    skipped = 0
    session = memes.MemeSession()
    for candidate in memes.trending(limit=limit):
        try:
            clip = memes._local_copy(candidate, 1.5)
        except memes.MemeError as exc:
            skipped += 1
            session.failed_files += 1
            if session.failed_files >= memes.MAX_FAILED_FILES:
                raise ValueError(f"meme downloads keep failing: {memes.scrub_secrets(exc)}") from None
            continue
        if clip is None:
            skipped += 1
            continue
        downloaded.append(clip.name)
    for query in searches:
        try:
            clip = memes.fetch(query, 1.5)
        except memes.SearchBlocked as exc:
            raise ValueError(memes.scrub_secrets(exc)) from None
        except memes.MemeError:
            skipped += 1
            continue
        if clip is None:
            skipped += 1
            continue
        downloaded.append(Path(clip.path).name)
    return {"ok": True, "provider": memes.provider(), "downloaded": len(downloaded),
            "skipped": skipped, "files": sorted(set(downloaded))}


def main(n: int | list[str] | None = None) -> int:
    if not isinstance(n, list):
        n = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(n)
    try:
        summary = fetch_packs(args.limit, args.search or [])
    except ValueError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Fetched {summary['downloaded']} memes via {summary['provider']} "
              f"({summary['skipped']} skipped) into content/memes/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
