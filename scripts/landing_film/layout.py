"""The strip's layout rule, shared by prep.py (system Python) and film.py (Blender's Python, which has no Pillow)."""
from __future__ import annotations

COLS = 8  # tiles per atlas row


def cells_from(cfg: dict) -> list[dict]:
    """Every sentence of film.json split into cells of about cell_seconds, so each sentence boundary falls in a gap
    between two tiles. gap_after is "cut" where the pipeline cut, "sentence" at any other sentence end, else None."""
    edges = [0.0] + [b[0] for b in cfg["bounds"]] + [cfg["duration"]]
    cuts = {b[0] for b in cfg["bounds"] if b[1]}
    out: list[dict] = []
    for s in range(len(edges) - 1):
        a, b = edges[s], edges[s + 1]
        k = max(1, round((b - a) / cfg["cell_seconds"]))
        for j in range(k):
            lo, hi = a + (b - a) * j / k, a + (b - a) * (j + 1) / k
            clip = next((c for c, cl in enumerate(cfg["clips"])
                         if cl["start"] - 0.01 <= lo and hi <= cl["end"] + 0.01), -1)
            out.append({"i": len(out), "a": round(lo, 3), "b": round(hi, 3), "sentence": s, "clip": clip,
                        "gap_after": None})
        if s < len(edges) - 2:
            out[-1]["gap_after"] = "cut" if edges[s + 1] in cuts else "sentence"
    return out
