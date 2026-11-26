"""Download trending SFX from Freesound (CC0) or MyInstants into content/sfx/.

Freesound needs FREESOUND_API_KEY (see .env.example). When --source is auto (default),
Freesound is used if FREESOUND_API_KEY is set, otherwise MyInstants populates
content/sfx/en/, content/sfx/hi/, and content/sfx/trending/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.clipper import myinstants, trending_sfx  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="trending sounds to fetch (1..50)")
    parser.add_argument("--search", action="append", default=[],
                        help="extra search term to fetch top result for (repeatable)")
    parser.add_argument("--source", choices=("auto", "freesound", "myinstants"), default="auto",
                        help="SFX source: auto (freesound if key set, else myinstants)")
    parser.add_argument("--lang", choices=("en", "hi", "both"), default="both",
                        help="language bucket for MyInstants (en, hi, or both)")
    parser.add_argument("--json", action="store_true", help="print result as JSON")
    return parser


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError(f"limit must be 1..50, got {limit}")


def _call_fetch(query: str, seconds: float, exclude: set[str], session: trending_sfx.SfxSession):
    try:
        return trending_sfx.fetch(query, seconds, exclude=exclude, session=session)
    except TypeError:
        return trending_sfx.fetch(query, seconds, exclude=exclude)


def _fetch_freesound(limit: int, searches: list[str]) -> dict:
    _validate_limit(limit)
    if not trending_sfx.available():
        raise ValueError("set FREESOUND_API_KEY first (see .env.example)")
    downloaded: list[str] = []
    skipped = 0
    session = trending_sfx.SfxSession()
    used: set[str] = set()
    try:
        candidates = trending_sfx.trending(limit=limit)
    except (trending_sfx.SearchBlocked, trending_sfx.SfxError) as exc:
        raise ValueError(trending_sfx.scrub_secrets(exc)) from None
    for _candidate in candidates:
        if session.stopped:
            break
        try:
            clip = _call_fetch("", 1.5, used, session)
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
        session.failed_files = 0
        used.add(clip.sfx_id)
        downloaded.append(Path(clip.path).name)
    for query in searches:
        if session.stopped:
            break
        try:
            clip = _call_fetch(query, 1.5, used, session)
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
        session.failed_files = 0
        used.add(clip.sfx_id)
        downloaded.append(Path(clip.path).name)
    return {"ok": True, "provider": trending_sfx.provider(), "downloaded": len(downloaded),
            "skipped": skipped, "files": sorted(set(downloaded))}


def fetch_packs(limit: int, searches: list[str], source: str = "auto", lang: str = "both") -> dict:
    _validate_limit(limit)
    chosen = (source or "auto").strip().lower()
    if chosen == "auto":
        chosen = "freesound" if trending_sfx.available() else "myinstants"
    if chosen == "freesound":
        return _fetch_freesound(limit, searches)
    if chosen == "myinstants":
        try:
            return myinstants.fetch_all(limit=limit, lang=lang, searches=searches)
        except RuntimeError as exc:
            raise ValueError(trending_sfx.scrub_secrets(exc)) from None
    raise ValueError(f"unsupported source: {source!r}")


def main(n: int | list[str] | None = None) -> int:
    if not isinstance(n, list):
        n = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(n)
    try:
        summary = fetch_packs(args.limit, args.search or [], source=args.source, lang=args.lang)
    except ValueError as exc:
        err = trending_sfx.scrub_secrets(exc)
        if args.json:
            print(json.dumps({"ok": False, "error": err}, indent=2))
        else:
            print(f"error: {err}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Fetched {summary['downloaded']} sounds via {summary['provider']} "
              f"({summary['skipped']} skipped) into content/sfx/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
