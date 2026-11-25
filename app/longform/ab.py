"""Automatic Ambient A/B testing engine (pure stdlib + SQLAlchemy, zero numpy/cv2)."""
from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import select

from app.growth import channels, learn
from app.models import Channel, LongJob, Video, VideoSnapshot, VideoStatus

AMBIENT_THUMB_VARIANTS = ("v1", "v2", "v3")
_FACTORS = ("preset", "thumb", "portrait")


def record_arms(*, preset: str, thumb_id: str, portrait_path: str = "") -> dict:
    """Return normalized ambient A/B arm tags for storage in Video.meta['ambient_ab_arms']."""
    return {
        "preset": str(preset or "").strip(),
        "thumb": str(thumb_id or "").strip(),
        "portrait": Path(str(portrait_path)).stem if portrait_path else "",
    }


def _extract_arms(meta: dict) -> dict[str, str]:
    raw = meta.get("ambient_ab_arms") if isinstance(meta.get("ambient_ab_arms"), dict) else {}
    preset = str(raw.get("preset") or meta.get("ambient_preset") or meta.get("long_format") or "").strip()
    thumb = str(raw.get("thumb") or meta.get("selected_thumbnail_id") or "").strip()
    portrait = str(raw.get("portrait") or "").strip()
    if not portrait and meta.get("portrait_path"):
        portrait = Path(str(meta["portrait_path"])).stem
    return {"preset": preset, "thumb": thumb, "portrait": portrait}


def evaluate_ambient_ab(db, channel: Channel) -> dict:
    """Score published non-MOCK procedural ambient videos per preset, thumbnail, and portrait."""
    published = db.execute(
        select(Video).where(channels.video_scope(channel), Video.status == VideoStatus.PUBLISHED)
    ).scalars().all()
    vids = [
        v for v in published
        if (v.meta or {}).get("procedural_ambient")
        and v.youtube_video_id
        and not str(v.youtube_video_id).startswith("MOCK_")
    ]

    latest_views: dict[str, int] = {}
    if vids:
        snaps = db.execute(
            select(VideoSnapshot)
            .where(VideoSnapshot.video_id.in_([v.id for v in vids]))
            .order_by(VideoSnapshot.taken_at.desc(), VideoSnapshot.age_hours.desc())
        ).scalars().all()
        for snap in snaps:
            if snap.video_id not in latest_views:
                latest_views[snap.video_id] = max(0, int(snap.views or 0))

    buckets: dict[str, dict[str, list[tuple[int, Optional[float], float]]]] = {f: {} for f in _FACTORS}
    for v in vids:
        meta = v.meta or {}
        views = latest_views.get(v.id, max(0, int(v.views or 0)))
        yt = meta.get("yt_analytics")
        raw_ret = yt.get("averageViewPercentage") if isinstance(yt, dict) else None
        avg_ret = (
            float(raw_ret)
            if isinstance(raw_ret, (int, float))
            and not isinstance(raw_ret, bool)
            and math.isfinite(float(raw_ret))
            and float(raw_ret) >= 0.0
            else None
        )
        score = math.log1p(views) * (0.5 + avg_ret / 100.0) if avg_ret is not None else math.log1p(views)
        arms = _extract_arms(meta)
        for f in _FACTORS:
            arm = arms[f]
            if arm:
                buckets[f].setdefault(arm, []).append((views, avg_ret, score))

    factors: dict[str, list[dict]] = {}
    for f in _FACTORS:
        rows: list[dict] = []
        for arm, items in buckets[f].items():
            n = len(items)
            avg_v = sum(x[0] for x in items) / n
            rets = [x[1] for x in items if x[1] is not None]
            avg_r = (sum(rets) / len(rets)) if rets else None
            mean_score = sum(x[2] for x in items) / n
            rows.append({
                "arm": arm,
                "n": n,
                "avg_views": round(avg_v, 1),
                "avg_retention": round(avg_r, 1) if avg_r is not None else None,
                "score": round(mean_score, 4),
            })
        rows.sort(key=lambda r: (-r["score"], -r["n"], r["arm"]))
        factors[f] = rows

    best = {f: rows[0]["arm"] for f, rows in factors.items() if rows}
    summary = {"tested": len(vids), "factors": factors, "best": best}
    learn.set_state(db, channels.state_key(channel, "ambient_ab"), summary)
    db.flush()
    return summary


def _pending_counts(db, channel: Channel) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {f: {} for f in _FACTORS}
    jobs = db.execute(
        select(LongJob).where(LongJob.channel_id == channel.id, LongJob.status != "failed")
    ).scalars().all()
    for j in jobs:
        if j.status in ("queued", "running") or not j.video_id:
            fmt = str(j.format or "").strip()
            if fmt:
                counts["preset"][fmt] = counts["preset"].get(fmt, 0) + 1

    waiting = db.execute(
        select(Video).where(
            channels.video_scope(channel),
            Video.status.in_((VideoStatus.PENDING, VideoStatus.TRANSFORMED)),
        )
    ).scalars().all()
    for v in waiting:
        meta = v.meta or {}
        if not (meta.get("procedural_ambient") or meta.get("long_form")):
            continue
        arms = _extract_arms(meta)
        for f in _FACTORS:
            arm = arms[f]
            if arm:
                counts[f][arm] = counts[f].get(arm, 0) + 1
    return counts


def _choose_arm(
    arms: Sequence[str],
    stats_rows: list[dict],
    pending: dict[str, int],
    *,
    explore_rate: float,
    rng: random.Random,
) -> tuple[str, str]:
    clean = list(dict.fromkeys(str(a).strip() for a in arms if str(a).strip()))
    if not clean:
        return "", "empty"
    by_arm = {r["arm"]: r for r in stats_rows}
    untested = [a for a in clean if by_arm.get(a, {}).get("n", 0) == 0]
    if untested:
        return min(untested, key=lambda a: pending.get(a, 0)), "cold_start"
    total = sum(by_arm[a]["n"] for a in clean)
    if rng.random() >= explore_rate:
        pick = max(
            clean,
            key=lambda a: (
                by_arm[a]["score"]
                + math.sqrt(2.0 * math.log(total + 1) / by_arm[a]["n"]) * 0.35
                + rng.gauss(0, 0.08)
            ),
        )
        return pick, "exploit"
    pick = min(clean, key=lambda a: (by_arm[a]["n"] + pending.get(a, 0), by_arm[a]["n"]))
    return pick, "explore"


def choose_next_experiment(
    db,
    channel: Channel,
    presets: Sequence[str],
    *,
    portraits: Sequence[dict] = (),
    thumb_ids: Sequence[str] = AMBIENT_THUMB_VARIANTS,
    explore_rate: float = 0.25,
    rng: Optional[random.Random] = None,
) -> dict:
    """Pick the next (preset, portrait, thumb) experiment using cold-start rotation then UCB/Thompson."""
    summary = evaluate_ambient_ab(db, channel)
    pending = _pending_counts(db, channel)
    r = rng if rng is not None else random.Random()

    chosen_preset, m_preset = _choose_arm(
        presets, summary["factors"]["preset"], pending["preset"], explore_rate=explore_rate, rng=r
    )

    stem_to_path: dict[str, str] = {}
    portrait_stems: list[str] = []
    for p in portraits:
        if not isinstance(p, dict) or not p.get("path"):
            continue
        p_preset = str(p.get("preset") or "")
        if p_preset and p_preset != chosen_preset:
            continue
        stem = Path(str(p["path"])).stem
        if stem and stem not in stem_to_path:
            stem_to_path[stem] = str(p["path"])
            portrait_stems.append(stem)

    if portrait_stems:
        chosen_portrait, m_port = _choose_arm(
            portrait_stems, summary["factors"]["portrait"], pending["portrait"], explore_rate=explore_rate, rng=r
        )
        chosen_portrait_path = stem_to_path[chosen_portrait]
    else:
        chosen_portrait, chosen_portrait_path, m_port = "", "", ""

    chosen_thumb, m_thumb = _choose_arm(
        thumb_ids, summary["factors"]["thumb"], pending["thumb"], explore_rate=explore_rate, rng=r
    )

    modes = [m for m in (m_preset, m_port, m_thumb) if m and m != "empty"]
    mode = "cold_start" if "cold_start" in modes else ("explore" if "explore" in modes else "exploit")
    return {
        "preset": chosen_preset,
        "thumb": chosen_thumb,
        "portrait_path": chosen_portrait_path,
        "portrait": chosen_portrait,
        "mode": mode,
        "summary": summary,
    }
