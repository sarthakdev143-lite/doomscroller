"""Mapping between the source video's clock and a tightened clip's clock.

Removing pauses is most of what makes a clip feel tight, and it means captions,
framing and zooms all need to convert from source time to output time.
Boundaries sit on a frame grid so video and audio cut at the same instant.
"""
from __future__ import annotations

from typing import Optional, Sequence

FPS = 30
Segment = tuple[float, float]

LEAD_IN_SEC = 0.06
TAIL_SEC = 0.10
_MIN_SEGMENT_SEC = 0.25
# Only pauses this short may be kept in order to join a tiny island to its neighbour.
_MAX_JOINED_GAP_SEC = 0.9


def on_grid(seconds: float) -> float:
    return round(seconds * FPS) / FPS


def keep_segments(start: float, end: float, words: Sequence, max_gap: float) -> list[Segment]:
    """The parts of [start, end] to keep: every word, minus pauses over ``max_gap``."""
    inside = [w for w in words if w.end > start and w.start < end]
    if not inside:
        return [(on_grid(start), on_grid(end))]
    segments: list[list[float]] = []
    for word in inside:
        begin, finish = max(start, word.start - LEAD_IN_SEC), min(end, word.end + TAIL_SEC)
        if segments and begin - segments[-1][1] <= max_gap:
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
    return sum(b - a for a, b in segments)


def output_time(segments: Sequence[Segment], source_time: float) -> Optional[float]:
    """Where a source moment lands in the clip. Inside a removed gap it snaps forward."""
    elapsed = 0.0
    for begin, finish in segments:
        if source_time < begin:
            return elapsed
        if source_time <= finish:
            return elapsed + source_time - begin
        elapsed += finish - begin
    return None


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
