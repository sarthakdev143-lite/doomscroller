"""Download trending SFX from Freesound (CC0) or MyInstants into content/sfx/.

Freesound needs FREESOUND_API_KEY (see .env.example). When --source is auto (default),
Freesound is used if FREESOUND_API_KEY is set, otherwise MyInstants populates
content/sfx/en/, content/sfx/hi/, and content/sfx/trending/.

--cc0 builds the separate CC0-only pack the SaaS seam uses (content/sfx_cc0/, or CLIP_CC0_SFX_DIR):
CC0 Freesound sounds per reaction mood when FREESOUND_API_KEY is set, synthesised stings otherwise,
with MANIFEST.json naming every file's source and licence. The owner pack is never touched.

    python scripts/fetch_sfx.py --cc0                 # auto: Freesound CC0 if keyed, else synthesised
    python scripts/fetch_sfx.py --cc0 --source synth  # always synthesised
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.clipper import cc0_sfx, myinstants, trending_sfx  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="trending sounds to fetch (1..50)")
    parser.add_argument("--search", action="append", default=[],
                        help="extra search term to fetch top result for (repeatable)")
    parser.add_argument("--source", choices=("auto", "freesound", "myinstants", "synth"), default="auto",
                        help="SFX source: auto (freesound if key set, else myinstants; with --cc0, else synth)")
    parser.add_argument("--cc0", action="store_true",
                        help="build the CC0-only SaaS pack instead (Freesound CC0 or synthesised)")
    parser.add_argument("--out", default="", help="with --cc0: the pack folder (default content/sfx_cc0)")
    parser.add_argument("--per-query", type=int, default=1, help="with --cc0: Freesound sounds per mood query (1..5)")
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


def build_cc0(out: str = "", source: str = "auto", per_query: int = 1) -> dict:
    if source == "myinstants":
        raise ValueError("MyInstants sounds are not CC0; use --source freesound, synth or auto")
    if isinstance(per_query, bool) or not 1 <= per_query <= 5:
        raise ValueError(f"per-query must be 1..5, got {per_query}")
    if source == "freesound" and not trending_sfx.provider():
        raise ValueError("set FREESOUND_API_KEY first (see .env.example), or use --source synth")
    try:
        return cc0_sfx.build_pack(Path(out) if out else None, source=source, per_query=per_query)
    except trending_sfx.SfxError as exc:
        raise ValueError(trending_sfx.scrub_secrets(exc)) from None


def fetch_packs(limit: int, searches: list[str], source: str = "auto", lang: str = "both") -> dict:
    _validate_limit(limit)
    chosen = (source or "auto").strip().lower()
    if chosen == "auto":
        chosen = "freesound" if trending_sfx.available() else "myinstants"
    if chosen == "freesound":
        return _fetch_freesound(limit, searches)
    if chosen == "synth":
        raise ValueError("synthesised sounds only build the CC0 pack: add --cc0")
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
        if args.cc0:
            summary = build_cc0(args.out, args.source, args.per_query)
        else:
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
    elif args.cc0:
        print(f"Built the CC0 pack ({summary['source']}): {summary['sounds']} sounds for "
              f"{', '.join(summary['moods'])} in {summary['folder']}.")
    else:
        print(f"Fetched {summary['downloaded']} sounds via {summary['provider']} "
              f"({summary['skipped']} skipped) into content/sfx/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
