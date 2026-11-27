"""Decide what the showcase workflow does for one source, and expose the source's fields as step outputs.

    python scripts/showcase/plan.py --source-id black_holes [--event push|workflow_dispatch]
                                    [--dispatch-source all|<id>] [--vet-only true|false]

Outputs (to $GITHUB_OUTPUT on Actions): selected, run_cut, and the source's url, captions_url, sha256, bytes,
title, count, max_clip_seconds, caption_override. A source is cut only when it is approved, its sha256 is
pinned and the trigger asks for it (dispatch without vet_only, or a push with cut_on_push). Every selected
source still gets the vetting pack. No secrets are read here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import github_output  # noqa: E402

EDL = Path(__file__).resolve().parent / "edit_decision_list.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--edl", default=str(EDL))
    ap.add_argument("--source-id", required=True)
    ap.add_argument("--event", default="workflow_dispatch")
    ap.add_argument("--dispatch-source", default="")
    ap.add_argument("--vet-only", default="false")
    args = ap.parse_args(argv)

    sources = json.loads(Path(args.edl).read_text(encoding="utf-8"))["sources"]
    if args.source_id not in sources:
        raise SystemExit(f"unknown source id {args.source_id!r}; known: {', '.join(sources)}")
    src = sources[args.source_id]
    wanted = (args.dispatch_source or "all").strip()
    selected = args.event != "workflow_dispatch" or wanted in ("all", args.source_id)
    vet_only = args.vet_only.strip().lower() == "true"
    asked = (not vet_only) if args.event == "workflow_dispatch" else bool(src.get("cut_on_push"))
    approved = bool(src.get("approved"))
    pinned = bool(src.get("sha256"))
    run_cut = selected and asked and approved and pinned

    if not selected:
        reason = f"not selected (dispatch asked for {wanted})"
    elif run_cut:
        reason = "vetting pack, then cut"
    elif not approved:
        reason = "vetting pack only: not approved in the edit list yet"
    elif not pinned:
        reason = "vetting pack only: sha256 not pinned in the edit list"
    else:
        reason = "vetting pack only: " + ("dispatch asked for vet_only" if args.event == "workflow_dispatch"
                                          else "cut_on_push is false for this source")
    print(f"[plan] {args.source_id}: {reason}")
    if src.get("open_items") and not approved:
        for item in src["open_items"]:
            print(f"[plan]   open: {item}")
    github_output(
        selected=selected, run_cut=run_cut, url=src["url"], captions_url=src.get("captions_url") or "",
        sha256=src.get("sha256") or "", bytes=src.get("bytes") or "", title=src["title"],
        count=int(src.get("count", 8)), max_clip_seconds=int(src.get("max_clip_seconds", 45)),
        caption_override=bool(src.get("caption_detector_override")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
