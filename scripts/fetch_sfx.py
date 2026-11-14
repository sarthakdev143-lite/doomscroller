"""Download trending CC0 SFX from Freesound into the runtime pack (content/sfx/).

Only Creative Commons 0 sounds are kept, so clips need no attribution line and
the synth fallback stays silent. Needs a free key from
https://freesound.org/apiv2/apply (set FREESOUND_API_KEY, see .env.example).
Without a key there is nothing to download; the synth still plays.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.clipper import trending_sfx  # noqa: E402
from app.config import settings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="trending sounds to fetch (1..50)")
    parser.add_argument("--search", action="append", default=[],
                        help="extra search term to fetch top result for (repeatable)")
    parser.add_argument("--json", action="store_true", help="print result as JSON")
    return parser


def fetch_packs(limit: int, searches: list[str]) -> dict:
    if not trending_sfx.available():
        raise ValueError("set FREESOUND_API_KEY first (see .env.example)")
    if not 1 <= limit <= 50:
        raise ValueError(f"limit must be 1..50, got {limit}")
    downloaded: list[str] = []
    skipped = 0
    session = trending_sfx.SfxSession()
    used: set[str] = set()
    for candidate in trending_sfx.trending(limit=limit):
        if session.stopped:
            break
        try:
            clip = trending_sfx.fetch("", 1.5, exclude=used)
        except trending_sfx.SearchBlocked as exc:
            raise ValueError(trending_sfx.scrub_secrets(exc)) from None
        except trending_sfx.DownloadsFailed:
            session.failed_files += 1
            if session.failed_files >= trending_sfx.MAX_FAILED_FILES:
                session.stopped = True
            skipped += 1
            continue
        except trending_sfx.SfxError:
            skipped += 1
            continue
        if clip is None or clip.sfx_id in used:
            skipped += 1
            continue
        used.add(clip.sfx_id)
        downloaded.append(Path(clip.path).name)
    for query in searches:
        if session.stopped:
            break
        try:
            clip = trending_sfx.fetch(query, 1.5, exclude=used)
        except trending_sfx.SearchBlocked as exc:
            raise ValueError(trending_sfx.scrub_secrets(exc)) from None
        except trending_sfx.SfxError:
            skipped += 1
            continue
        if clip is None:
            skipped += 1
            continue
        used.add(clip.sfx_id)
        downloaded.append(Path(clip.path).name)
    return {"ok": True, "provider": trending_sfx.provider(), "downloaded": len(downloaded),
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
        print(f"Fetched {summary['downloaded']} CC0 sounds via {summary['provider']} "
              f"({summary['skipped']} skipped) into content/sfx/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
