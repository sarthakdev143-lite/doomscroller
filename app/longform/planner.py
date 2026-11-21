"""Compilation planning: creator discovery, cross-part dedup, energy-curve ordering, cold-open pick, metadata."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import select

from app.clipper import ai, ingest, moments, signals, transcribe
from app.clipper.media import MediaInfo, probe
from app.config import settings
from app.growth import safety
from app.metadata import compose, detect_language
from app.models import Video, VideoStatus
from app.winproc import background_subprocess_kwargs

log = logging.getLogger(__name__)

VALID_FORMATS = ("best_of", "vs", "theme")
_MIN_SOURCE_DURATION_SEC = 180
_SEGMENT_MIN_SEC = 22
_SEGMENT_MAX_SEC = 65
_SEGMENT_TARGET_SEC = 42


@dataclass(frozen=True)
class SegmentPlan:
    source_path: str
    source_url: str
    source_id: str
    creator: str
    creator_name: str
    video_title: str
    start: float
    end: float
    score: float
    moment_title: str
    language: str
    keep_segments: tuple[tuple[float, float], ...] = ()
    cold_open_start: float = 0.0
    cold_open_end: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class CompilationPlan:
    format_kind: str
    channel_slug: str
    part_number: Optional[int]
    targets: tuple[str, ...]
    theme: str
    title: str
    description: str
    tags: tuple[str, ...]
    language: str
    target_seconds: float
    cold_open: SegmentPlan
    segments: tuple[SegmentPlan, ...]
    warnings: tuple[str, ...] = ()


def creator_handle(feed_or_handle: str) -> str:
    """Normalize a YouTube feed URL or handle into a clean display handle (e.g. ``@Creator``)."""
    raw = str(feed_or_handle or "").strip().rstrip("/")
    if not raw:
        return ""
    m = re.search(r"/@([A-Za-z0-9._-]+)", raw)
    if m:
        return f"@{m.group(1)}"
    tail = raw.rsplit("/", 1)[-1].split("?")[0].strip()
    if not tail:
        return ""
    return tail if tail.startswith("@") else f"@{tail}"


def _targets_key(targets: Sequence[str]) -> str:
    cleaned = sorted({creator_handle(t).casefold() or str(t).strip().casefold() for t in targets if str(t).strip()})
    return "|".join(cleaned)


def _is_rehearsed(video: Video) -> bool:
    return video.status == VideoStatus.PUBLISHED and (video.youtube_video_id or "").startswith("MOCK_")


def used_spans_for_channel(db, channel_id: Optional[str] = None) -> dict[str, list[moments.Moment]]:
    """Source video spans already compiled into prior non-failed long-form rows for cross-part dedup."""
    rows = db.execute(select(Video).where(Video.status != VideoStatus.FAILED)).scalars().all()
    used: dict[str, list[moments.Moment]] = {}
    for row in rows:
        meta = row.meta or {}
        if not meta.get("long_form") or _is_rehearsed(row):
            continue
        if channel_id is not None and row.channel_id not in (channel_id, None):
            continue
        for item in meta.get("long_segments") or []:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("source_id") or "").strip()
            try:
                s, e = float(item["start"]), float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if sid and e > s:
                used.setdefault(sid, []).append(moments.Moment(s, e, kind="speech"))
    return used


def next_part_number(db, channel_id: Optional[str], targets: Sequence[str]) -> int:
    """Return next ``Part N`` for ``best_of`` compilations covering the same creator/targets."""
    key = _targets_key(targets)
    if not key:
        return 1
    rows = db.execute(select(Video).where(Video.status != VideoStatus.FAILED)).scalars().all()
    highest = 0
    for row in rows:
        meta = row.meta or {}
        if not meta.get("long_form") or _is_rehearsed(row):
            continue
        if channel_id is not None and row.channel_id not in (channel_id, None):
            continue
        if meta.get("long_format") != "best_of":
            continue
        if str(meta.get("long_targets_key") or "") == key:
            try:
                highest = max(highest, int(meta.get("part_number") or 0))
            except (TypeError, ValueError):
                pass
    return highest + 1


def _ydl_cli_net_args() -> list[str]:
    args: list[str] = []
    opts = ingest.network_options()
    if opts.get("proxy"):
        args += ["--proxy", str(opts["proxy"])]
    if opts.get("cookiefile"):
        args += ["--cookies", str(opts["cookiefile"])]
    elif opts.get("cookiesfrombrowser"):
        spec = opts["cookiesfrombrowser"]
        val = ":".join(str(x) for x in spec if x) if isinstance(spec, tuple) else str(spec)
        if val:
            args += ["--cookies-from-browser", val]
    for rt in (opts.get("js_runtimes") or {}).keys():
        args += ["--js-runtimes", str(rt)]
    for rc in opts.get("remote_components") or ():
        args += ["--remote-components", str(rc)]
    return args


def _blocked_source_titles_and_authors() -> tuple[set[str], set[str]]:
    from app.db import session_scope

    try:
        with session_scope() as db:
            return safety.blocked_parents(db), safety.blocked_authors(db)
    except Exception:
        return set(), set()


def list_creator_videos(feed_url: str, *, limit: int = 30) -> list[dict]:
    """List candidate long source videos (>3 min, non-live, unblocked) via yt-dlp flat playlist."""
    url = str(feed_url or "").strip()
    if not url:
        return []
    target_url = url.rstrip("/")
    if not target_url.endswith(("/videos", "/streams", "/shorts")) and "list=" not in target_url:
        if "/@" in target_url or "/channel/" in target_url or "/c/" in target_url:
            target_url = f"{target_url}/videos"
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist", "--dump-single-json", "--no-warnings", "--ignore-errors",
        "--playlist-items", f"1:{max(1, min(60, int(limit)))}",
        *_ydl_cli_net_args(),
        target_url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **background_subprocess_kwargs(low_priority=True),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("longform.planner: flat listing failed for %s: %s", url, exc)
        return []
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        log.warning("longform.planner: yt-dlp exit %d for %s: %s", proc.returncode, url, (proc.stderr or "")[-240:])
        return []
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return []

    blocked_parents, blocked_authors = _blocked_source_titles_and_authors()
    out: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("id") or "").strip()
        dur = entry.get("duration")
        if entry.get("is_live") or entry.get("live_status") in ("is_live", "is_upcoming"):
            continue
        if not isinstance(dur, (int, float)) or float(dur) <= _MIN_SOURCE_DURATION_SEC:
            continue
        if vid and vid in blocked_parents:
            continue
        author = str(entry.get("uploader_id") or entry.get("channel_id") or entry.get("uploader") or "").strip()
        if author and author.casefold() in blocked_authors:
            continue
        watch_url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
        if watch_url and not watch_url.startswith(("http://", "https://")) and vid:
            watch_url = f"https://www.youtube.com/watch?v={vid}"
        elif not watch_url and vid:
            watch_url = f"https://www.youtube.com/watch?v={vid}"
        if not watch_url:
            continue
        out.append({
            "id": vid or hashlib.sha1(watch_url.encode()).hexdigest()[:12],
            "url": watch_url,
            "title": str(entry.get("title") or "").strip(),
            "duration": float(dur),
            "creator": creator_handle(url) or (author if author.startswith("@") else f"@{author}" if author else ""),
            "view_count": int(entry.get("view_count") or 0),
        })
    return out


def order_energy_curve(segments: Sequence[SegmentPlan], *, format_kind: str = "best_of") -> list[SegmentPlan]:
    """Order segments for high retention: #2 best opens, balanced middle rhythm, #1 climax closes.

    For ``vs``, alternates two creators round-by-round while still closing on the #1 highest-scoring clip.
    """
    items = list(segments)
    if len(items) <= 1:
        return items
    ranked = sorted(items, key=lambda s: s.score, reverse=True)
    if format_kind == "vs":
        by_creator: dict[str, list[SegmentPlan]] = {}
        for seg in ranked:
            by_creator.setdefault(seg.creator.casefold() or "a", []).append(seg)
        creators = sorted(by_creator.keys(), key=lambda k: by_creator[k][0].score, reverse=True)
        if len(creators) >= 2:
            climax = ranked[0]
            climax_key = climax.creator.casefold() or "a"
            for lst in by_creator.values():
                if climax in lst:
                    lst.remove(climax)
                    break
            # Opener should start strong with the second-highest segment (or other creator's top).
            other_keys = [k for k in creators if k != climax_key and by_creator[k]]
            opener_key = other_keys[0] if other_keys else climax_key
            opener = by_creator[opener_key].pop(0) if by_creator[opener_key] else ranked[1]
            ordered: list[SegmentPlan] = [opener]
            turn = 0
            pair = [k for k in creators[:2]]
            while any(by_creator[k] for k in pair):
                k = pair[turn % len(pair)]
                if k == ordered[-1].creator.casefold() and any(by_creator[o] for o in pair if o != k):
                    k = next(o for o in pair if o != k)
                if by_creator[k]:
                    ordered.append(by_creator[k].pop(0))
                turn += 1
            for k in creators[2:]:
                ordered.extend(by_creator[k])
            ordered.append(climax)
            return ordered

    climax = ranked[0]
    opener = ranked[1]
    middle = ranked[2:]
    waved: list[SegmentPlan] = []
    lo, hi = 0, len(middle) - 1
    while lo <= hi:
        waved.append(middle[hi] if len(waved) % 2 == 1 else middle[lo])
        if len(waved) % 2 == 0:
            hi -= 1
        else:
            lo += 1
    return [opener, *waved, climax]


def pick_cold_open(segments: Sequence[SegmentPlan], *, climax: Optional[SegmentPlan] = None) -> SegmentPlan:
    """Pick a crisp 4-6s preview teaser cut from the climax segment's setup just before its payoff."""
    seg = climax or segments[-1]
    span = max(0.5, seg.end - seg.start)
    teaser_len = min(5.5, max(4.0, span * 0.22)) if span >= 8.0 else span
    # Cut right before the final payoff beat so viewers stay to see what happens.
    payoff_margin = min(2.0, max(0.5, span * 0.15)) if span > teaser_len + 1.5 else 0.0
    end = max(seg.start + min(teaser_len, span), seg.end - payoff_margin)
    end = min(seg.end, end)
    start = max(seg.start, end - teaser_len)
    return SegmentPlan(
        source_path=seg.source_path,
        source_url=seg.source_url,
        source_id=seg.source_id,
        creator=seg.creator,
        creator_name=seg.creator_name,
        video_title=seg.video_title,
        start=round(start, 2),
        end=round(end, 2),
        score=seg.score,
        moment_title=seg.moment_title,
        language=seg.language,
        keep_segments=((round(start, 2), round(end, 2)),),
        cold_open_start=round(start, 2),
        cold_open_end=round(end, 2),
    )


def _match_feed(target: str, feeds: Sequence[str]) -> Optional[str]:
    clean = str(target or "").strip()
    if not clean:
        return None
    if clean.startswith(("http://", "https://")) or os.path.exists(clean):
        return clean
    want = creator_handle(clean).casefold()
    for feed in feeds:
        if creator_handle(feed).casefold() == want:
            return feed
    handle = clean if clean.startswith("@") else f"@{clean}"
    return f"https://www.youtube.com/{handle}"


def _pick_source_candidates(
    *,
    format_kind: str,
    targets: Sequence[str],
    feeds: Sequence[str],
) -> tuple[list[str], list[dict]]:
    """Resolve normalized creator labels and ordered source entries per format."""
    if format_kind == "vs":
        resolved_targets = list(targets[:2])
        if len(resolved_targets) < 2:
            for f in feeds:
                h = creator_handle(f)
                if h and all(creator_handle(t).casefold() != h.casefold() for t in resolved_targets):
                    resolved_targets.append(h)
                if len(resolved_targets) >= 2:
                    break
        per_creator: list[list[dict]] = []
        for t in resolved_targets[:2]:
            matched = _match_feed(t, feeds)
            if not matched:
                continue
            if os.path.isfile(matched):
                per_creator.append([{"id": Path(matched).stem, "url": matched, "title": Path(matched).stem, "creator": creator_handle(t) or "@Creator"}])
            else:
                per_creator.append(list_creator_videos(matched, limit=18))
        # Interleave creators so both get fair candidate exposure.
        combined: list[dict] = []
        max_len = max((len(p) for p in per_creator), default=0)
        for i in range(max_len):
            for lst in per_creator:
                if i < len(lst):
                    combined.append(lst[i])
        labels = [creator_handle(t) or t for t in resolved_targets[:2]]
        return labels, combined

    if format_kind == "theme":
        feed_pool = list(targets) if targets else list(feeds[:6])
        labels = [creator_handle(f) or f for f in feed_pool]
        per_feed = []
        for f in feed_pool:
            matched = _match_feed(f, feeds) or f
            if os.path.isfile(matched):
                per_feed.append([{"id": Path(matched).stem, "url": matched, "title": Path(matched).stem, "creator": creator_handle(f) or "@Creator"}])
            else:
                per_feed.append(list_creator_videos(matched, limit=8))
        combined = []
        max_len = max((len(p) for p in per_feed), default=0)
        for i in range(max_len):
            for lst in per_feed:
                if i < len(lst):
                    combined.append(lst[i])
        return labels, combined

    # best_of
    chosen = list(targets[:1]) or ([creator_handle(feeds[0])] if feeds else [])
    matched = _match_feed(chosen[0], feeds) if chosen else None
    if not matched:
        return [], []
    if os.path.isfile(matched):
        return [creator_handle(chosen[0]) or "@Creator"], [
            {"id": Path(matched).stem, "url": matched, "title": Path(matched).stem, "creator": creator_handle(chosen[0]) or "@Creator"}
        ]
    return [creator_handle(matched) or matched], list_creator_videos(matched, limit=25)


def _moments_from_source(
    src: ingest.SourceVideo,
    creator: str,
    existing_used: Sequence[moments.Moment],
    max_needed: int = 4,
) -> list[SegmentPlan]:
    """Probe, analyse, transcribe (reusing cache), and extract distinct non-overlapping highlights."""
    info: MediaInfo = probe(src.path)
    if not info.duration or info.duration <= 60:
        return []
    sig = signals.analyse(src.path, info)
    transcript = None
    try:
        transcript = transcribe.transcribe(src.path, info)
    except Exception as exc:
        log.warning("longform.planner: transcription skipped for %s: %s", src.source_id, exc)
    speech = transcript is not None and (transcript.is_english_speech or transcript.is_hindi_speech)
    words = [w for w in transcript.words if w.end <= info.duration] if speech else []
    lang = ("hi" if transcript.is_hindi_speech else "en") if speech else detect_language(f"{src.title} {src.description}")

    if speech and words:
        picked = moments.pick_speech(
            words,
            sig,
            count=max_needed,
            title=src.title,
            used=list(existing_used),
            min_sec=_SEGMENT_MIN_SEC,
            max_sec=_SEGMENT_MAX_SEC,
            target_sec=_SEGMENT_TARGET_SEC,
        )
    else:
        picked = moments.pick_visual(
            src.path,
            info,
            sig,
            count=max_needed,
            title=src.title,
            used=list(existing_used),
        )
    plans: list[SegmentPlan] = []
    for m in picked:
        if any(moments.clashes(m, u) for u in existing_used):
            continue
        plans.append(SegmentPlan(
            source_path=src.path,
            source_url=src.url,
            source_id=src.source_id,
            creator=creator or (src.author if str(src.author).startswith("@") else f"@{src.author}" if src.author else "@Creator"),
            creator_name=src.author_name or creator,
            video_title=src.title or "",
            start=round(m.start, 2),
            end=round(m.end, 2),
            score=float(m.score or 0.0),
            moment_title=m.title or src.title or "Highlight",
            language=lang or "en",
        ))
    return plans


def _fallback_title_and_desc(
    *,
    format_kind: str,
    targets: Sequence[str],
    theme: str,
    part_number: Optional[int],
    niche: str,
    language: str,
) -> tuple[str, str, list[str]]:
    clean_targets = [t for t in targets if t]
    if format_kind == "vs" and len(clean_targets) >= 2:
        title = f"{clean_targets[0]} vs {clean_targets[1]} — Biggest Moments"
        desc = f"Round-by-round showdown between {clean_targets[0]} and {clean_targets[1]}."
        tags = ["compilation", clean_targets[0].lstrip("@"), clean_targets[1].lstrip("@"), "vs"]
    elif format_kind == "theme":
        label = theme.strip() or f"{(niche or 'Top Creators').title()} Highlights"
        title = f"{label} — Best Moments"
        desc = f"{label}: curated compilation of unscripted creator moments."
        tags = ["compilation", label.lower()[:30], "highlights"]
    else:
        who = clean_targets[0] if clean_targets else (niche or "Creator")
        part_txt = f" Part {part_number}" if part_number and part_number > 1 else ""
        title = f"Best of {who}{part_txt}"
        desc = f"The funniest and biggest moments from {who}{part_txt}."
        tags = ["compilation", who.lstrip("@"), "best moments"]
    if language == "hi":
        tags.append("hindi vlog")
    return title, desc, tags


def _ai_long_metadata(
    *,
    format_kind: str,
    targets: Sequence[str],
    theme: str,
    part_number: Optional[int],
    niche: str,
    language: str,
    segments: Sequence[SegmentPlan],
) -> tuple[str, str, list[str]]:
    fallback_title, fallback_desc, fallback_tags = _fallback_title_and_desc(
        format_kind=format_kind, targets=targets, theme=theme,
        part_number=part_number, niche=niche, language=language,
    )
    if not ai.available():
        return fallback_title, fallback_desc, fallback_tags
    lang_hint = "Hinglish (Hindi in Roman script)" if language == "hi" else "natural English"
    seg_lines = "\n".join(f"- {s.creator}: {s.moment_title} ({s.duration:.0f}s)" for s in segments[:12])
    prompt = (
        f"Write YouTube long-form compilation metadata in {lang_hint}.\n"
        f"Format: {format_kind}; Creators: {', '.join(targets)}; Theme: {theme or niche}; Part: {part_number or 1}.\n"
        f"Segments included:\n{seg_lines}\n\n"
        "Rules: NO clickbait lies, NO hashtags (#Shorts) anywhere in title, <= 75 chars crisp intriguing title.\n"
        'Reply with JSON only: {"title": "...", "description": "1-2 short teaser sentences", "tags": ["tag1", ...]}'
    )
    reply = ai.ask_json(prompt)
    if not isinstance(reply, dict):
        return fallback_title, fallback_desc, fallback_tags
    title = str(reply.get("title") or "").strip() or fallback_title
    desc = str(reply.get("description") or "").strip() or fallback_desc
    raw_tags = reply.get("tags")
    tags = [str(t).strip() for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else fallback_tags
    return title, desc, tags or fallback_tags


def _cached_media_path(video_id: str) -> Optional[Path]:
    """Return an already-downloaded full video in content/long matching ``video_id`` if usable."""
    vid = re.sub(r"[^A-Za-z0-9._-]+", "", str(video_id or "").strip())
    if not vid:
        return None
    long_dir = settings.content_path / "long"
    if not long_dir.is_dir():
        return None
    direct = long_dir / f"Youtube_{vid}.mp4"
    candidates = [direct] if direct.is_file() else []
    if not candidates:
        candidates = sorted(
            p for p in long_dir.glob(f"*{vid}.mp4")
            if p.is_file() and not p.name.endswith((".tmp.mp4", ".part.mp4"))
        )
    for path in candidates:
        try:
            if path.stat().st_size > 50_000:
                return path
        except OSError:
            continue
    return None


def _cached_source_for_entry(entry: dict) -> Optional[ingest.SourceVideo]:
    """Build a local SourceVideo directly from cached content/long/ without invoking yt-dlp."""
    vid = str(entry.get("id") or "").strip()
    path = _cached_media_path(vid)
    if path is None:
        return None
    try:
        os.utime(path, None)
    except OSError:
        pass
    creator = str(entry.get("creator") or "").strip()
    return ingest.SourceVideo(
        path=str(path),
        title=str(entry.get("title") or "").strip() or path.stem,
        author=creator,
        author_name=creator or path.stem,
        url=str(entry.get("url") or f"https://www.youtube.com/watch?v={vid}"),
        source="youtube",
        source_id=vid,
    )


def _entry_cache_tier(entry: dict, used_map: dict[str, list[moments.Moment]]) -> int:
    vid = str(entry.get("id") or "").strip()
    if not vid or len(used_map.get(vid, ())) >= 3:
        return 2
    media = _cached_media_path(vid)
    if media is None:
        return 2
    transcript_sidecar = Path(f"{media}.transcript.json")
    return 0 if transcript_sidecar.is_file() else 1


def _prioritize_cached_entries(
    entries: Sequence[dict],
    used_map: dict[str, list[moments.Moment]],
    *,
    format_kind: str = "best_of",
) -> list[dict]:
    """Prefer already-cached videos & transcripts to cut compilation build time on low-spec CPUs."""
    items = list(entries)
    if len(items) <= 1:
        return items
    if format_kind == "vs":
        buckets: dict[str, list[dict]] = {}
        for item in items:
            key = str(item.get("creator") or "").casefold()
            buckets.setdefault(key, []).append(item)
        sorted_buckets = [
            sorted(group, key=lambda e: _entry_cache_tier(e, used_map))
            for group in buckets.values()
        ]
        out: list[dict] = []
        max_len = max((len(b) for b in sorted_buckets), default=0)
        for i in range(max_len):
            for b in sorted_buckets:
                if i < len(b):
                    out.append(b[i])
        return out
    return sorted(items, key=lambda e: _entry_cache_tier(e, used_map))


def plan_compilation(
    *,
    format_kind: str = "best_of",
    channel_slug: str = "default",
    targets: Sequence[str] = (),
    theme: str = "",
    feeds: Sequence[str] = (),
    target_minutes: int = 8,
    db=None,
    channel_id: Optional[str] = None,
    niche: str = "",
    min_total_sec: float = 120.0,
) -> CompilationPlan:
    """Discover sources, pick non-overlapping segments, order energy curve & prepare compilation plan."""
    fmt = str(format_kind or "best_of").strip().lower()
    if fmt not in VALID_FORMATS:
        raise ValueError(f"unsupported long-form format {format_kind!r}; expected one of {VALID_FORMATS}")
    mins = max(6, min(10, int(target_minutes or 8)))
    target_sec = float(mins * 60)

    if db is not None:
        used_map = used_spans_for_channel(db, channel_id)
    else:
        from app.db import session_scope

        with session_scope() as s:
            used_map = used_spans_for_channel(s, channel_id)

    labels, entries = _pick_source_candidates(format_kind=fmt, targets=targets, feeds=feeds)
    if not entries:
        raise ValueError("no eligible source videos found (>3 min) for compilation")

    if db is not None:
        part_no = next_part_number(db, channel_id, labels) if fmt == "best_of" else None
    else:
        from app.db import session_scope

        with session_scope() as s:
            part_no = next_part_number(s, channel_id, labels) if fmt == "best_of" else None

    warnings: list[str] = []
    collected: list[SegmentPlan] = []
    total_sec = 0.0
    fetched_ids: set[str] = set()
    # Keep track of spans picked within this run so no moment repeats.
    session_used: dict[str, list[moments.Moment]] = {k: list(v) for k, v in used_map.items()}
    ordered_entries = _prioritize_cached_entries(entries, used_map, format_kind=fmt)

    for entry in ordered_entries:
        if total_sec >= target_sec:
            break
        try:
            src = _cached_source_for_entry(entry) or ingest.fetch(entry["url"])
        except Exception as exc:
            warnings.append(f"skipped {entry.get('url', '')}: {exc}")
            continue
        if src.source_id in fetched_ids:
            continue
        fetched_ids.add(src.source_id)
        already = session_used.get(src.source_id, [])
        segs = _moments_from_source(src, entry.get("creator") or creator_handle(src.author), already, max_needed=4)
        for seg in segs:
            collected.append(seg)
            session_used.setdefault(seg.source_id, []).append(moments.Moment(seg.start, seg.end, kind="speech"))
            total_sec += seg.duration
            if total_sec >= target_sec:
                break

    if not collected or total_sec < min_total_sec:
        raise ValueError(f"insufficient highlight footage ({total_sec:.0f}s collected < {min_total_sec:.0f}s minimum)")

    ordered = order_energy_curve(collected, format_kind=fmt)
    cold_open = pick_cold_open(ordered)
    langs = [s.language for s in ordered if s.language]
    dominant_lang = max(set(langs), key=langs.count) if langs else "en"

    raw_title, raw_desc, raw_tags = _ai_long_metadata(
        format_kind=fmt, targets=labels, theme=theme, part_number=part_no,
        niche=niche, language=dominant_lang, segments=ordered,
    )
    composed = compose(
        title=raw_title,
        description=raw_desc,
        keywords=raw_tags,
        language=dominant_lang,
        source="longform",
        is_short=False,
    )
    return CompilationPlan(
        format_kind=fmt,
        channel_slug=channel_slug,
        part_number=part_no,
        targets=tuple(labels),
        theme=theme.strip(),
        title=composed.title,
        description=composed.description,
        tags=tuple(composed.tags),
        language=composed.detected_language or dominant_lang,
        target_seconds=target_sec,
        cold_open=cold_open,
        segments=tuple(ordered),
        warnings=tuple(warnings),
    )
