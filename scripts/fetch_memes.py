"""Download the meme/GIF pool the clipper overlays on reaction beats.

The clipper searches the library by reaction mood (shock, laugh, fail, awkward, hype, mindblown) at
render time and never repeats a meme a recent clip used (content/meme_history.json). ``--moods``
pre-downloads the top results of every mood's searches, so a render has a warm, varied pool even when
the library is slow or rate-limited. Giphy needs GIPHY_API_KEY (Google shut the Tenor API down on
2026-06-30); keyless MemeGen templates are stills and only used when CLIP_MEMES_ALLOW_STILLS is on.
Files land in ``content/memes/`` and are reused from cache, so running this weekly keeps the pool fresh.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.clipper import memes, reactions  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="trending memes to fetch (1..50)")
    parser.add_argument("--search", action="append", default=[],
                        help="extra search term to fetch top result for (repeatable)")
    parser.add_argument("--moods", nargs="?", const="all", default="",
                        help="pre-download each reaction mood's pool: 'all' or a comma list such as laugh,fail")
    parser.add_argument("--per-query", type=int, default=5, help="results kept per mood search (1..15)")
    parser.add_argument("--language", default="en", help="en or hi: which mood searches to warm")
    parser.add_argument("--json", action="store_true", help="print result as JSON")
    return parser


def _mood_list(raw: str) -> list[str]:
    wanted = [m.strip().lower() for m in str(raw or "").split(",") if m.strip()]
    if not wanted:
        return []
    if "all" in wanted:
        return list(reactions.MOODS)
    unknown = [m for m in wanted if m not in reactions.MOODS]
    if unknown:
        raise ValueError(f"unknown mood(s) {', '.join(unknown)}; choose from {', '.join(reactions.MOODS)}")
    return wanted


def warm_moods(moods: list[str], per_query: int = 5, language: str = "en") -> dict[str, int]:
    """Download the top results of each mood's searches. Returns files ready per mood."""
    if isinstance(per_query, bool) or not 1 <= per_query <= 15:
        raise ValueError(f"per-query must be 1..15, got {per_query}")
    ready: dict[str, int] = {}
    failed = 0
    for mood in moods:
        ready[mood] = 0
        for query in reactions.queries(mood, language):
            try:
                found = memes.search(query)[:per_query]
            except memes.SearchBlocked as exc:
                raise ValueError(memes.scrub_secrets(exc)) from None
            except memes.MemeError:
                continue
            for candidate in found:
                try:
                    path = memes._local_copy(candidate, 1.5)
                except (memes.MemeError, OSError):
                    failed += 1
                    if failed >= memes.MAX_FAILED_FILES:
                        raise ValueError("meme downloads keep failing") from None
                    continue
                if path is not None:
                    ready[mood] += 1
    return ready


def fetch_packs(limit: int, searches: list[str], moods: list[str] = (), per_query: int = 5,
                language: str = "en") -> dict:
    if isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError(f"limit must be 1..50, got {limit}")
    downloaded: list[str] = []
    skipped = 0
    session = memes.MemeSession()
    try:
        candidates = memes.trending(limit=limit)
    except memes.MemeError as exc:
        raise ValueError(memes.scrub_secrets(exc)) from None
    for candidate in candidates:
        try:
            clip = memes._local_copy(candidate, 1.5)
        except (memes.MemeError, OSError) as exc:
            skipped += 1
            session.failed_files += 1
            if session.failed_files >= memes.MAX_FAILED_FILES:
                raise ValueError(f"meme downloads keep failing: {memes.scrub_secrets(exc)}") from None
            continue
        if clip is None:
            skipped += 1
            continue
        session.failed_files = 0
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
    summary = {"ok": True, "provider": memes.provider(), "downloaded": len(downloaded),
               "skipped": skipped, "files": sorted(set(downloaded))}
    if moods:
        summary["moods"] = warm_moods(list(moods), per_query, language)
    return summary


def main(n: int | list[str] | None = None) -> int:
    if not isinstance(n, list):
        n = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(n)
    try:
        summary = fetch_packs(args.limit, args.search or [], _mood_list(args.moods), args.per_query, args.language)
    except ValueError as exc:
        err = memes.scrub_secrets(exc)
        if args.json:
            print(json.dumps({"ok": False, "error": err}, indent=2))
        else:
            print(f"error: {err}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Fetched {summary['downloaded']} memes via {summary['provider']} "
              f"({summary['skipped']} skipped) into content/memes/.")
        for mood, ready in summary.get("moods", {}).items():
            print(f"  {mood}: {ready} ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
