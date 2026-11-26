"""Geometry for the admin panel's inline-SVG charts. No chart library: Python computes coordinates,
Jinja macros (templates/admin/_charts.html) draw them. Every string produced here is built from
numbers only, so it is safe to place in an attribute under autoescape."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta


def nice_max(v: float) -> float:
    """Round an axis maximum up to 1/2/2.5/5 x 10^n."""
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def series_chart(series: list[dict], labels: list[str], *, width: int = 640, height: int = 180,
                 pad_l: int = 36, pad_b: int = 22, pad_t: int = 10, pad_r: int = 10) -> dict:
    """Multi-series line/area chart. series: [{"name", "values": [..], "kind": "area"|"line"}]."""
    n = max((len(s["values"]) for s in series), default=0)
    top = nice_max(max((max(s["values"] or [0]) for s in series), default=0))
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def xy(i: int, v: float) -> tuple[float, float]:
        x = pad_l + (iw * i / (n - 1) if n > 1 else iw / 2)
        y = pad_t + ih - (ih * (v / top) if top else 0)
        return round(x, 1), round(y, 1)

    out = []
    for idx, s in enumerate(series):
        pts = [xy(i, float(v or 0)) for i, v in enumerate(s["values"])]
        if not pts:
            continue
        line = "M" + " L".join(f"{x},{y}" for x, y in pts)
        area = line + f" L{pts[-1][0]},{pad_t + ih} L{pts[0][0]},{pad_t + ih} Z"
        out.append({"name": s["name"], "kind": s.get("kind", "line"), "line": line, "area": area,
                    "idx": idx, "total": sum(float(v or 0) for v in s["values"]),
                    "last": s["values"][-1] if s["values"] else 0,
                    "points": [{"x": x, "y": y, "v": s["values"][i], "label": labels[i] if i < len(labels) else ""}
                               for i, (x, y) in enumerate(pts)]})
    ticks = [{"y": round(pad_t + ih - ih * f, 1), "v": top * f} for f in (0, 0.5, 1)]
    step = max(1, n // 6)
    xlabels = [{"x": xy(i, 0)[0], "text": labels[i]} for i in range(0, n, step) if i < len(labels)]
    return {"width": width, "height": height, "series": out, "ticks": ticks, "xlabels": xlabels,
            "pad_l": pad_l, "baseline": pad_t + ih, "right": width - pad_r, "max": top}


def sparkline(values: list[float], width: int = 96, height: int = 24) -> str:
    if not values:
        return ""
    top = max(values) or 1
    n = len(values)
    pts = [(round(width * i / (n - 1) if n > 1 else width / 2, 1), round(height - 2 - (height - 4) * v / top, 1))
           for i, v in enumerate(values)]
    return "M" + " L".join(f"{x},{y}" for x, y in pts)


def days(since: datetime, until: datetime) -> list[date]:
    d, end, out = since.date(), until.date(), []
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def bucket_daily(stamps: list[datetime], day_list: list[date]) -> list[int]:
    idx = {d: i for i, d in enumerate(day_list)}
    counts = [0] * len(day_list)
    for ts in stamps:
        if ts is None:
            continue
        i = idx.get(ts.date())
        if i is not None:
            counts[i] += 1
    return counts


def bars(rows: list[tuple], *, key: int = 1) -> list[dict]:
    """[(label, value, ...)] -> rows with a 0-100 width for CSS bars."""
    top = max((float(r[key] or 0) for r in rows), default=0) or 1
    return [{"label": r[0], "value": r[key], "pct": round(100 * float(r[key] or 0) / top, 1), "row": r} for r in rows]


def funnel(steps: list[tuple[str, int]]) -> list[dict]:
    first = steps[0][1] if steps and steps[0][1] else 0
    out, prev = [], None
    for label, n in steps:
        out.append({"label": label, "n": n, "pct_of_first": round(100 * n / first, 1) if first else 0,
                    "pct_of_prev": round(100 * n / prev, 1) if prev else None,
                    "width": max(1.5, round(100 * n / first, 1)) if first else 1.5})
        prev = n
    return out


def scroll_curve(depths: list[int]) -> list[dict]:
    """% of page views that reached at least each depth (0, 10, ... 100)."""
    total = len(depths) or 0
    return [{"depth": d, "pct": round(100 * sum(1 for x in depths if (x or 0) >= d) / total, 1) if total else 0}
            for d in range(0, 101, 10)]


def path_for_curve(points: list[dict], width: int = 560, height: int = 180, pad: int = 28) -> dict:
    iw, ih = width - 2 * pad, height - 2 * pad
    pts = [(round(pad + iw * p["depth"] / 100, 1), round(pad + ih - ih * p["pct"] / 100, 1)) for p in points]
    line = "M" + " L".join(f"{x},{y}" for x, y in pts) if pts else ""
    area = (line + f" L{pts[-1][0]},{pad + ih} L{pts[0][0]},{pad + ih} Z") if pts else ""
    return {"width": width, "height": height, "line": line, "area": area, "pad": pad, "ih": ih, "iw": iw,
            "points": [{"x": x, "y": y, **p} for (x, y), p in zip(pts, points)]}
