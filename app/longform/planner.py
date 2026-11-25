"""Compilation planning: creator discovery, cross-part dedup, energy-curve ordering, cold-open pick, metadata."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
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
_SEGMENT_MIN_SEC = 35
_SEGMENT_MAX_SEC = 70
_SEGMENT_TARGET_SEC = 42
_MONETIZATION_UNSAFE_RE = re.compile(
    r"\b(?:sperm|semen|porn|nsfw|nude|nudity|sex|sexual|suicide|self[- ]harm|kill yourself|gore|cocaine|meth|heroin)\b",
    re.IGNORECASE,
)
_LOW_VALUE_FILLER_RE = re.compile(
    r"(?:वॉशरूम|बाथरूम|टॉयलेट|कपड़े\s*(?:सुख|डाल)|सामान\s*पैक|पैकिंग|चेक\s*इन|"
    r"washroom|bathroom|toilet|laundry|drying\s+clothes|unpacking|packing\s+(?:bags|luggage))",
    re.IGNORECASE,
)
_HINGLISH_CUE_RE = re.compile(
    r"[\u0900-\u097F]|\b(?:ka|ki|ke|ko|se|me|mein|par|pe|aur|ya|hai|hain|tha|thi|the|hua|hui|gaya|gayi|"
    r"aaya|aayi|kya|kyun|kaise|sab|bahut|bada|badi|chota|darr|maza|mazedaar|raat|subah|din|bhai|dost|"
    r"safar|pahad|pahadon|paani|khana|ghar|gaadi|log|dekho|dekha|mil|wala|wali|wale|tak|bhi|nahi|mat|ab|phir|"
    r"pehle|aakhri|khatarnak|dhamaka|masti|kahani|khaas|pal|toh|ne|yaar|khel|bhasad|jugaad|asli|ulta|"
    r"naya|nayi|naye|mera|meri|mere|hamara|hamari|apna|apni|apne|aaj|ek|kuch|koi|chalo|karo|kiya|saath|sath|"
    r"wapas|ekdum|bilkul|pakka|jaldi|tez|ghoom|ghum|shadi|piyush|sourav)\b",
    re.IGNORECASE,
)
_CREATOR_DISPLAY_MAP = {
    "souravjvlogs": "Sourav Joshi",
    "lakhneetvlogs": "Lakhneet",
    "mrbeast": "MrBeast",
    "ksi": "KSI",
    "fukrainsaan": "Fukra Insaan",
    "triggeredinsaan": "Triggered Insaan",
    "flyingbeast320": "Flying Beast",
    "elvishyadavvlogs": "Elvish Yadav",
    "mrindianhacker": "MR. INDIAN HACKER",
    "crazyxyz": "Crazy XYZ",
    "rug": "FaZe Rug",
    "staywild-": "Stay Wild",
    "dimplemalhanvlogs": "Dimple Malhan",
    "sonehavlogs": "Soneha",
    "anantrastogi": "Anant Rastogi",
    "sahiljoshivlogs7376": "Sahil Joshi",
    "albadivlogs": "Albadi",
    "ishowspeed": "IShowSpeed",
    "sidemen": "Sidemen",
    "loganpaulvlogs": "Logan Paul",
    "jidion": "JiDion",
    "airrack": "Airrack",
    "ryantrahan": "Ryan Trahan",
    "betasquad": "Beta Squad",
    "rebeccazamolo": "Rebecca Zamolo",
}


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
    transcript_excerpt: str = ""
    reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def display_creator_name(creator: str) -> str:
    """Clean display name for on-screen chapter badges (without ugly raw handles)."""
    raw = str(creator or "").strip().lstrip("@")
    if not raw:
        return "Creator"
    return _CREATOR_DISPLAY_MAP.get(raw.casefold(), raw)


def _clean_handles(text: str) -> str:
    return re.sub(r"@([A-Za-z0-9._-]+)", lambda m: display_creator_name(m.group(1)), str(text or ""))


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
    """Source video spans already compiled into prior active/published long-form rows for cross-part dedup."""
    rows = db.execute(
        select(Video).where(Video.status.notin_((VideoStatus.FAILED, VideoStatus.REJECTED)))
    ).scalars().all()
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
    rows = db.execute(
        select(Video).where(Video.status.notin_((VideoStatus.FAILED, VideoStatus.REJECTED)))
    ).scalars().all()
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
    """Pick a crisp 4.5-5.8s live-action teaser cut from the climax segment just before its payoff."""
    seg = climax or segments[-1]
    span = max(0.5, seg.end - seg.start)
    teaser_len = min(5.8, max(4.5, span * 0.16)) if span >= 8.0 else span
    # Never start at a 0:00 dark fade-in when enough room exists later in the climax act.
    floor_start = max(seg.start, 4.0) if (seg.end - max(seg.start, 4.0)) >= teaser_len + 2.0 else seg.start
    ideal_end = max(floor_start + teaser_len, seg.start + span * 0.65)
    max_end = (seg.end - min(3.0, span * 0.15)) if span > teaser_len + 2.5 else seg.end
    end = min(seg.end, max(floor_start + teaser_len, min(ideal_end, max_end)))
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
        transcript_excerpt=seg.transcript_excerpt,
        reason=seg.reason,
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


def _is_monetization_safe(seg: SegmentPlan) -> bool:
    """Reject demonetizable/explicit or owner-blocked topics before entering any compilation."""
    text = f"{seg.moment_title} {seg.reason} {seg.transcript_excerpt}".casefold()
    if _MONETIZATION_UNSAFE_RE.search(text):
        return False
    for blocked in (*settings.excluded_topics_list, *settings.blocked_title_words_list):
        if blocked and blocked in text:
            return False
    return True


def _is_low_value_filler(seg: SegmentPlan) -> bool:
    """Reject mundane hotel-washroom/laundry/packing filler (>=2 transcript cues or direct title match)."""
    if _LOW_VALUE_FILLER_RE.search(seg.moment_title or ""):
        return True
    text = f"{seg.moment_title} {seg.reason} {seg.transcript_excerpt}"
    return len(_LOW_VALUE_FILLER_RE.findall(text)) >= 2


def _moments_from_source(
    src: ingest.SourceVideo,
    creator: str,
    existing_used: Sequence[moments.Moment],
    max_needed: int = 4,
) -> list[SegmentPlan]:
    """Probe, analyse, transcribe (reusing cache), ban vlog intros/outros & extract clean story acts."""
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

    exclusions = list(existing_used)
    if info.duration >= 240.0:
        # Ban opening vlog greetings / fade-in black frames and closing sponsor/subscribe outros.
        exclusions.append(moments.Moment(0.0, 45.0, kind="intro"))
        exclusions.append(moments.Moment(max(45.0, info.duration - 25.0), info.duration, kind="outro"))

    if speech and words:
        picked = moments.pick_speech(
            words,
            sig,
            count=max_needed,
            title=src.title,
            used=exclusions,
            min_sec=_SEGMENT_MIN_SEC,
            max_sec=_SEGMENT_MAX_SEC,
            target_sec=_SEGMENT_TARGET_SEC,
        )
        if not picked:
            picked = moments.pick_speech(
                words,
                sig,
                count=max_needed,
                title=src.title,
                used=exclusions,
                min_sec=22,
                max_sec=65,
                target_sec=42,
            )
    else:
        picked = moments.pick_visual(
            src.path,
            info,
            sig,
            count=max_needed,
            title=src.title,
            used=exclusions,
        )

    plans: list[SegmentPlan] = []
    for m in picked:
        if any(moments.clashes(m, u) for u in existing_used):
            continue
        if words:
            full_text = " ".join(w.text for w in words if m.start <= w.start <= m.end).strip()
            excerpt = full_text if len(full_text) <= 600 else f"{full_text[:340].rstrip()} ... {full_text[-240:].lstrip()}"
        else:
            excerpt = ""
        seg = SegmentPlan(
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
            transcript_excerpt=excerpt,
            reason=str(getattr(m, "reason", "") or ""),
        )
        if _is_monetization_safe(seg) and not _is_low_value_filler(seg):
            plans.append(seg)
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
        left, right = display_creator_name(clean_targets[0]), display_creator_name(clean_targets[1])
        title = f"{left} vs {right} — Biggest Moments"
        desc = f"Round-by-round showdown between {left} and {right}."
        tags = ["compilation", clean_targets[0].lstrip("@"), clean_targets[1].lstrip("@"), "vs"]
    elif format_kind == "theme":
        label = theme.strip() or f"{(niche or 'Top Creators').title()} Highlights"
        title = f"{label} — Best Moments"
        desc = f"{label}: curated compilation of unscripted creator moments."
        tags = ["compilation", label.lower()[:30], "highlights"]
    else:
        who = clean_targets[0] if clean_targets else (niche or "Creator")
        cname = display_creator_name(who)
        part_txt = f" Part {part_number}" if part_number and part_number > 1 else ""
        if language == "hi":
            title = f"{cname} Ke Best Moments{part_txt}"
            desc = f"{cname} ke sabse mazedaar aur khatarnak moments{part_txt}."
        else:
            title = f"Best of {cname}{part_txt}"
            desc = f"The funniest and biggest moments from {cname}{part_txt}."
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
    display_targets = [display_creator_name(t) for t in targets if t]
    seg_lines = "\n".join(f"- {display_creator_name(s.creator)}: {s.moment_title} ({s.duration:.0f}s)" for s in segments[:12])
    prompt = (
        f"Write YouTube long-form compilation metadata in {lang_hint}.\n"
        f"Format: {format_kind}; Creators: {', '.join(display_targets)}; Theme: {theme or niche}; Part: {part_number or 1}.\n"
        f"Segments included:\n{seg_lines}\n\n"
        "Rules: NO clickbait lies, NO hashtags (#Shorts) anywhere in title, <= 75 chars crisp intriguing title. Use natural creator names (never raw @handles).\n"
        'Reply with JSON only: {"title": "...", "description": "1-2 short teaser sentences", "tags": ["tag1", ...]}'
    )
    reply = ai.ask_json(prompt)
    if not isinstance(reply, dict):
        return fallback_title, fallback_desc, fallback_tags
    raw_title = str(reply.get("title") or "").strip()
    title = _clean_handles(raw_title) if raw_title else fallback_title
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
    if not vid:
        return 2
    media = _cached_media_path(vid)
    if media is None:
        return 2
    if len(used_map.get(vid, ())) >= 3:
        return 1
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


def _clean_chapter_title(raw: str, fallback: str) -> str:
    clean = " ".join(str(raw or "").replace("#Shorts", "").replace("#shorts", "").replace("...", "").split())
    clean = clean.strip(" -|:,—\"'")
    if not clean or len(clean) < 3:
        return fallback
    if len(clean) <= 36:
        return clean
    clipped = clean[:36].rsplit(" ", 1)[0].rstrip(" -|:,—")
    return clipped or clean[:36]


def _ensure_hinglish_title(title: str, language: str) -> str:
    if not str(language or "").strip().lower().startswith("hi"):
        return title
    clean = _clean_chapter_title(title, "Khaas Pal")
    if _HINGLISH_CUE_RE.search(clean):
        return clean
    suffix = " Ka Scene"
    max_base = 36 - len(suffix)
    base = clean if len(clean) <= max_base else (clean[:max_base].rsplit(" ", 1)[0].rstrip(" -|:,—") or clean[:max_base])
    return f"{base}{suffix}".strip()


def _enforce_source_chronology(ordered: Sequence[SegmentPlan]) -> list[SegmentPlan]:
    """Keep acts originating from the same source video in chronological start order."""
    out = list(ordered)
    by_src_positions: dict[str, list[int]] = {}
    for pos, seg in enumerate(out):
        by_src_positions.setdefault(seg.source_id, []).append(pos)
    for positions in by_src_positions.values():
        if len(positions) <= 1:
            continue
        sorted_segs = sorted((out[p] for p in positions), key=lambda s: (s.start, s.end))
        for pos, seg in zip(positions, sorted_segs):
            out[pos] = seg
    return out


def _curate_story_arc(
    candidates: Sequence[SegmentPlan],
    *,
    format_kind: str,
    targets: Sequence[str],
    theme: str,
    language: str,
    target_sec: float,
    min_total_sec: float,
) -> list[SegmentPlan]:
    """Use AI Story Director to curate a cohesive narrative arc sized to target_sec (or ordered fallback)."""
    safe = (
        [s for s in candidates if _is_monetization_safe(s) and not _is_low_value_filler(s)]
        or [s for s in candidates if _is_monetization_safe(s)]
        or list(candidates)
    )
    if len(safe) < 3 or not ai.available():
        ordered = _enforce_source_chronology(order_energy_curve(safe, format_kind=format_kind))
        return [replace(s, moment_title=_ensure_hinglish_title(s.moment_title, language)) for s in ordered]

    max_acts = max(6, min(10, int(round(target_sec / 52.0))))
    min_acts = max(4, max_acts - 3)
    rows = []
    for idx, s in enumerate(safe):
        speech = " ".join(str(s.transcript_excerpt or "").split())[:320]
        rows.append(
            f"[{idx}] src={s.source_id} | {s.start:.0f}..{s.end:.0f}s ({s.duration:.0f}s) | score={s.score:.0f} | "
            f"{s.creator} | {s.moment_title}" + (f" | said={speech}" if speech else "")
        )
    hi = str(language or "").strip().lower().startswith("hi")
    lang_hint = (
        "natural conversational Hinglish (Hindi words in Roman script, e.g. 'Pahadon Mein Train Cafe', 'Glass Bridge Par Darr')"
        if hi else "crisp English"
    )
    hi_rule = (
        "- EVERY chapter title MUST be clean natural conversational Hinglish (NEVER pure English like 'Mountain Magic', NEVER awkward typos or weird slang like 'Maa-Peet').\n"
        if hi else ""
    )
    vs_rule = (
        "- This is a VS video: pick a balanced mix including at least 2 acts from EACH distinct creator available in the list.\n"
        if format_kind == "vs" else ""
    )
    prompt = (
        f"You are directing a high-retention YouTube 16:9 {format_kind} story video (target ~{int(target_sec // 60)} min).\n"
        f"Creators: {', '.join(targets) or 'creator'}; Theme: {theme or format_kind}.\n"
        f"Candidate moments:\n" + "\n".join(rows) + "\n\n"
        f"Pick {min_acts} to {max_acts} coherent acts (never mundane hotel/washroom/laundry/packing filler) that tell a rewarding story totaling roughly "
        f"{int(target_sec - 15)}-{int(target_sec + 45)} seconds.\n"
        "Rules:\n"
        "- Strong engaging opener first; BIGGEST climax last.\n"
        "- Multiple acts from the same `src` MUST appear in ascending start-time order.\n"
        f"{vs_rule}"
        f"{hi_rule}"
        f"- Give each picked index a complete, punchy chapter title in {lang_hint} (3-6 words, max 36 chars, NO ellipsis, NO #Shorts).\n"
        'Reply with JSON only: {"indices": [0, ...], "titles": {"0": "..."}, "theme_hook": "..."}'
    )
    reply = ai.ask_json(prompt)
    if not isinstance(reply, dict):
        ordered = _enforce_source_chronology(order_energy_curve(safe, format_kind=format_kind))
        return [replace(s, moment_title=_ensure_hinglish_title(s.moment_title, language)) for s in ordered]

    raw_indices = reply.get("indices")
    raw_titles = reply.get("titles") if isinstance(reply.get("titles"), dict) else {}
    picked: list[SegmentPlan] = []
    seen: set[int] = set()
    if isinstance(raw_indices, list):
        for item in raw_indices:
            try:
                idx = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(safe) and idx not in seen and len(picked) < max_acts:
                seen.add(idx)
                seg = safe[idx]
                custom = raw_titles.get(str(idx)) or raw_titles.get(idx)
                cleaned = _clean_chapter_title(str(custom), seg.moment_title) if custom else seg.moment_title
                picked.append(replace(seg, moment_title=_ensure_hinglish_title(cleaned, language)))

    # Guarantee multi-creator representation on `vs` whenever safe pool contains >=2 distinct creators.
    if format_kind == "vs" and picked:
        safe_creators: list[str] = list(dict.fromkeys(
            creator_handle(s.creator).casefold() or s.creator.casefold()
            for s in safe if (s.creator or "").strip()
        ))
        if len(safe_creators) >= 2:
            for ck in safe_creators[:2]:
                have = [s for s in picked if (creator_handle(s.creator).casefold() or s.creator.casefold()) == ck]
                missing_pool = [
                    (idx, s) for idx, s in enumerate(safe)
                    if idx not in seen and (creator_handle(s.creator).casefold() or s.creator.casefold()) == ck
                ]
                while len(have) < 2 and missing_pool:
                    idx_add, seg_add = max(missing_pool, key=lambda pair: pair[1].score)
                    missing_pool.remove((idx_add, seg_add))
                    seen.add(idx_add)
                    norm_add = replace(seg_add, moment_title=_ensure_hinglish_title(seg_add.moment_title, language))
                    if len(picked) >= max_acts:
                        # Drop lowest-scored item from a more-represented creator so duration stays bounded.
                        counts_by_c = {}
                        for p in picked:
                            pk = creator_handle(p.creator).casefold() or p.creator.casefold()
                            counts_by_c[pk] = counts_by_c.get(pk, 0) + 1
                        swappable = [p for p in picked if counts_by_c.get(creator_handle(p.creator).casefold() or p.creator.casefold(), 0) > 2]
                        if swappable:
                            picked.remove(min(swappable, key=lambda p: p.score))
                    picked.append(norm_add)
                    have.append(norm_add)

    # Top up from remaining safe candidates so 8-10 min builds never fall short.
    avail_sec = sum(s.duration for s in safe)
    desired_min = min(avail_sec, max(min_total_sec, target_sec * 0.94))
    cur_dur = sum(s.duration for s in picked)
    if cur_dur < desired_min:
        for idx, seg in enumerate(safe):
            if idx not in seen:
                seen.add(idx)
                picked.append(replace(seg, moment_title=_ensure_hinglish_title(seg.moment_title, language)))
                cur_dur += seg.duration
                if cur_dur >= desired_min and len(picked) >= 4:
                    break

    if not picked or sum(s.duration for s in picked) < min_total_sec:
        ordered = _enforce_source_chronology(order_energy_curve(safe, format_kind=format_kind))
        return [replace(s, moment_title=_ensure_hinglish_title(s.moment_title, language)) for s in ordered]
    if format_kind == "vs" and len({creator_handle(s.creator).casefold() for s in picked if s.creator}) >= 2:
        picked = order_energy_curve(picked, format_kind="vs")
    return _enforce_source_chronology(picked)


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
    """Discover sources, curate coherent 5-6 act arc, order chronology & prepare compilation plan."""
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
    # Collect a 1.55x candidate pool when AI Story Director curates acts; avoid extra uncached fetches once target_sec is filled.
    pool_target = max(target_sec * 1.55, target_sec + 180.0) if ai.available() else target_sec
    pool_cap = 16
    session_used: dict[str, list[moments.Moment]] = {k: list(v) for k, v in used_map.items()}
    ordered_entries = _prioritize_cached_entries(entries, used_map, format_kind=fmt)

    for entry in ordered_entries:
        if total_sec >= pool_target or len(collected) >= pool_cap:
            break
        cached_src = _cached_source_for_entry(entry)
        if cached_src is None and total_sec >= target_sec:
            break
        try:
            src = cached_src or ingest.fetch(entry["url"])
        except Exception as exc:
            warnings.append(f"skipped {entry.get('url', '')}: {exc}")
            continue
        if src.source_id in fetched_ids:
            continue
        fetched_ids.add(src.source_id)
        already = session_used.get(src.source_id, [])
        max_per_src = 4 if fmt == "vs" else 5
        segs = _moments_from_source(src, entry.get("creator") or creator_handle(src.author), already, max_needed=max_per_src)
        for seg in segs:
            collected.append(seg)
            session_used.setdefault(seg.source_id, []).append(moments.Moment(seg.start, seg.end, kind="speech"))
            total_sec += seg.duration
            if total_sec >= pool_target or len(collected) >= pool_cap:
                break

    if not collected or total_sec < min_total_sec:
        raise ValueError(f"insufficient highlight footage ({total_sec:.0f}s collected < {min_total_sec:.0f}s minimum)")

    langs = [s.language for s in collected if s.language]
    dominant_lang = max(set(langs), key=langs.count) if langs else "en"
    ordered = _curate_story_arc(
        collected,
        format_kind=fmt,
        targets=labels,
        theme=theme,
        language=dominant_lang,
        target_sec=target_sec,
        min_total_sec=min_total_sec,
    )
    actual_creators = list(dict.fromkeys(
        creator_handle(s.creator) or s.creator
        for s in ordered
        if str(s.creator or "").strip()
    ))
    if fmt == "vs" and len(actual_creators) < 2:
        fmt = "best_of"
        labels = actual_creators[:1] or labels[:1]
        if db is not None:
            part_no = next_part_number(db, channel_id, labels)
        else:
            from app.db import session_scope

            with session_scope() as s:
                part_no = next_part_number(s, channel_id, labels)
    climax = max(ordered, key=lambda s: s.score)
    cold_open = pick_cold_open(ordered, climax=climax)

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
