"""Per-second measurements of a video, for footage where there is nothing to read.

Motion and loudness come from a tiny grayscale decode and low-rate audio, so a
long film costs seconds rather than a full-resolution pass. The same motion data
drives framing: where the pixels are changing is usually where the subject is.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .media import MediaInfo, read_ffmpeg

_WIDTH, _HEIGHT = 64, 36
_FPS = 4
_AUDIO_RATE = 8000
# A frame this different from its neighbour is a hard cut rather than motion.
_CUT_MIN_DIFF = 0.10
_CUT_LOCAL_RATIO = 5.0
_CUT_DEBOUNCE_SEC = 0.5
_DARK_BRIGHTNESS = 0.05


def _robust_z(values: np.ndarray) -> np.ndarray:
    median = np.median(values)
    spread = np.median(np.abs(values - median)) * 1.4826
    return np.clip((values - median) / (spread + 1e-6), -3, 3)


@dataclass
class Signals:
    duration: float
    motion: np.ndarray
    loudness: np.ndarray
    brightness: np.ndarray
    centroid: np.ndarray
    centroid_weight: np.ndarray
    cuts: list[float]

    @property
    def seconds(self) -> int:
        return len(self.motion)

    def interest(self) -> np.ndarray:
        """How much is happening, per second. Only comparable within one video."""
        onset = np.maximum(0, np.diff(self.loudness, prepend=self.loudness[:1]))
        cut_rate = np.zeros(self.seconds)
        for cut in self.cuts:
            cut_rate[max(0, int(cut) - 5):int(cut) + 5] += 1
        score = (0.45 * _robust_z(self.motion) + 0.35 * _robust_z(self.loudness)
                 + 0.20 * _robust_z(onset) + 0.20 * _robust_z(cut_rate))
        # Fades and black frames can be loud or busy without showing anything.
        score[self.brightness < _DARK_BRIGHTNESS] -= 3
        return score

    def centre_between(self, start: float, end: float) -> float:
        """Where the subject sits horizontally in a span, from 0 (left) to 1 (right)."""
        # Prefer whole seconds, so a second straddling a cut cannot pull the crop off.
        first, last = max(0, int(math.ceil(start))), min(self.seconds, int(math.floor(end)))
        if last <= first or self.centroid_weight[first:last].sum() <= 1e-6:
            first = max(0, int(start))
            last = min(self.seconds, max(first + 1, int(math.ceil(end))))
        weight = self.centroid_weight[first:last]
        if weight.sum() <= 1e-6:
            return 0.5
        return float((self.centroid[first:last] * weight).sum() / weight.sum())

    def centre_spread(self, start: float, end: float) -> float:
        """Standard deviation of the subject's position across a span."""
        first = max(0, int(start))
        last = min(self.seconds, max(first + 1, int(math.ceil(end))))
        weight = self.centroid_weight[first:last]
        if weight.sum() <= 1e-6:
            return 0.0
        centre = (self.centroid[first:last] * weight).sum() / weight.sum()
        return float(math.sqrt(((self.centroid[first:last] - centre) ** 2 * weight).sum() / weight.sum()))


def _find_cuts(diffs: np.ndarray) -> list[float]:
    cuts: list[float] = []
    window = _FPS * 8
    for index in np.flatnonzero(diffs > _CUT_MIN_DIFF):
        around = diffs[max(0, index - window):index + window]
        if diffs[index] < _CUT_LOCAL_RATIO * np.median(around):
            continue
        time = float(index) / _FPS
        if not cuts or time - cuts[-1] >= _CUT_DEBOUNCE_SEC:
            cuts.append(time)
    return cuts


def _video_signals(path: str, seconds: int) -> dict:
    raw = read_ffmpeg(["-i", path, "-map", "0:v:0", "-an", "-vf",
                       f"fps={_FPS},scale={_WIDTH}:{_HEIGHT}:flags=fast_bilinear,format=gray",
                       "-f", "rawvideo", "-"])
    pixels = np.frombuffer(raw, dtype=np.uint8)
    count = pixels.size // (_WIDTH * _HEIGHT)
    frames = pixels[:count * _WIDTH * _HEIGHT].reshape(count, _HEIGHT, _WIDTH)

    diffs = np.zeros(max(count, 1))
    columns = np.zeros((max(count, 1), _WIDTH))
    for index in range(1, count):
        delta = np.abs(frames[index].astype(np.int16) - frames[index - 1].astype(np.int16))
        diffs[index] = delta.mean() / 255
        columns[index] = delta.sum(axis=0)
    cuts = _find_cuts(diffs)
    is_cut = {int(cut * _FPS) for cut in cuts}

    motion = np.zeros(seconds)
    brightness = np.zeros(seconds)
    weight = np.zeros(seconds)
    position = np.zeros(seconds)
    samples = np.zeros(seconds)
    xs = np.arange(_WIDTH) / (_WIDTH - 1)
    for index in range(count):
        second = min(seconds - 1, index // _FPS)
        brightness[second] += frames[index].mean() / 255
        samples[second] += 1
        if index in is_cut:
            continue
        motion[second] += diffs[index]
        column_sum = columns[index].sum()
        if column_sum > 0:
            position[second] += (columns[index] * xs).sum()
            weight[second] += column_sum
    filled = samples > 0
    motion[filled] /= samples[filled]
    brightness[filled] /= samples[filled]
    centroid = np.where(weight > 0, position / np.maximum(weight, 1e-6), 0.5)
    return {"motion": motion, "brightness": brightness, "centroid": centroid,
            "centroid_weight": weight, "cuts": cuts}


def _loudness(path: str, seconds: int, has_audio: bool) -> np.ndarray:
    decibels = np.full(seconds, -90.0)
    if not has_audio:
        return decibels
    raw = read_ffmpeg(["-i", path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(_AUDIO_RATE),
                       "-f", "s16le", "-"])
    samples = np.frombuffer(raw, dtype=np.int16)
    for second in range(seconds):
        chunk = samples[second * _AUDIO_RATE:(second + 1) * _AUDIO_RATE].astype(np.float32)
        if chunk.size:
            rms = math.sqrt(float((chunk * chunk).mean()))
            decibels[second] = 20 * math.log10(max(rms / 32768, 1e-5))
    return decibels


def analyse(path: str, info: MediaInfo) -> Signals:
    seconds = max(1, int(math.ceil(info.duration)))
    return Signals(
        duration=info.duration,
        loudness=_loudness(path, seconds, info.has_audio),
        **_video_signals(path, seconds),
    )
