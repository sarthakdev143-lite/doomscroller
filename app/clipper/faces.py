"""Finding the person talking in a shot, so a vertical crop lands on them.

Motion alone is not enough: in a wide two-person shot it averages out to the empty
space between them. This looks at pairs of frames a moment apart and scores each face
by how much it moves, because the person talking moves their mouth, head and hands
while the listener mostly holds still. It is a heuristic, so a nodding listener can win.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import numpy as np

from .media import MediaError, MediaInfo, read_ffmpeg

log = logging.getLogger(__name__)

# High enough to find the small faces in a wide shot.
_DETECT_WIDTH = 1280
_SAMPLES_PER_SHOT = 3
_PAIR_FPS = 5
_MIN_FACE_SHARE = 0.025
_MIN_NEIGHBOURS = 5


@lru_cache(maxsize=1)
def _detector():
    try:
        import cv2

        classifier = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except (ImportError, AttributeError) as exc:
        log.warning("face detection unavailable: %s", exc)
        return None
    return None if classifier.empty() else classifier


def speaker_in_pair(first: np.ndarray, second: np.ndarray) -> Optional[tuple[float, float]]:
    """(horizontal centre from 0 to 1, confidence) of the likeliest speaker, if any."""
    found = speaker_box_in_pair(first, second)
    return (found[0], found[2]) if found else None


def speaker_box_in_pair(first: np.ndarray, second: np.ndarray) -> Optional[tuple[float, float, float]]:
    """(horizontal centre, vertical centre, confidence) of the likeliest speaker, each centre from 0 to 1."""
    detector = _detector()
    if detector is None:
        return None
    height, width = first.shape
    smallest = max(1, int(width * _MIN_FACE_SHARE))
    found = detector.detectMultiScale(first, scaleFactor=1.1, minNeighbors=_MIN_NEIGHBOURS,
                                      minSize=(smallest, smallest))
    best: Optional[tuple[float, float, float]] = None
    for x, y, w, h in found:
        # Take in the jaw and chin as well, where talking shows.
        top, bottom = max(0, y), min(height, y + int(h * 1.3))
        left, right = max(0, x), min(width, x + w)
        motion = float(np.abs(first[top:bottom, left:right].astype(np.int16)
                              - second[top:bottom, left:right].astype(np.int16)).mean())
        # Bigger faces get some credit, so detector noise on a shelf cannot win.
        score = (motion + 0.5) * float(np.sqrt(w))
        if best is None or score > best[2]:
            best = ((x + w / 2) / width, (y + h / 2) / height, score)
    return best


def _frames_at(source: str, when: float, height: int) -> tuple[np.ndarray, np.ndarray]:
    raw = read_ffmpeg(["-ss", f"{when:.3f}", "-i", source, "-map", "0:v:0", "-frames:v", "2", "-vf",
                       f"fps={_PAIR_FPS},scale={_DETECT_WIDTH}:{height},format=gray", "-f", "rawvideo", "-"])
    pixels = np.frombuffer(raw, dtype=np.uint8)
    size = _DETECT_WIDTH * height
    if pixels.size < size:
        raise MediaError("could not read a frame")
    first = pixels[:size].reshape(height, _DETECT_WIDTH)
    # At the very end of a file there may be no second frame to compare.
    second = pixels[size:size * 2].reshape(height, _DETECT_WIDTH) if pixels.size >= size * 2 else first
    return first, second


def face_centre(source: str, start: float, end: float, info: MediaInfo) -> Optional[float]:
    """Where the person talking is in the shot [start, end], or None if nobody was found."""
    if _detector() is None or not info.width:
        return None
    height = int(_DETECT_WIDTH * info.height / info.width) // 2 * 2
    picks = []
    for step in range(1, _SAMPLES_PER_SHOT + 1):
        when = start + (end - start) * step / (_SAMPLES_PER_SHOT + 1)
        try:
            pick = speaker_in_pair(*_frames_at(source, when, height))
        except MediaError as exc:
            log.debug("faces: no frame at %.2f: %s", when, exc)
            continue
        if pick is not None:
            picks.append(pick)
    # Never average: two people's positions average out to the empty space between them.
    return max(picks, key=lambda pick: pick[1])[0] if picks else None


def speaker_position(source: str, start: float, end: float, info: MediaInfo) -> Optional[tuple[float, float]]:
    """(x, y) of the face of the person talking in [start, end], each from 0 to 1, or None."""
    if _detector() is None or not info.width:
        return None
    height = int(_DETECT_WIDTH * info.height / info.width) // 2 * 2
    picks = []
    for step in range(1, _SAMPLES_PER_SHOT + 1):
        when = start + (end - start) * step / (_SAMPLES_PER_SHOT + 1)
        try:
            pick = speaker_box_in_pair(*_frames_at(source, when, height))
        except MediaError as exc:
            log.debug("faces: no frame at %.2f: %s", when, exc)
            continue
        if pick is not None:
            picks.append(pick)
    if not picks:
        return None
    x, y, _score = max(picks, key=lambda pick: pick[2])
    return x, y

