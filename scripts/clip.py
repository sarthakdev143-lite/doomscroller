#!/usr/bin/env python3
"""Cut one long video into finished Shorts from the command line.

    venv/bin/python scripts/clip.py "https://www.youtube.com/watch?v=..."
    venv/bin/python scripts/clip.py ~/Videos/talk.mp4 --count 3 --queue
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clipper.job import clip_source  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="video link or local file")
    parser.add_argument("--count", type=int, default=None, help="clips to make (default: MAX_CLIPS_PER_VIDEO)")
    parser.add_argument("--queue", action="store_true", help="send clips straight to the publish queue")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()

    init_db()
    result = clip_source(args.source, count=args.count, auto_queue=True if args.queue else None,
                         progress=lambda stage: print(f"... {stage}", flush=True))
    if args.json:
        print(json.dumps(result, indent=2))
    for warning in result.get("warnings", []):
        print(f"warning: {warning}")
    if not result.get("ok"):
        print(f"failed: {result.get('error')}")
        return 1
    print(f"\n{len(result['clips'])} {result['kind']} clips from: {result['source']}")
    for clip in result["clips"]:
        print(f"  {clip['duration']:>5.1f}s  {clip['title']}\n         {clip['path']}")
    print("\nThey are in the publish queue." if result["queued"] else "\nReview them in the dashboard's Pending tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
