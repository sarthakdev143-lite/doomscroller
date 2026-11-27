"""Two landing directions for the owner to choose between (previews only; the live landing is landing.html).

A "dark studio" and B "dense light" share every section below the hero, so the owner compares design, not copy.
Nothing here is routed: scripts/saas_export_static.py --previews renders them into a scratch folder.

The price comparison is built ONLY from docs/saas/costs-and-pricing.md, section 3 (research memo dated
2026-11-27). Every figure below is copied from that table; tests/saas/test_web_redesign.py checks that each dollar
amount appears in the memo. Our own price is not shown until the owner approves one.
"""
from __future__ import annotations

from pathlib import Path

MEMO = "docs/saas/costs-and-pricing.md"
MEMO_DATE = "27 November 2026"

# Each figure is the memo's "$/min" for one plan: plan price divided by the source minutes it includes, at full use.
# The plan behind every figure is shown beside it. Alphabetical, so nothing is ranked in our favour.
COMPARISON = [
    {"tool": "Klap", "ref": "C3", "note": "Priced per clip; counted as 8 clips per 30-minute video.",
     "rows": [("Basic, $29 a month, 100 clips", "$0.077"), ("Basic billed yearly, $14.50 a month", "$0.039")]},
    {"tool": "Munch", "ref": "C5", "note": "Same price billed monthly or yearly.",
     "rows": [("Essential, $38 a month, 500 min", "$0.076"), ("Premium, $60 a month, 1,000 min", "$0.060")]},
    {"tool": "Opus Clip", "ref": "C1", "note": "Minutes per plan are second-hand.",
     "rows": [("Starter, $15 a month, 150 min", "$0.100"), ("Pro billed yearly, $14.50 a month, 300 min", "$0.048")]},
    {"tool": "Quso", "ref": "C6", "note": "",
     "rows": [("Growth, $49 a month, 600 credits", "$0.082"), ("Growth billed yearly, $24.50 a month", "$0.041")]},
    {"tool": "Reap", "ref": "C17", "note": "",
     "rows": [("Creator, $24 a month, 600 min", "$0.040"), ("Creator billed yearly, $9.99 a month", "$0.0167")]},
    {"tool": "Submagic", "ref": "C4", "note": "Priced per video; counted as 30-minute videos, at $1 = ₹94.",
     "rows": [("Starter, ₹799 a month, 5 long videos", "~$0.057")]},
    {"tool": "WayinVideo", "ref": "C16", "note": "The lower figure was a Black Friday sale price.",
     "rows": [("Pro at list price, $26.99 a month, 12,000 credits a year", "$0.027"),
              ("Pro on sale, $9.58 a month billed yearly", "$0.0096")]},
]

SOURCES = {
    "C1": ("Opus Clip", "https://www.opus.pro/pricing", "plans seen 27 November 2026; minutes per plan from "
           "castmagic.io and stork.ai (second-hand)"),
    "C3": ("Klap", "https://klap.app/pricing", "page updated 4 November 2026"),
    "C4": ("Submagic", "https://www.submagic.co/pricing", f"seen for the memo of {MEMO_DATE}"),
    "C5": ("Munch", "https://www.munchstudio.com/pricing", f"seen for the memo of {MEMO_DATE}"),
    "C6": ("Quso", "https://quso.ai/pricing", f"seen for the memo of {MEMO_DATE}"),
    "C16": ("WayinVideo", "https://wayin.ai/pricing/", "rendered in a browser 27 November 2026"),
    "C17": ("Reap", "https://reap.video/pricing", f"seen for the memo of {MEMO_DATE}"),
}

# The three clips of the demo episode, as the before/after section shows them. Captions are the renderer's groups
# (at most three words); the hook is the title burned into the opening seconds. Scripted, drawn, not footage.
DEMO_TILES = [
    {"n": 1, "focus": "a", "hook": "The first 100 videos are practice",
     "groups": ["Nobody tells you", "the first hundred", "videos are practice."]},
    {"n": 2, "focus": "b", "hook": "Views are not the score",
     "groups": ["Views tell you", "who clicked.", "Watch time tells", "you who stayed."]},
    {"n": 3, "focus": "a", "hook": "Finish it, then fix the next one",
     "groups": ["Ship it before", "it feels ready,", "then fix", "the next one."]},
]


def demo_videos(static_dir: Path, asset) -> list[dict] | None:
    """Real clips for the before/after section, when the owner has put all three in static/demo/.

    All or nothing: clip-1.mp4 .. clip-3.mp4 plus clip-1.jpg .. clip-3.jpg posters. URLs go through asset() so a
    replaced file gets a new ?v= (static files are cached as immutable)."""
    folder = Path(static_dir) / "demo"
    out = []
    for n in (1, 2, 3):
        video, poster = folder / f"clip-{n}.mp4", folder / f"clip-{n}.jpg"
        if not (video.is_file() and poster.is_file()):
            return None
        out.append({"n": n, "src": asset(f"/static/demo/clip-{n}.mp4"), "poster": asset(f"/static/demo/clip-{n}.jpg")})
    return out


def context(static_dir: Path, asset) -> dict:
    refs = []
    for row in COMPARISON:
        if row["ref"] not in refs:
            refs.append(row["ref"])
    return {
        "comparison": COMPARISON,
        "comparison_sources": [{"ref": r, "tool": SOURCES[r][0], "url": SOURCES[r][1], "seen": SOURCES[r][2]}
                               for r in sorted(refs, key=lambda r: int(r[1:]))],
        "memo_date": MEMO_DATE,
        "demo_tiles": DEMO_TILES,
        "demo_videos": demo_videos(static_dir, asset),
        "founder": {"name": "Sarthak Parulekar", "initials": "SP"},
    }
