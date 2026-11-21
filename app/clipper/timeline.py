"""Mapping between the source video's clock and a tightened clip's clock.

Removing pauses is most of what makes a clip feel tight, and it means captions,
framing and zooms all need to convert from source time to output time.
Boundaries sit on a frame grid so video and audio cut at the same instant.
"""
from __future__ import annotations

import logging
import math
import os
import re
import subprocess
from typing import Optional, Sequence

from app.winproc import background_subprocess_kwargs

log = logging.getLogger(__name__)
_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)")

FPS = 30
Segment = tuple[float, float]

LEAD_IN_SEC = 0.06
TAIL_SEC = 0.10
_MIN_SEGMENT_SEC = 0.25
# Only pauses this short may be kept in order to join a tiny island to its neighbour.
_MAX_JOINED_GAP_SEC = 0.9


def on_grid(seconds: float) -> float:
    if not math.isfinite(seconds) or seconds <= 0:
        return 0.0
    return round(seconds * FPS) / FPS


def keep_segments(start: float, end: float, words: Sequence, max_gap: float) -> list[Segment]:
    """The parts of [start, end] to keep: every word, minus pauses over ``max_gap``."""
    if not math.isfinite(start) or not math.isfinite(end):
        return []
    start = max(0.0, start)
    if on_grid(end) <= on_grid(start):
        return []
    safe_gap = max(0.0, max_gap) if math.isfinite(max_gap) else 0.0
    inside = [
        w for w in words
        if math.isfinite(w.start) and math.isfinite(w.end) and w.end > start and w.start < end
    ]
    if not inside:
        return [(on_grid(start), on_grid(end))]
    segments: list[list[float]] = []
    for word in inside:
        begin, finish = max(start, word.start - LEAD_IN_SEC), min(end, word.end + TAIL_SEC)
        if segments and begin - segments[-1][1] <= safe_gap:
            segments[-1][1] = max(segments[-1][1], finish)
        else:
            segments.append([begin, finish])
    # A tiny island between two cuts reads as a glitch, so join it to its neighbour.
    merged: list[list[float]] = []
    for begin, finish in segments:
        tiny = finish - begin < _MIN_SEGMENT_SEC or (merged and merged[-1][1] - merged[-1][0] < _MIN_SEGMENT_SEC)
        if merged and tiny and begin - merged[-1][1] <= _MAX_JOINED_GAP_SEC:
            merged[-1][1] = finish
        else:
            merged.append([begin, finish])
    # Still tiny means only long pauses surround it, so pad into the silence
    # rather than keep the pause or lose a spoken word.
    for segment in merged:
        missing = _MIN_SEGMENT_SEC - (segment[1] - segment[0])
        if missing > 0:
            segment[1] = min(end, segment[1] + missing)
            segment[0] = max(start, segment[1] - _MIN_SEGMENT_SEC)
    return [(on_grid(a), on_grid(b)) for a, b in merged if on_grid(b) > on_grid(a)]


def total_duration(segments: Sequence[Segment]) -> float:
    return sum(max(0.0, b - a) for a, b in segments if math.isfinite(a) and math.isfinite(b))


def output_time(segments: Sequence[Segment], source_time: float) -> Optional[float]:
    """Where a source moment lands in the clip. Inside a removed gap it snaps forward."""
    if not math.isfinite(source_time):
        return None
    elapsed = 0.0
    for begin, finish in segments:
        if source_time < begin:
            return elapsed
        if source_time <= finish:
            return elapsed + source_time - begin
        elapsed += finish - begin
    return None


def source_time(segments: Sequence[Segment], clip_time: float) -> float:
    """The source moment shown at a point in the clip: the inverse of output_time, at normal speed."""
    if not segments:
        return 0.0
    if not math.isfinite(clip_time):
        return segments[0][0]
    remaining = max(0.0, clip_time)
    for begin, finish in segments:
        # At a join the clip shows the next segment's first frame, not the last one's end.
        if remaining < finish - begin:
            return begin + remaining
        remaining -= finish - begin
    return segments[-1][1]


def edit_segments(words: Sequence, kept: Sequence[int], max_gap: float, *,
                  start: Optional[float] = None, end: Optional[float] = None) -> list[Segment]:
    """Segments for hand-picked words, indexing into ``words``.

    Removing a word must always cut, even when the pause around it is tiny, so a gap
    in the indices forces a break, and the edges stay clear of the removed word.
    """
    segments: list[list[float]] = []
    previous = None
    for index in kept:
        word = words[index]
        begin = max(0.0, word.start - LEAD_IN_SEC)
        adjacent = previous is not None and index == previous + 1
        if not adjacent and index > 0:
            begin = max(begin, words[index - 1].end)
        if segments and adjacent and begin - segments[-1][1] <= max_gap:
            segments[-1][1] = word.end + TAIL_SEC
        else:
            if segments and not adjacent:
                segments[-1][1] = min(segments[-1][1], words[previous + 1].start)
            segments.append([begin, word.end + TAIL_SEC])
        previous = index
    if segments and previous is not None and previous + 1 < len(words):
        segments[-1][1] = min(segments[-1][1], words[previous + 1].start)
    # Padding must not reach words outside the moment, which have no captions.
    if segments and start is not None:
        segments[0][0] = max(segments[0][0], start)
    if segments and end is not None:
        segments[-1][1] = min(segments[-1][1], end)
    rounded = [(on_grid(a), on_grid(b)) for a, b in segments]
    return [(a, b) for a, b in rounded if b > a]


def detect_silences(
    source: str,
    start: float,
    end: float,
    *,
    noise_db: float = -34.0,
    min_duration: float = 0.55,
) -> list[Segment]:
    """Find silent intervals [s, e] in source time inside [start, end] via ffmpeg silencedetect."""
    if not source or not math.isfinite(start) or not math.isfinite(end):
        return []
    safe_start = max(0.0, float(start))
    span = float(end) - safe_start
    if span <= 0.5:
        return []
    cmd = ["ffmpeg", "-hide_banner", "-nostdin"]
    if safe_start > 0:
        cmd.extend(["-ss", f"{safe_start:.3f}"])
    cmd.extend([
        "-t", f"{span:.3f}", "-i", source, "-vn",
        "-af", f"silencedetect=noise={noise_db:g}dB:d={min_duration:g}",
        "-f", "null", os.devnull,
    ])
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **background_subprocess_kwargs(low_priority=True),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("timeline: silencedetect failed for %s: %s", source, exc)
        return []
    stderr = getattr(proc, "stderr", "") or ""
    silences: list[Segment] = []
    open_start: Optional[float] = None
    for line in stderr.splitlines():
        m_start = _SILENCE_START_RE.search(line)
        if m_start:
            open_start = safe_start + float(m_start.group(1))
            continue
        m_end = _SILENCE_END_RE.search(line)
        if m_end and open_start is not None:
            s_end = min(safe_start + span, safe_start + float(m_end.group(1)))
            if s_end > open_start:
                silences.append((open_start, s_end))
            open_start = None
    if open_start is not None and safe_start + span > open_start:
        silences.append((open_start, safe_start + span))
    return silences


def audio_keep_segments(
    start: float,
    end: float,
    silences: Sequence[Segment],
    *,
    pad_sec: float = 0.14,
    min_cut_sec: float = 0.45,
    min_keep_sec: float = 0.35,
) -> list[Segment]:
    """Invert detected silences into grid-aligned kept segments without chopping speech edges."""
    if not math.isfinite(start) or not math.isfinite(end):
        return []
    safe_start = max(0.0, float(start))
    safe_end = float(end)
    if on_grid(safe_end) <= on_grid(safe_start):
        return []

    cuts: list[list[float]] = []
    for raw_a, raw_b in sorted(silences or ()):
        if not (math.isfinite(raw_a) and math.isfinite(raw_b)):
            continue
        a = max(safe_start, raw_a) + max(0.0, pad_sec)
        b = min(safe_end, raw_b) - max(0.0, pad_sec)
        if b - a < min_cut_sec:
            continue
        if cuts and a <= cuts[-1][1]:
            cuts[-1][1] = max(cuts[-1][1], b)
        else:
            cuts.append([a, b])

    if not cuts:
        return [(on_grid(safe_start), on_grid(safe_end))]

    kept: list[list[float]] = []
    cursor = safe_start
    for cut_a, cut_b in cuts:
        if cut_a - cursor >= min_keep_sec:
            kept.append([cursor, cut_a])
        elif kept:
            kept[-1][1] = max(kept[-1][1], cut_a)
        cursor = max(cursor, cut_b)
    if safe_end - cursor >= min_keep_sec:
        kept.append([cursor, safe_end])
    elif kept:
        kept[-1][1] = max(kept[-1][1], safe_end)

    rounded = [(on_grid(a), on_grid(b)) for a, b in kept]
    cleaned = [(a, b) for a, b in rounded if b > a]
    return cleaned or [(on_grid(safe_start), on_grid(safe_end))]
