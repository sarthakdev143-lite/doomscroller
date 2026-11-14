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
    if values.size == 0:
        return np.zeros(0, dtype=np.float64)
    clean = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    median = np.median(clean)
    spread = np.median(np.abs(clean - median)) * 1.4826
    return np.clip((clean - median) / (spread + 1e-6), -3, 3)


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
        if self.seconds <= 0:
            return np.zeros(0, dtype=np.float64)
        onset = np.maximum(0, np.diff(self.loudness, prepend=self.loudness[:1]))
        cut_rate = np.zeros(self.seconds)
        for cut in self.cuts:
            if math.isfinite(cut):
                cut_rate[max(0, int(cut) - 5):min(self.seconds, max(0, int(cut) + 5))] += 1
        score = (0.45 * _robust_z(self.motion) + 0.35 * _robust_z(self.loudness)
                 + 0.20 * _robust_z(onset) + 0.20 * _robust_z(cut_rate))
        # Fades and black frames can be loud or busy without showing anything.
        score[self.brightness < _DARK_BRIGHTNESS] -= 3
        return score

    def centre_between(self, start: float, end: float) -> float:
        """Where the subject sits horizontally in a span, from 0 (left) to 1 (right)."""
        if self.seconds <= 0 or not math.isfinite(start) or not math.isfinite(end):
            return 0.5
        if end < start:
            start, end = end, start
        # Prefer whole seconds, so a second straddling a cut cannot pull the crop off.
        first, last = max(0, int(math.ceil(start))), min(self.seconds, int(math.floor(end)))
        if last <= first or self.centroid_weight[first:last].sum() <= 1e-6:
            first = max(0, int(start))
            last = min(self.seconds, max(first + 1, int(math.ceil(end))))
        weight = self.centroid_weight[first:last]
        if weight.sum() <= 1e-6:
            return 0.5
        value = float((self.centroid[first:last] * weight).sum() / weight.sum())
        return min(1.0, max(0.0, value)) if math.isfinite(value) else 0.5

    def centre_spread(self, start: float, end: float) -> float:
        """Standard deviation of the subject's position across a span."""
        if self.seconds <= 0 or not math.isfinite(start) or not math.isfinite(end):
            return 0.0
        if end < start:
            start, end = end, start
        first = max(0, int(start))
        last = min(self.seconds, max(first + 1, int(math.ceil(end))))
        weight = self.centroid_weight[first:last]
        if weight.sum() <= 1e-6:
            return 0.0
        centre = (self.centroid[first:last] * weight).sum() / weight.sum()
        variance = float(((self.centroid[first:last] - centre) ** 2 * weight).sum() / weight.sum())
        return float(math.sqrt(max(0.0, variance))) if math.isfinite(variance) else 0.0


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
    if count > 1:
        # Vectorised chunked diff avoids 14k+ Python loop iterations while bounding temporary RAM.
        for start in range(1, count, 1024):
            end = min(count, start + 1024)
            delta = np.abs(frames[start:end].astype(np.int16) - frames[start - 1:end - 1].astype(np.int16))
            diffs[start:end] = delta.mean(axis=(1, 2)) / 255
            columns[start:end] = delta.sum(axis=1)
    cuts = _find_cuts(diffs)
    is_cut = np.zeros(max(count, 1), dtype=bool)
    for cut in cuts:
        idx = int(cut * _FPS)
        if 0 <= idx < is_cut.size:
            is_cut[idx] = True

    motion = np.zeros(seconds)
    brightness = np.zeros(seconds)
    weight = np.zeros(seconds)
    position = np.zeros(seconds)
    samples = np.zeros(seconds)
    if count > 0:
        sec_idx = np.minimum(seconds - 1, np.arange(count) // _FPS)
        frame_means = frames.mean(axis=(1, 2)) / 255
        np.add.at(brightness, sec_idx, frame_means)
        np.add.at(samples, sec_idx, 1.0)
        keep = ~is_cut[:count]
        if np.any(keep):
            keep_sec = sec_idx[keep]
            np.add.at(motion, keep_sec, diffs[:count][keep])
            col_sums = columns[:count][keep].sum(axis=1)
            pos_mask = col_sums > 0
            if np.any(pos_mask):
                xs = np.arange(_WIDTH) / (_WIDTH - 1)
                weighted_pos = columns[:count][keep][pos_mask] @ xs
                np.add.at(position, keep_sec[pos_mask], weighted_pos)
                np.add.at(weight, keep_sec[pos_mask], col_sums[pos_mask])
    filled = samples > 0
    motion[filled] /= samples[filled]
    brightness[filled] /= samples[filled]
    centroid = np.where(weight > 0, position / np.maximum(weight, 1e-6), 0.5)
    return {"motion": motion, "brightness": brightness, "centroid": centroid,
            "centroid_weight": weight, "cuts": cuts}


def _loudness(path: str, seconds: int, has_audio: bool) -> np.ndarray:
    seconds = max(0, int(seconds))
    decibels = np.full(seconds, -90.0)
    if not has_audio or seconds == 0:
        return decibels
    raw = read_ffmpeg(["-i", path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(_AUDIO_RATE),
                       "-f", "s16le", "-"])
    even_len = (len(raw) // 2) * 2
    if even_len == 0:
        return decibels
    samples = np.frombuffer(memoryview(raw)[:even_len], dtype=np.int16)
    full_secs = min(seconds, samples.size // _AUDIO_RATE)
    if full_secs > 0:
        blocks = samples[:full_secs * _AUDIO_RATE].reshape(full_secs, _AUDIO_RATE).astype(np.float32)
        rms = np.sqrt((blocks * blocks).mean(axis=1))
        decibels[:full_secs] = 20.0 * np.log10(np.maximum(rms / 32768.0, 1e-5))
    if full_secs < seconds:
        tail = samples[full_secs * _AUDIO_RATE:(full_secs + 1) * _AUDIO_RATE].astype(np.float32)
        if tail.size:
            rms_tail = math.sqrt(float((tail * tail).mean()))
            decibels[full_secs] = 20 * math.log10(max(rms_tail / 32768, 1e-5))
    return decibels


def analyse(path: str, info: MediaInfo) -> Signals:
    safe_dur = info.duration if math.isfinite(info.duration) and info.duration > 0 else 1.0
    seconds = max(1, int(math.ceil(safe_dur)))
    return Signals(
        duration=safe_dur,
        loudness=_loudness(path, seconds, info.has_audio),
        **_video_signals(path, seconds),
    )
