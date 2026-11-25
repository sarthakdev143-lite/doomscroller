"""Claim-free procedural 16:9 Lofi, Rain, Night Ambient, and Deep Study video generator."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from app.clipper.media import probe, run_ffmpeg
from app.metadata import compose, finished_title, youtube_description
from app.transformers.fonts import resolve_font
from app.winproc import background_subprocess_kwargs, reclaim_memory

if TYPE_CHECKING:
    from PIL import Image

log = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1920, 1080, 30
THUMB_W, THUMB_H = 1280, 720
_MAX_THUMB_BYTES = 1_900_000
SAMPLE_RATE = 48000
DEFAULT_LOOP_SEC = 16.0


@dataclass(frozen=True)
class AmbientPreset:
    key: str
    label: str
    bpm: float
    musical_key: str
    chords_midi: tuple[tuple[int, ...], ...]
    bass_midi: tuple[int, ...]
    pulse_mode: str
    rain_level: float
    vinyl_level: float
    brown_level: float
    shimmer_level: float
    sky_top: tuple[int, int, int]
    sky_bottom: tuple[int, int, int]
    glow_color: tuple[int, int, int]
    accent_color: tuple[int, int, int]
    scene_style: str
    mood_summary: str
    title_templates: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True)
class AmbientMetadata:
    title: str
    description: str
    tags: tuple[str, ...]
    language: str
    chapters: tuple[tuple[float, str], ...]
    chapter_text: str


@dataclass(frozen=True)
class AmbientResult:
    output_path: str
    preset: str
    duration: float
    width: int
    height: int
    fps: int
    title: str
    description: str
    tags: tuple[str, ...]
    chapters: tuple[tuple[float, str], ...]
    chapter_text: str
    thumbnails: tuple[dict, ...] = ()
    hero_frame: str = ""
    thumbnail_prompt: str = ""
    language: str = "en"
    theme: str = ""
    part_number: int = 1
    portrait_path: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def duration_sec(self) -> int:
        return int(round(self.duration))


AMBIENT_PRESETS: dict[str, AmbientPreset] = {
    "lofi_chill": AmbientPreset(
        key="lofi_chill",
        label="2AM Memory · Slowed Dark Ambient",
        bpm=54.0,
        musical_key="F min9",
        chords_midi=(
            (53, 56, 60, 63, 67),
            (51, 55, 58, 62, 67),
            (49, 53, 56, 60, 63),
            (48, 51, 55, 58, 63),
        ),
        bass_midi=(41, 39, 37, 36),
        pulse_mode="ethereal_pad",
        rain_level=0.16,
        vinyl_level=0.14,
        brown_level=0.22,
        shimmer_level=0.24,
        sky_top=(8, 12, 32),
        sky_bottom=(24, 18, 54),
        glow_color=(122, 138, 255),
        accent_color=(172, 188, 255),
        scene_style="rooftop_window",
        mood_summary="Slowed ethereal dark ambient pads, distant felt piano reverb, and 2am midnight stillness.",
        title_templates=(
            "she was only a dream",
            "i still think about her",
            "3am, you're not alone",
        ),
        tags=(
            "ambient music", "dark ambient", "sleep music", "2am thoughts",
            "late night loneliness", "oneheart", "snowfall ambient", "sad ambient mix",
            "slowed and reverb", "insomnia relief", "midnight playlist", "dreamscape",
            "music for overthinking", "calm your mind", "night ambient mix",
        ),
    ),
    "rain_focus": AmbientPreset(
        key="rain_focus",
        label="Midnight Rain · Sleep & Dissociate",
        bpm=52.0,
        musical_key="D min9",
        chords_midi=(
            (50, 53, 57, 60, 64),
            (46, 50, 53, 57, 62),
            (43, 46, 50, 53, 57),
            (45, 50, 52, 57, 60),
        ),
        bass_midi=(38, 34, 31, 33),
        pulse_mode="rain_swell",
        rain_level=0.58,
        vinyl_level=0.08,
        brown_level=0.34,
        shimmer_level=0.22,
        sky_top=(6, 15, 34),
        sky_bottom=(16, 36, 62),
        glow_color=(102, 174, 232),
        accent_color=(162, 214, 248),
        scene_style="rainy_glass",
        mood_summary="Soft rain on cold glass with cavern-reverb dark ambient chords for deep sleep.",
        title_templates=(
            "it's late, go to bed.",
            "let your mind rest.",
            "it's 3 am, you need sleep.",
        ),
        tags=(
            "midnight rain", "sleep rain sounds", "dark ambient music", "rain on window",
            "sounds for sleep", "go to sleep", "insomnia music", "3am rain mix",
            "sad piano rain", "slowed reverb ambient", "calm night ambience", "night rain",
            "ambient sleep music", "peaceful midnight", "deep sleep soundscape",
        ),
    ),
    "night_ambient": AmbientPreset(
        key="night_ambient",
        label="Cobalt Twilight · Nocturne Drone",
        bpm=50.0,
        musical_key="C min9",
        chords_midi=(
            (48, 51, 55, 58, 62),
            (44, 48, 51, 55, 60),
            (41, 44, 48, 51, 55),
            (43, 48, 50, 55, 58),
        ),
        bass_midi=(36, 32, 29, 31),
        pulse_mode="nocturne_drone",
        rain_level=0.10,
        vinyl_level=0.06,
        brown_level=0.28,
        shimmer_level=0.36,
        sky_top=(5, 9, 24),
        sky_bottom=(14, 26, 64),
        glow_color=(130, 168, 255),
        accent_color=(198, 218, 255),
        scene_style="starry_horizon",
        mood_summary="Deep cobalt midnight synth drones, slowed reverb shimmer, and quiet solitude.",
        title_templates=(
            "nothing feels the same anymore.",
            "you're not alone",
            "forget about it, relax.",
        ),
        tags=(
            "dark ambient mix", "night ambient music", "ambient music dark", "sleep music",
            "music for sleep", "2am vibes", "slow nocturne", "oneheart",
            "antent", "diedlonely", "ethereal night mix", "dreamy ambient pads",
            "late night chill", "hypnotic soundscape", "ambient chillout",
        ),
    ),
    "deep_study": AmbientPreset(
        key="deep_study",
        label="Empty Train · Midnight Drift",
        bpm=56.0,
        musical_key="A min9",
        chords_midi=(
            (45, 48, 52, 55, 59),
            (41, 45, 48, 52, 57),
            (48, 52, 55, 59, 64),
            (43, 47, 50, 52, 59),
        ),
        bass_midi=(33, 29, 36, 31),
        pulse_mode="midnight_drift",
        rain_level=0.18,
        vinyl_level=0.10,
        brown_level=0.30,
        shimmer_level=0.26,
        sky_top=(8, 16, 32),
        sky_bottom=(22, 22, 48),
        glow_color=(140, 168, 245),
        accent_color=(194, 212, 255),
        scene_style="study_desk",
        mood_summary="Slowed midnight reverb tones and quiet cobalt stillness for late-night overthinking.",
        title_templates=(
            "let your heart rest for a moment..",
            "calm down.",
            "you need some rest.",
        ),
        tags=(
            "late night playlist", "dark ambient", "after midnight mix", "quiet company music",
            "slowed ambient", "rainy night ambience", "sad piano", "sleep soundscape",
            "midnight stillness", "emotional chillout", "overthinking music", "cozy dark ambient",
            "nocturne chill", "late night focus", "night solitude",
        ),
    ),
}

_CHAPTER_LABELS: dict[str, tuple[str, ...]] = {
    "lofi_chill": ("Fairy Light Glow", "Unspoken Words", "2:14 AM Rain", "Warm Hoodie Season", "One Last Glance", "Until Dawn"),
    "rain_focus": ("Window Droplets", "Breathe Slow", "Midnight Traffic Fade", "Heavy Silver Rain", "Quiet Breathing", "Almost Asleep"),
    "night_ambient": ("Candlelight Shadow", "Hold Still", "Velvet Midnight", "Close Enough", "No One Else", "First Light"),
    "deep_study": ("Amber Desk Lamp", "Two Cups Left", "Soft Rain Outside", "Stay Awhile", "Quiet Hours", "Closing Notes"),
}


def list_presets() -> list[dict]:
    """Lightweight preset listing safe to call during idle UI/API requests without importing numpy."""
    return [
        {"key": p.key, "label": p.label, "bpm": p.bpm, "musical_key": p.musical_key, "scene_style": p.scene_style}
        for p in AMBIENT_PRESETS.values()
    ]


def _get_preset(preset_key: str) -> AmbientPreset:
    key = str(preset_key or "").strip().lower()
    if key not in AMBIENT_PRESETS:
        raise ValueError(f"unknown ambient preset {preset_key!r}; expected one of {', '.join(AMBIENT_PRESETS)}")
    return AMBIENT_PRESETS[key]


def _seed_int(seed: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _duration_badge(duration_sec: float) -> str:
    mins = max(1, int(round(duration_sec / 60.0)))
    if mins >= 60 and mins % 60 == 0:
        hrs = mins // 60
        return f"{hrs} Hour" if hrs == 1 else f"{hrs} Hours"
    if duration_sec < 90:
        return f"{max(1, int(round(duration_sec)))}s"
    return f"{mins} Min"


def _format_chapters(chapters: Sequence[tuple[float, str]]) -> str:
    lines: list[str] = []
    seen_ts: set[int] = set()
    for raw_sec, raw_label in chapters:
        sec = max(0, int(round(raw_sec)))
        if sec in seen_ts and lines:
            continue
        seen_ts.add(sec)
        hrs = sec // 3600
        mins = (sec % 3600) // 60
        rem = sec % 60
        stamp = f"{hrs}:{mins:02d}:{rem:02d}" if hrs > 0 else f"{mins}:{rem:02d}"
        label = " ".join(str(raw_label or "Drift").split()).strip()[:80] or "Drift"
        lines.append(f"{stamp} {label}")
    return "\n".join(lines)


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg")


def ambient_portraits_dir() -> Path:
    from app.config import settings
    return settings.content_path / "ambient_portraits"


def ambient_music_dir() -> Path:
    from app.config import settings
    return settings.content_path / "ambient_music"


def ambient_overlays_dir() -> Path:
    from app.config import settings
    return settings.content_path / "ambient_overlays"


def list_ambient_portraits() -> list[dict]:
    root = ambient_portraits_dir()
    if not root.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(root.iterdir()):
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS:
            stem = p.stem.lower()
            matched = stem if stem in AMBIENT_PRESETS else next((k for k in AMBIENT_PRESETS if stem.startswith(f"{k}_")), "")
            out.append({
                "name": p.name,
                "preset": matched,
                "path": str(p),
                "size_bytes": p.stat().st_size,
            })
    return out


def list_ambient_tracks(preset: str = "", *, include_siblings: bool = False) -> list[dict]:
    root = ambient_music_dir()
    if not root.is_dir():
        return []
    key = str(preset or "").strip().lower()
    seen: set[Path] = set()
    candidates: list[tuple[str, Path]] = []
    if not key:
        scan_keys = tuple(AMBIENT_PRESETS)
    elif include_siblings:
        scan_keys = (key,) + tuple(k for k in AMBIENT_PRESETS if k != key)
    else:
        scan_keys = (key,)
    for pkey in scan_keys:
        sub = root / pkey
        if sub.is_dir():
            for p in sorted(sub.iterdir()):
                if p.is_file() and p.suffix.lower() in _AUDIO_EXTS and p not in seen:
                    candidates.append((pkey, p))
                    seen.add(p)
    for p in sorted(root.iterdir()):
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS and p not in seen:
            candidates.append(("", p))
            seen.add(p)
    return [
        {
            "name": _clean_track_title(p.stem),
            "filename": p.name,
            "preset": p_tag,
            "path": str(p),
            "size_bytes": p.stat().st_size,
        }
        for p_tag, p in candidates
    ]


def _clean_track_title(stem: str) -> str:
    clean = " ".join(stem.replace("_", " ").replace("-", " ").split()).strip()
    return clean[:72] or "Midnight Track"


def _resolve_preset_portraits(preset: str, portrait_path: Optional[Path | str] = None) -> list[Path]:
    """Return all ordered 16:9 cinema scene portraits for a preset (or single explicit upload)."""
    if portrait_path:
        explicit = Path(portrait_path)
        if explicit.is_file():
            return [explicit]
    root = ambient_portraits_dir()
    if not root.is_dir():
        return []
    key = _get_preset(preset).key
    found: list[Path] = []
    seen: set[Path] = set()
    for ext in _IMAGE_EXTS:
        cand = root / f"{key}{ext}"
        if cand.is_file() and cand not in seen:
            found.append(cand)
            seen.add(cand)
    for p in sorted(root.iterdir()):
        if (
            p.is_file()
            and p not in seen
            and p.suffix.lower() in _IMAGE_EXTS
            and p.stem.lower().startswith(f"{key}_")
            and not p.stem.lower().endswith("_custom")
        ):
            found.append(p)
            seen.add(p)
            if len(found) >= 4:
                break
    if found:
        return found
    any_files = sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS and not p.stem.lower().endswith("_custom")
    )
    return any_files[:1]


def _resolve_portrait(preset: str, portrait_path: Optional[Path | str] = None) -> Optional[Path]:
    scenes = _resolve_preset_portraits(preset, portrait_path)
    return scenes[0] if scenes else None


def _resolve_overlay(preset: str) -> Optional[Path]:
    root = ambient_overlays_dir()
    if not root.is_dir():
        return None
    key = _get_preset(preset).key
    prefs = {
        "rain_focus": ("midnight_bokeh.mp4", "warm_gold_dust.mp4"),
        "night_ambient": ("midnight_bokeh.mp4", "warm_gold_dust.mp4"),
        "lofi_chill": ("midnight_bokeh.mp4", "warm_gold_dust.mp4"),
        "deep_study": ("midnight_bokeh.mp4", "warm_gold_dust.mp4"),
    }.get(key, ("midnight_bokeh.mp4", "warm_gold_dust.mp4"))
    for name in prefs:
        cand = root / name
        if cand.is_file():
            return cand
    return None


def _load_track_credits() -> dict[str, dict]:
    cred_path = ambient_music_dir() / "credits.json"
    if not cred_path.is_file():
        return {}
    try:
        raw = json.loads(cred_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _order_curated_tracks(
    tracks: Sequence[dict],
    *,
    seed: str = "",
    limit: int = 12,
    target_seconds: float = 0.0,
) -> list[dict]:
    ordered = list(tracks)
    if not ordered:
        return []
    primary_tag = str(ordered[0].get("preset") or "")
    if primary_tag:
        primary = [tr for tr in ordered if str(tr.get("preset") or "") == primary_tag]
        siblings = [tr for tr in ordered if str(tr.get("preset") or "") != primary_tag]
    else:
        primary, siblings = ordered, []
    rot_p = _seed_int(seed or "curated") % len(primary)
    rotated = primary[rot_p:] + primary[:rot_p]
    if siblings:
        rot_s = _seed_int(f"{seed or 'curated'}:sib") % len(siblings)
        rotated.extend(siblings[rot_s:] + siblings[:rot_s])
    rotated = rotated[:limit]
    if target_seconds <= 0.0 or len(rotated) <= 2:
        return rotated
    chosen: list[dict] = []
    acc = 0.0
    for tr in rotated:
        chosen.append(tr)
        try:
            dur = max(2.0, _probe_audio_duration(tr["path"]))
        except Exception:
            dur = 160.0
        acc += max(1.5, dur - 4.0)
        if len(chosen) >= 2 and acc >= (target_seconds + 15.0):
            break
    return chosen


def _resolve_track_credits_for(tracks: Sequence[dict]) -> tuple[str, ...]:
    credits_map = _load_track_credits()
    lines: list[str] = []
    seen: set[str] = set()
    for tr in tracks:
        fn = str(tr.get("filename") or Path(str(tr.get("path") or "")).name)
        entry = credits_map.get(fn)
        if not isinstance(entry, dict):
            continue
        ctxt = str(entry.get("credit_text") or "").strip()
        if ctxt and ctxt not in seen:
            lines.append(ctxt)
            seen.add(ctxt)
    return tuple(lines)


def _prepare_portrait_canvas(
    portrait_file: Path,
    *,
    size: tuple[int, int] = (WIDTH, HEIGHT),
    brand_name: str = "",
) -> "Image.Image":
    """Fit an alluring night portrait into 16:9 with eye-preserving framing, soft cobalt-violet film grade, and zero text."""
    from PIL import Image, ImageEnhance, ImageOps

    w, h = size
    with Image.open(portrait_file) as im:
        fitted = ImageOps.fit(
            ImageOps.exif_transpose(im).convert("RGB"),
            (w, h),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.24),
        )
    # Feather-light polish so owner-graded midnight portraits keep their native colors and crispness.
    fitted = ImageEnhance.Color(fitted).enhance(0.98)
    fitted = ImageEnhance.Brightness(fitted).enhance(0.96)
    fitted = ImageEnhance.Contrast(fitted).enhance(1.04).convert("RGBA")

    cobalt_wash = Image.new("RGBA", (w, h), (18, 22, 68, 12))
    out = Image.alpha_composite(fitted, cobalt_wash)

    # Gentle bottom-edge vignette leaving the face, eyes, and silhouette luminous.
    scrim = Image.new("RGBA", (1, h), (0, 0, 0, 0))
    px = scrim.load()
    start_y = int(h * 0.72)
    for y in range(start_y, h):
        frac = (y - start_y) / max(1, h - start_y)
        px[0, y] = (6, 8, 18, int(round(48 * (frac ** 1.5))))
    out = Image.alpha_composite(out, scrim.resize((w, h), Image.Resampling.BILINEAR))
    return out.convert("RGB")


def build_ambient_metadata(
    preset: str,
    *,
    duration_sec: float,
    title: Optional[str] = None,
    brand_name: str = "",
    theme: str = "",
    seed: str = "",
    chapter_interval_sec: Optional[float] = None,
    language: str = "en",
    include_chapters: bool = True,
    chapters_override: Optional[Sequence[tuple[float, str]]] = None,
    credits_lines: Sequence[str] = (),
) -> AmbientMetadata:
    """Build claim-free SEO metadata and chapters (no #Shorts, no viral/trending filler)."""
    cfg = _get_preset(preset)
    dur_badge = _duration_badge(duration_sec)
    idx = _seed_int(seed or f"{cfg.key}:{int(round(duration_sec))}") % len(cfg.title_templates)
    raw_title = (title or "").strip()
    if not raw_title:
        base = cfg.title_templates[idx].format(dur=dur_badge)
        if theme and theme.strip():
            raw_title = f"{theme.strip()} | {base}"
        else:
            raw_title = base

    if chapters_override:
        chapters: list[tuple[float, str]] = [(round(max(0.0, float(s)), 2), str(lbl)) for s, lbl in chapters_override]
    else:
        interval = float(chapter_interval_sec) if chapter_interval_sec else (300.0 if duration_sec >= 900.0 else max(15.0, round(duration_sec / 4.0)))
        labels = _CHAPTER_LABELS.get(cfg.key, ("Drift", "Focus", "Calm", "Night"))
        chapters = [(0.0, labels[0])]
        t = interval
        step_i = 1
        while t < max(0.0, duration_sec - 8.0):
            chapters.append((round(t, 2), labels[step_i % len(labels)]))
            t += interval
            step_i += 1
    chapter_text = _format_chapters(chapters)

    brand_line = f"Curated by {brand_name.strip()}." if brand_name.strip() else ""
    theme_line = f"Mood: {theme.strip()}." if theme.strip() else ""
    body_parts = [
        cfg.mood_summary,
        theme_line,
        "100% original ambient music & visuals — claim-free and safe for late-night listening, sleep, reading, and relaxation.",
        brand_line,
    ]
    if include_chapters and chapter_text:
        body_parts.append(f"Chapters:\n{chapter_text}")
    raw_desc = "\n\n".join(p for p in body_parts if p)

    composed = compose(
        title=finished_title(raw_title, shorts_tag=False),
        description=raw_desc,
        keywords=list(cfg.tags),
        language=language or "en",
        source="procedural",
        require_credit=False,
        is_short=False,
        title_shorts_tag=False,
    )
    clean_title = finished_title(composed.title, shorts_tag=False)
    clean_desc = composed.description
    if credits_lines and "Music Credits:" not in clean_desc:
        clean_desc = f"{clean_desc}\n\nMusic Credits:\n" + "\n".join(credits_lines)
    if include_chapters and chapter_text and "Chapters:" not in clean_desc:
        clean_desc = f"{clean_desc}\n\nChapters:\n{chapter_text}"
    clean_desc = youtube_description(clean_desc)
    return AmbientMetadata(
        title=clean_title,
        description=clean_desc,
        tags=tuple(composed.tags),
        language=language or "en",
        chapters=tuple(chapters),
        chapter_text=chapter_text,
    )


def _midi_to_hz(midi: int) -> float:
    return 440.0 * (2.0 ** ((float(midi) - 69.0) / 12.0))


def _synthesize_raw_loop_wav(
    preset: str,
    wav_path: Path,
    *,
    cycle_sec: Optional[float] = None,
    seed: str = "",
) -> float:
    """Lazily import numpy and synthesize a seamless circular 48kHz stereo PCM loop."""
    import numpy as np

    cfg = _get_preset(preset)
    bars = len(cfg.chords_midi)
    bar_sec = 4.0 * 60.0 / cfg.bpm
    target_cycle = float(cycle_sec) if cycle_sec is not None else round(bars * bar_sec, 3)
    target_cycle = max(2.0, target_cycle)
    xfade_sec = min(2.0, target_cycle * 0.35)
    total_sec = target_cycle + xfade_sec

    m_samples = int(round(target_cycle * SAMPLE_RATE))
    k_samples = int(round(xfade_sec * SAMPLE_RATE))
    n_samples = m_samples + k_samples
    t = np.arange(n_samples, dtype=np.float64) / float(SAMPLE_RATE)
    rng = np.random.default_rng(_seed_int(seed or cfg.key))

    periodic = np.zeros((m_samples, 2), dtype=np.float64)
    actual_bar_sec = target_cycle / float(bars)

    def _add_periodic(start_idx: int, sig_lr) -> None:
        slen = len(sig_lr)
        if slen <= 0 or m_samples <= 0:
            return
        pos = start_idx % m_samples
        first = min(slen, m_samples - pos)
        periodic[pos:pos + first] += sig_lr[:first]
        rem = slen - first
        if rem > 0:
            second = min(rem, m_samples)
            periodic[:second] += sig_lr[first:first + second]

    # 1. Warm Rhodes/felt chord pads + deep bass + expressive felt-piano top-line (modulo m_samples periodic).
    motif_idx = _seed_int(f"melody:{seed or cfg.key}") % 3
    sub_steps = ((0.0, 0.25, 0.50, 0.75), (0.0, 0.28, 0.54, 0.78), (0.125, 0.375, 0.625, 0.875))[motif_idx]

    for bar_idx, (chord, bass) in enumerate(zip(cfg.chords_midi, cfg.bass_midi)):
        i0 = int(round(bar_idx * actual_bar_sec * SAMPLE_RATE))
        blen = max(64, int(round(actual_bar_sec * SAMPLE_RATE)))
        lt = np.arange(blen, dtype=np.float64) / float(SAMPLE_RATE)
        att = min(0.36, actual_bar_sec * 0.22)
        rel = min(0.42, actual_bar_sec * 0.25)
        env = np.ones_like(lt)
        n_att = max(1, int(att * SAMPLE_RATE))
        n_rel = max(1, int(rel * SAMPLE_RATE))
        if n_att < len(env):
            env[:n_att] = 0.5 * (1.0 - np.cos(np.pi * np.linspace(0.0, 1.0, n_att)))
        if n_rel < len(env):
            env[-n_rel:] = 0.5 * (1.0 + np.cos(np.pi * np.linspace(0.0, 1.0, n_rel)))

        chord_l = np.zeros_like(lt)
        chord_r = np.zeros_like(lt)
        for note_i, midi in enumerate(chord):
            f = _midi_to_hz(midi)
            det = 0.42 if (note_i % 2 == 0) else -0.42
            phase_off = 0.37 * note_i
            wl = 2.0 * np.pi * (f + det) * lt + phase_off
            wr = 2.0 * np.pi * (f - det) * lt + phase_off + 0.14
            tone_l = np.sin(wl) + 0.24 * np.sin(2.0 * wl) + 0.06 * np.sin(3.0 * wl)
            tone_r = np.sin(wr) + 0.24 * np.sin(2.0 * wr) + 0.06 * np.sin(3.0 * wr)
            amp = 0.088 / max(1.0, math.sqrt(len(chord)))
            chord_l += amp * tone_l
            chord_r += amp * tone_r

        if cfg.shimmer_level > 0:
            f_hi = _midi_to_hz(chord[-1] + 12)
            sh_env = env * (0.65 + 0.35 * np.sin(2.0 * np.pi * 0.25 * lt))
            chord_l += sh_env * 0.028 * cfg.shimmer_level * np.sin(2.0 * np.pi * (f_hi + 0.3) * lt)
            chord_r += sh_env * 0.028 * cfg.shimmer_level * np.sin(2.0 * np.pi * (f_hi - 0.3) * lt)

        f_b = _midi_to_hz(bass)
        bass_tone = 0.145 * (np.sin(2.0 * np.pi * f_b * lt) + 0.18 * np.sin(4.0 * np.pi * f_b * lt))
        _add_periodic(i0, np.column_stack([env * (chord_l + bass_tone), env * (chord_r + bass_tone)]))

        # 1b. Sparse distant felt-piano notes + long cavernous ping-pong hall tail (zero upbeat snare/kick).
        arp_indices = (0, 2, 4) if (bar_idx % 2 == 0) else (4, 2, 3)
        note_dur = min(2.20, actual_bar_sec * 0.80)
        nlen = max(128, int(round(note_dur * SAMPLE_RATE)))
        nt = np.arange(nlen, dtype=np.float64) / float(SAMPLE_RATE)
        felt_env = np.exp(-nt / max(0.55, note_dur * 0.55)) * np.minimum(1.0, nt / 0.032) * np.minimum(1.0, (note_dur - nt) / 0.06)
        for step_i, sub_frac in enumerate(sub_steps[:3]):
            c_idx = arp_indices[step_i % len(arp_indices)] % len(chord)
            oct_shift = 24 if (step_i == 2 and bar_idx % 2 == 1) else 12
            fm = _midi_to_hz(chord[c_idx] + oct_shift)
            vel = 0.056 if step_i == 0 else 0.040
            pan = 0.34 if (step_i % 2 == 0) else 0.66
            wt_l = 2.0 * np.pi * (fm + 0.22) * nt
            wt_r = 2.0 * np.pi * (fm - 0.22) * nt + 0.11
            felt_l = vel * felt_env * (0.85 * np.sin(wt_l) + 0.12 * np.sin(2.0 * wt_l))
            felt_r = vel * felt_env * (0.85 * np.sin(wt_r) + 0.12 * np.sin(2.0 * wt_r))
            note_start = i0 + int(round(sub_frac * actual_bar_sec * SAMPLE_RATE))
            _add_periodic(note_start, np.column_stack([felt_l * (1.2 - pan), felt_r * (0.2 + pan)]))
            for echo_i, (d_sec, g_echo) in enumerate(((0.42, 0.36), (0.88, 0.19))):
                echo_start = note_start + int(round(d_sec * SAMPLE_RATE))
                sig_e = np.column_stack([felt_r * g_echo, felt_l * g_echo]) if echo_i % 2 == 0 else np.column_stack([felt_l * g_echo, felt_r * g_echo])
                _add_periodic(echo_start, sig_e)

    # 2. Gentle drumless sub-bass breathing swell wrapped across cycle boundary.
    beat_sec = actual_bar_sec / 2.0
    n_swells = bars * 2
    for b in range(n_swells):
        bi = int(round(b * beat_sec * SAMPLE_RATE))
        slen = int(min(0.65, beat_sec * 0.6) * SAMPLE_RATE)
        if slen > 64:
            st = np.arange(slen, dtype=np.float64) / float(SAMPLE_RATE)
            senv = np.sin(np.pi * st / (slen / float(SAMPLE_RATE))) ** 2
            f_sub = _midi_to_hz(cfg.bass_midi[(b // 2) % len(cfg.bass_midi)])
            sub = 0.052 * senv * np.sin(2.0 * np.pi * f_sub * st)
            _add_periodic(bi, np.column_stack([sub, sub]))

    buf = np.zeros((n_samples, 2), dtype=np.float64)
    buf[:m_samples] = periodic
    if k_samples > 0:
        copy_k = min(k_samples, m_samples)
        buf[m_samples:m_samples + copy_k] = periodic[:copy_k]
    del periodic

    # 3. Spectral-shaped brown noise + rain wash + stereo droplets + subtle vinyl crackle.
    freqs = np.fft.rfftfreq(n_samples, d=1.0 / float(SAMPLE_RATE))
    if cfg.brown_level > 0 or cfg.rain_level > 0:
        for ch in range(2):
            white = rng.standard_normal(n_samples)
            spec = np.fft.rfft(white)
            if cfg.brown_level > 0:
                brown_shape = 1.0 / np.maximum(freqs, 18.0) ** 1.05
                brown_shape[0] = 0.0
                brown = np.fft.irfft(spec * brown_shape, n=n_samples)
                brown /= max(1e-9, float(np.std(brown)))
                buf[:, ch] += (0.095 * cfg.brown_level) * brown
            if cfg.rain_level > 0:
                rain_shape = (1.0 / np.maximum(freqs, 45.0) ** 0.55) * np.exp(-freqs / 4200.0)
                rain_shape[0] = 0.0
                rain_wash = np.fft.irfft(spec * rain_shape, n=n_samples)
                rain_wash /= max(1e-9, float(np.std(rain_wash)))
                buf[:, ch] += (0.090 * cfg.rain_level) * rain_wash

    if cfg.rain_level > 0.20:
        n_drops = max(12, int(round(total_sec * 18.0 * cfg.rain_level)))
        drop_len = int(0.032 * SAMPLE_RATE)
        dt = np.arange(drop_len, dtype=np.float64) / float(SAMPLE_RATE)
        for _ in range(n_drops):
            pos = int(rng.integers(0, max(1, n_samples - drop_len)))
            f_drop = float(rng.uniform(1100.0, 2900.0))
            decay = float(rng.uniform(0.006, 0.015))
            amp = float(rng.uniform(0.012, 0.035)) * cfg.rain_level
            pan = float(rng.uniform(0.2, 0.8))
            ping = amp * np.exp(-dt / decay) * np.minimum(1.0, dt / 0.0015) * np.sin(2.0 * np.pi * f_drop * dt)
            buf[pos:pos + drop_len, 0] += ping * (1.0 - pan)
            buf[pos:pos + drop_len, 1] += ping * pan

    if cfg.vinyl_level > 0:
        n_pops = max(4, int(round(total_sec * 6.0 * cfg.vinyl_level)))
        pop_len = int(0.004 * SAMPLE_RATE)
        pt = np.arange(pop_len, dtype=np.float64) / float(SAMPLE_RATE)
        for _ in range(n_pops):
            pos = int(rng.integers(0, max(1, n_samples - pop_len)))
            amp = float(rng.uniform(0.015, 0.045)) * cfg.vinyl_level
            ch = int(rng.integers(0, 2))
            buf[pos:pos + pop_len, ch] += amp * np.exp(-pt / 0.0008) * np.sin(2.0 * np.pi * 2400.0 * pt)

    # 4. Warmth low-pass via FFT (gentle roll-off above 3.8 kHz) so tones never sound harsh.
    lp_curve = 1.0 / np.sqrt(1.0 + (freqs / 3800.0) ** 4)
    for ch in range(2):
        buf[:, ch] = np.fft.irfft(np.fft.rfft(buf[:, ch]) * lp_curve, n=n_samples)

    # 5. Seamless equal-power circular crossfade over K samples.
    core = buf[:m_samples].copy()
    if k_samples > 0 and m_samples + k_samples <= len(buf):
        u = np.linspace(0.0, 1.0, k_samples, endpoint=False)
        fade_in = np.sin(0.5 * np.pi * u)[:, None]
        fade_out = np.cos(0.5 * np.pi * u)[:, None]
        core[:k_samples] = core[:k_samples] * fade_in + buf[m_samples:m_samples + k_samples] * fade_out

    # Soft pre-normalize to peak <= 0.70 (-3.1 dBFS).
    core -= np.mean(core, axis=0, keepdims=True)
    peak = float(np.max(np.abs(core)))
    if peak > 1e-9:
        core *= min(0.70 / peak, 2.5)
    pcm = np.clip(np.round(core * 32767.0), -32767, 32767).astype("<i2")

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_wav = wav_path.with_name(f"{wav_path.name}.tmp.wav")
    with wave.open(str(tmp_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    os.replace(tmp_wav, wav_path)
    del buf, core, pcm
    reclaim_memory()
    return float(m_samples) / float(SAMPLE_RATE)


def _normalize_loop_wav(raw_wav: Path, norm_wav: Path) -> None:
    import numpy as np

    with wave.open(str(raw_wav), "rb") as wf:
        if wf.getnchannels() != 2 or wf.getsampwidth() != 2 or wf.getframerate() != SAMPLE_RATE:
            raise ValueError(f"unexpected raw WAV format: {raw_wav}")
        m_samples = wf.getnframes()
        if m_samples <= 0:
            raise ValueError(f"empty raw WAV: {raw_wav}")
        raw_pcm = np.frombuffer(wf.readframes(m_samples), dtype="<i2").reshape(-1, 2)

    pre_samples = min(m_samples, int(round(4.0 * SAMPLE_RATE)))
    k_samples = min(m_samples // 3, int(round(2.0 * SAMPLE_RATE)))
    ext_pcm = np.concatenate([raw_pcm[-pre_samples:], raw_pcm, raw_pcm[:k_samples]], axis=0)

    norm_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="norm_", dir=str(norm_wav.parent)) as td:
        td_path = Path(td)
        ext_wav = td_path / "ext.wav"
        norm_ext_wav = td_path / "norm_ext.wav"
        with wave.open(str(ext_wav), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(ext_pcm.astype("<i2", copy=False).tobytes())
        del raw_pcm, ext_pcm

        run_ffmpeg(
            [
                "-i", str(ext_wav),
                "-af", "highpass=f=28,loudnorm=I=-14.6:TP=-1.5:LRA=9,alimiter=limit=0.89:level=0:attack=5:release=50,aresample=48000,aformat=channel_layouts=stereo",
                "-c:a", "pcm_s16le",
                str(norm_ext_wav),
            ],
            cwd=td,
        )

        with wave.open(str(norm_ext_wav), "rb") as wf:
            nframes = wf.getnframes()
            norm_f = np.frombuffer(wf.readframes(nframes), dtype="<i2").reshape(-1, 2).astype(np.float64) / 32768.0

    # Skip pre-roll startup transient, then Hann-crossfade steady-state tail into head so sample[-1] -> sample[0] is exact.
    if len(norm_f) >= pre_samples + m_samples:
        core = norm_f[pre_samples:pre_samples + m_samples].copy()
        avail_k = min(k_samples, len(norm_f) - (pre_samples + m_samples))
        if avail_k > 0:
            tail = norm_f[pre_samples + m_samples:pre_samples + m_samples + avail_k]
            u = np.linspace(0.0, 1.0, avail_k, endpoint=False)
            w = (0.5 * (1.0 - np.cos(np.pi * u)))[:, None]
            core[:avail_k] = core[:avail_k] * w + tail * (1.0 - w)
    else:
        core = norm_f[-m_samples:].copy()

    pcm_out = np.clip(np.round(np.clip(core, -0.89, 0.89) * 32767.0), -32767, 32767).astype("<i2")
    tmp_out = norm_wav.with_name(f"{norm_wav.name}.tmp.wav")
    try:
        with wave.open(str(tmp_out), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm_out.tobytes())
        os.replace(tmp_out, norm_wav)
    finally:
        tmp_out.unlink(missing_ok=True)
    del norm_f, core, pcm_out
    reclaim_memory()


def _probe_audio_duration(path: Path | str) -> float:
    """Probe an audio-only (or AV) file with ffprobe and return its duration in seconds."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        **background_subprocess_kwargs(low_priority=True),
    )
    if proc.returncode != 0:
        raise ValueError(f"unreadable audio file: {(proc.stderr or '')[-240:]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise ValueError("ffprobe returned invalid JSON for audio") from exc
    streams = data.get("streams") or []
    audio_stream = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"), None)
    if audio_stream is None:
        raise ValueError("uploaded audio file has no readable audio stream")
    fmt_dur = (data.get("format") or {}).get("duration") or audio_stream.get("duration") or 0.0
    try:
        dur = float(fmt_dur)
    except (TypeError, ValueError):
        dur = 0.0
    if not math.isfinite(dur) or dur < 0.5:
        raise ValueError("uploaded audio file duration is too short")
    return dur


def _build_curated_mix_wav(
    tracks: Sequence[dict],
    norm_wav: Path,
    target_seconds: float,
    *,
    seed: str = "",
    pre_ordered: bool = False,
) -> tuple[tuple[float, str], ...]:
    """Stitch user-supplied MP3/WAV tracks with crossfade + EBU R128 normalization in 1 FFmpeg call."""
    ordered = list(tracks) if pre_ordered else _order_curated_tracks(tracks, seed=seed, target_seconds=target_seconds)
    if not ordered:
        raise ValueError("no curated tracks available")

    durations: list[float] = []
    for tr in ordered:
        durations.append(max(2.0, _probe_audio_duration(tr["path"])))

    # Slowed (~0.86x) + cavernous stereo hall reverb so curated tracks feel nocturnal & hypnotic.
    slow_ratio = 0.86
    durations = [d / slow_ratio for d in durations]
    xf = min(4.5, min(durations) * 0.25)
    norm_chain = (
        f"asetrate={SAMPLE_RATE}*{slow_ratio:.2f},aresample={SAMPLE_RATE},"
        "highpass=f=28,lowpass=f=3600,aecho=0.8:0.86:55|115:0.36|0.22,"
        f"afade=t=in:st=0:d=0.45,loudnorm=I=-14.6:TP=-1.5:LRA=9,"
        f"alimiter=limit=0.89:level=0:attack=5:release=50,"
        f"aresample={SAMPLE_RATE},aformat=channel_layouts=stereo"
    )
    norm_wav.parent.mkdir(parents=True, exist_ok=True)

    args: list[str] = []
    for tr in ordered:
        args.extend(["-i", str(tr["path"])])
    if len(ordered) == 1:
        args.extend(["-af", norm_chain, "-c:a", "pcm_s16le", str(norm_wav)])
    else:
        filters: list[str] = []
        prev_lbl = "0:a"
        for i in range(1, len(ordered)):
            out_lbl = "mixed" if i == len(ordered) - 1 else f"ax{i}"
            filters.append(f"[{prev_lbl}][{i}:a]acrossfade=d={xf:.2f}:c1=tri:c2=tri[{out_lbl}]")
            prev_lbl = out_lbl
        filters.append(f"[{prev_lbl}]{norm_chain}[aout]")
        args.extend(["-filter_complex", ";".join(filters), "-map", "[aout]", "-c:a", "pcm_s16le", str(norm_wav)])

    run_ffmpeg(args, cwd=str(norm_wav.parent))

    chapters: list[tuple[float, str]] = []
    cursor = 0.0
    step = 0
    while cursor < max(1.0, target_seconds - 8.0) and step < 120:
        idx = step % len(ordered)
        chapters.append((round(cursor, 2), str(ordered[idx]["name"])))
        eff = max(1.5, durations[idx] - (xf if len(ordered) > 1 else 0.0))
        cursor += eff
        step += 1
    return tuple(chapters or [(0.0, str(ordered[0]["name"]))])


def _vertical_gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> "Image.Image":
    from PIL import Image

    w, h = size
    strip = Image.new("RGB", (1, h))
    px = strip.load()
    denom = max(1, h - 1)
    for y in range(h):
        a = y / denom
        px[0, y] = (
            int(round(top[0] + (bottom[0] - top[0]) * a)),
            int(round(top[1] + (bottom[1] - top[1]) * a)),
            int(round(top[2] + (bottom[2] - top[2]) * a)),
        )
    return strip.resize((w, h), Image.Resampling.BILINEAR)


def _pil_font(size: int, text: str = ""):
    from PIL import ImageFont

    fpath = resolve_font("", text)
    if fpath:
        try:
            return ImageFont.truetype(fpath, size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_scene_image(
    preset: str,
    *,
    seed: str = "",
    brand_name: str = "",
    size: tuple[int, int] = (2048, 1152),
) -> "Image.Image":
    """Render an atmospheric 16:9 background scene completely in Pillow (zero cv2/numpy)."""
    from PIL import Image, ImageDraw, ImageFilter

    cfg = _get_preset(preset)
    w, h = size
    rng = random.Random(_seed_int(seed or cfg.key))
    img = _vertical_gradient((w, h), cfg.sky_top, cfg.sky_bottom).convert("RGBA")

    # Atmospheric radial horizon & moon/lamp bloom
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gx = int(w * (0.72 if cfg.scene_style in ("study_desk", "starry_horizon") else 0.52))
    gy = int(h * (0.44 if cfg.scene_style == "starry_horizon" else 0.62))
    gr = int(min(w, h) * 0.42)
    gd.ellipse((gx - gr, gy - gr, gx + gr, gy + gr), fill=(*cfg.glow_color, 130))
    gd.ellipse((0, int(h * 0.58), w, int(h * 1.15)), fill=(*cfg.glow_color, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=max(24, w // 28)))
    img = Image.alpha_composite(img, glow)

    # Soft bokeh orbs & stars
    bokeh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bokeh)
    if cfg.scene_style == "starry_horizon":
        for _ in range(110):
            sx, sy = rng.randint(20, w - 20), rng.randint(18, int(h * 0.68))
            sr = rng.randint(1, 3)
            sa = rng.randint(110, 235)
            bd.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=(240, 245, 255, sa))
        mx, my, mr = int(w * 0.74), int(h * 0.24), int(min(w, h) * 0.065)
        bd.ellipse((mx - mr, my - mr, mx + mr, my + mr), fill=(240, 246, 255, 225))
    for _ in range(18):
        bx, by = rng.randint(0, w), rng.randint(int(h * 0.2), int(h * 0.85))
        br = rng.randint(max(12, w // 90), max(28, w // 32))
        col = cfg.accent_color if rng.random() < 0.6 else cfg.glow_color
        bd.ellipse((bx - br, by - br, bx + br, by + br), fill=(*col, rng.randint(28, 70)))
    bokeh = bokeh.filter(ImageFilter.GaussianBlur(radius=max(4, w // 240)))
    img = Image.alpha_composite(img, bokeh)

    # Preset-specific foreground architecture / nature silhouettes (keeps mean luma well above black)
    fg = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fg)
    if cfg.scene_style == "rooftop_window":
        # Distant skyline + warm lit windows + rooftop ledge
        x = 0
        while x < w:
            bw = rng.randint(int(w * 0.055), int(w * 0.11))
            bh = rng.randint(int(h * 0.22), int(h * 0.50))
            top_y = h - bh
            fd.rectangle((x, top_y, x + bw - 6, h), fill=(18, 15, 32, 235))
            win_w, win_h = max(8, w // 140), max(12, h // 75)
            for wy in range(top_y + 24, h - 70, win_h * 2):
                for wx in range(x + 14, x + bw - 22, win_w * 2):
                    if rng.random() < 0.34:
                        fd.rectangle((wx, wy, wx + win_w, wy + win_h), fill=(*cfg.accent_color, rng.randint(120, 210)))
            x += bw
        fd.rectangle((0, int(h * 0.86), w, h), fill=(14, 11, 24, 248))
        fd.line((0, int(h * 0.86), w, int(h * 0.86)), fill=(*cfg.glow_color, 110), width=3)
    elif cfg.scene_style == "rainy_glass":
        # Misty city silhouette + window frame panes
        for i in range(14):
            cx = int(w * (i + 0.5) / 14)
            ch = rng.randint(int(h * 0.25), int(h * 0.52))
            cw = rng.randint(int(w * 0.05), int(w * 0.10))
            fd.rectangle((cx - cw // 2, h - ch, cx + cw // 2, h), fill=(12, 22, 34, 215))
        # Window mullions
        frame_col = (10, 18, 28, 235)
        fd.rectangle((0, 0, w, int(h * 0.04)), fill=frame_col)
        fd.rectangle((0, int(h * 0.88), w, h), fill=frame_col)
        fd.rectangle((int(w * 0.33), 0, int(w * 0.345), h), fill=frame_col)
        fd.rectangle((int(w * 0.66), 0, int(w * 0.675), h), fill=frame_col)
        fd.line((0, int(h * 0.88), w, int(h * 0.88)), fill=(*cfg.accent_color, 130), width=4)
    elif cfg.scene_style == "starry_horizon":
        # Layered mountain ridges + calm lake reflection
        ridge1 = [(0, int(h * 0.72))]
        for x in range(0, w + 120, 120):
            ridge1.append((x, int(h * 0.60 + rng.randint(-55, 55))))
        ridge1.extend([(w, h), (0, h)])
        fd.polygon(ridge1, fill=(14, 21, 44, 220))
        ridge2 = [(0, int(h * 0.80))]
        for x in range(0, w + 100, 100):
            ridge2.append((x, int(h * 0.74 + rng.randint(-35, 35))))
        ridge2.extend([(w, h), (0, h)])
        fd.polygon(ridge2, fill=(9, 14, 30, 240))
        # Subtle moonlight water streaks
        for i in range(12):
            ry = int(h * 0.83 + i * (h * 0.013))
            rw = int(w * (0.18 - i * 0.01))
            rx = int(w * 0.74) - rw // 2
            fd.line((rx, ry, rx + rw, ry), fill=(*cfg.accent_color, max(35, 135 - i * 9)), width=3)
    else:  # study_desk
        # Night window + warm desk surface + desk lamp silhouette
        fd.rectangle((int(w * 0.09), int(h * 0.10), int(w * 0.56), int(h * 0.68)), outline=(48, 42, 42, 220), width=16)
        fd.line((int(w * 0.325), int(h * 0.10), int(w * 0.325), int(h * 0.68)), fill=(48, 42, 42, 220), width=10)
        fd.rectangle((0, int(h * 0.76), w, h), fill=(26, 20, 20, 245))
        fd.line((0, int(h * 0.76), w, int(h * 0.76)), fill=(*cfg.glow_color, 130), width=4)
        # Warm desk lamp shade & beam on right
        lx, ly = int(w * 0.76), int(h * 0.38)
        fd.polygon([(lx - 70, ly + 40), (lx + 70, ly + 40), (lx + 240, int(h * 0.76)), (lx - 240, int(h * 0.76))],
                   fill=(*cfg.glow_color, 46))
        fd.polygon([(lx - 75, ly + 42), (lx + 75, ly + 42), (lx + 42, ly - 16), (lx - 42, ly - 16)],
                   fill=(38, 30, 32, 245))
        fd.rectangle((lx - 8, ly + 42, lx + 8, int(h * 0.76)), fill=(32, 26, 28, 245))

    img = Image.alpha_composite(img, fg)

    if brand_name.strip():
        bd_brand = ImageDraw.Draw(img)
        font = _pil_font(max(22, w // 64), brand_name)
        text = brand_name.strip()
        bbox = bd_brand.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        pad_x = max(140, int(w * 0.075))
        pad_y = max(84, int(h * 0.075))
        bd_brand.text((w - tw - pad_x, pad_y), text, font=font, fill=(245, 245, 250, 165))

    return img.convert("RGB")


def _render_particle_sheet(preset: str, out_path: Path, *, seed: str = "") -> None:
    """Create a vertically tileable 1920x2160 RGBA sheet of fine rain mist or soft bokeh dust motes."""
    from PIL import Image, ImageDraw, ImageFilter

    cfg = _get_preset(preset)
    rng = random.Random(_seed_int(f"particles:{seed or cfg.key}"))
    tile = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)

    if cfg.rain_level >= 0.30:
        count = int(85 + 160 * cfg.rain_level)
        for _ in range(count):
            x = rng.randint(0, WIDTH - 1)
            y = rng.randint(0, HEIGHT - 1)
            length = rng.randint(16, 38)
            dx = -3
            alpha = rng.randint(20, 68)
            col = (*cfg.accent_color, alpha)
            for dy_wrap in (0, -HEIGHT, HEIGHT):
                draw.line((x, y + dy_wrap, x + dx, y + dy_wrap + length), fill=col, width=1)
    else:
        for _ in range(52):
            x = rng.randint(0, WIDTH - 1)
            y = rng.randint(0, HEIGHT - 1)
            r = rng.randint(2, 4)
            alpha = rng.randint(18, 54)
            for dy_wrap in (0, -HEIGHT, HEIGHT):
                draw.ellipse((x - r, y + dy_wrap - r, x + r, y + dy_wrap + r), fill=(*cfg.accent_color, alpha))
        tile = tile.filter(ImageFilter.GaussianBlur(radius=1.4))

    sheet = Image.new("RGBA", (WIDTH, HEIGHT * 2), (0, 0, 0, 0))
    sheet.paste(tile, (0, 0))
    sheet.paste(tile, (0, HEIGHT))
    sheet.save(out_path, format="PNG")


def _save_jpeg_under_limit(im: "Image.Image", dest: Path) -> str:
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    rgb = im.convert("RGB")
    if rgb.size != (THUMB_W, THUMB_H):
        rgb = rgb.resize((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
    for q in (92, 86, 78, 70):
        tmp = dest.with_name(f"{dest.stem}.tmp.jpg")
        rgb.save(tmp, format="JPEG", quality=q, optimize=True)
        if tmp.stat().st_size <= _MAX_THUMB_BYTES or q == 70:
            os.replace(tmp, dest)
            return str(dest)
        tmp.unlink(missing_ok=True)
    return str(dest)


def _short_headline(title: str, fallback: str, *, max_words: int = 12) -> str:
    cleaned = title.split("[")[0].split("|")[0].split("—")[0].strip()
    words = [w for w in cleaned.split() if not w.startswith("#")][:max_words]
    return " ".join(words) or fallback


def _wrap_lines(text: str, font, max_w: int, draw, *, max_lines: int = 3) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        bb = draw.textbbox((0, 0), cand, font=font)
        if (bb[2] - bb[0]) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:max_lines]


def generate_ambient_thumbnails(
    preset: str,
    *,
    out_dir: Path,
    title: str,
    duration_sec: float = 1800.0,
    brand_name: str = "",
    seed: str = "",
    portrait_path: Optional[Path | str] = None,
) -> tuple[list[dict], str, str]:
    """Generate hero_raw.jpg + 3 distinct 1280x720 portrait/cinema thumbnails (plus optional AI v4)."""
    from PIL import Image, ImageEnhance

    cfg = _get_preset(preset)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    resolved_portrait = _resolve_portrait(cfg.key, portrait_path)
    if resolved_portrait is not None:
        scene = _prepare_portrait_canvas(resolved_portrait, size=(THUMB_W, THUMB_H), brand_name="")
    else:
        scene = render_scene_image(cfg.key, seed=seed or title, brand_name="", size=(THUMB_W, THUMB_H))
    hero_path = _save_jpeg_under_limit(scene, out_dir / "hero_raw.jpg")

    variants: list[dict] = []

    # v1: Cold Midnight Cobalt (zero text — pure atmospheric cobalt/indigo night memory matching @songsforcry / @nowayout.8)
    v1 = ImageEnhance.Color(scene).enhance(0.90).convert("RGBA")
    v1 = Image.alpha_composite(v1, Image.new("RGBA", (THUMB_W, THUMB_H), (10, 22, 64, 52)))
    variants.append({"id": "v1", "label": "Midnight Cobalt", "path": _save_jpeg_under_limit(v1, out_dir / "thumb_v1.jpg")})

    # v2: VHS Violet Memory (closer crop + deep indigo/purple cast, zero text)
    crop_w, crop_h = int(THUMB_W * 0.88), int(THUMB_H * 0.88)
    dx = (THUMB_W - crop_w) // 2
    dy = int((THUMB_H - crop_h) * 0.28)
    v2 = scene.crop((dx, dy, dx + crop_w, dy + crop_h)).resize((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
    v2 = ImageEnhance.Contrast(v2).enhance(1.12).convert("RGBA")
    v2 = Image.alpha_composite(v2, Image.new("RGBA", (THUMB_W, THUMB_H), (42, 18, 92, 62)))
    variants.append({"id": "v2", "label": "VHS Memory", "path": _save_jpeg_under_limit(v2, out_dir / "thumb_v2.jpg")})

    # v3: Deep Twilight (moody high-contrast silhouette vignette, pure zero-text)
    v3 = ImageEnhance.Brightness(scene).enhance(0.84)
    v3 = ImageEnhance.Contrast(v3).enhance(1.22).convert("RGBA")
    v3 = Image.alpha_composite(v3, Image.new("RGBA", (THUMB_W, THUMB_H), (8, 16, 48, 70)))
    scrim3 = Image.new("RGBA", (1, THUMB_H), (0, 0, 0, 0))
    s3_px = scrim3.load()
    for y in range(int(THUMB_H * 0.52), THUMB_H):
        frac = (y - int(THUMB_H * 0.52)) / max(1, THUMB_H - int(THUMB_H * 0.52))
        s3_px[0, y] = (4, 6, 16, int(round(170 * (frac ** 1.4))))
    v3 = Image.alpha_composite(v3, scrim3.resize((THUMB_W, THUMB_H), Image.Resampling.BILINEAR))
    variants.append({"id": "v3", "label": "Deep Twilight", "path": _save_jpeg_under_limit(v3, out_dir / "thumb_v3.jpg")})

    prompt = (
        f"Candid blurry 2000s midnight Sony digicam portrait of an alluring goth alt baddie woman in cold dark cobalt blue and violet twilight lighting "
        f"({cfg.mood_summary}), intimate eye contact like @imissher.ambient and @istillmissh3rr, "
        f"subtle low-light ISO film grain, soft out-of-focus background bokeh, zero AI gloss, zero text overlays, zero watermarks"
    )
    try:
        from app.longform import thumbnails as lf_thumbs

        ai_res = lf_thumbs.generate_ai_thumbnail(hero_path, prompt, out_dir / "thumb_v4.jpg")
        if isinstance(ai_res, tuple) and len(ai_res) == 2:
            ai_label, ai_path = ai_res
        else:
            ai_label, ai_path = "AI Enhanced", ai_res
        if isinstance(ai_path, (str, os.PathLike)) and ai_path and os.path.isfile(ai_path):
            variants.append({"id": "v4", "label": str(ai_label or "AI Enhanced"), "path": str(ai_path)})
    except Exception as exc:
        log.debug("ambient: optional AI thumbnail skipped: %s", exc)

    return variants, prompt, hero_path


def generate_ambient_video(
    preset: str = "lofi_chill",
    output_path: str = "",
    *,
    target_minutes: float = 30.0,
    duration_sec: Optional[float] = None,
    brand_name: str = "",
    theme: str = "",
    part_number: Optional[int] = None,
    seed: Optional[str] = None,
    title: Optional[str] = None,
    thumbs_dir: Optional[Path] = None,
    chapter_interval_sec: Optional[float] = None,
    language: str = "en",
    portrait_path: Optional[Path | str] = None,
) -> AmbientResult:
    """Generate a 16:9 1080p 30fps claim-free animated portrait or ambient MP4, thumbnails, and metadata."""
    cfg = _get_preset(preset)
    if not output_path:
        raise ValueError("output_path is required")
    if duration_sec is not None:
        target_seconds = float(duration_sec)
        if not math.isfinite(target_seconds) or target_seconds < 1.0:
            raise ValueError("duration_sec must be finite and at least 1.0 second")
    else:
        mins = float(target_minutes)
        if not math.isfinite(mins) or mins <= 0.0:
            raise ValueError("target_minutes must be finite and greater than 0")
        target_seconds = mins * 60.0
        if target_seconds < 1.0:
            raise ValueError("duration must be at least 1.0 second")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(f"{out.stem}.tmp{out.suffix or '.mp4'}")
    job_seed = seed or f"{cfg.key}:{out.stem}:{theme}:{part_number or 1}"
    scene_portraits = _resolve_preset_portraits(cfg.key, portrait_path)
    resolved_portrait = scene_portraits[0] if scene_portraits else None
    overlay_mp4 = _resolve_overlay(cfg.key)
    tracks = list_ambient_tracks(cfg.key, include_siblings=target_seconds > DEFAULT_LOOP_SEC)
    credits_lines: tuple[str, ...] = ()

    chapters_override: Optional[tuple[tuple[float, str], ...]] = None
    try:
        with tempfile.TemporaryDirectory(prefix="ambient_") as td:
            tmp = Path(td)
            raw_wav = tmp / "raw_loop.wav"
            norm_wav = tmp / "norm_loop.wav"
            particles_png = tmp / "particles_2160.png"

            if tracks:
                ordered_tracks = _order_curated_tracks(tracks, seed=job_seed, target_seconds=target_seconds)
                chapters_override = _build_curated_mix_wav(ordered_tracks, norm_wav, target_seconds, seed=job_seed, pre_ordered=True)
                credits_lines = _resolve_track_credits_for(ordered_tracks)
            else:
                audio_cycle = min(target_seconds, 12.8 if cfg.bpm == 75.0 else DEFAULT_LOOP_SEC)
                if target_seconds <= DEFAULT_LOOP_SEC:
                    audio_cycle = max(2.0, target_seconds)
                _synthesize_raw_loop_wav(cfg.key, raw_wav, cycle_sec=audio_cycle, seed=job_seed)
                _normalize_loop_wav(raw_wav, norm_wav)

            scene_pngs: list[Path] = []
            if scene_portraits:
                for idx, p_file in enumerate(scene_portraits[:4]):
                    sp = tmp / f"scene_{idx}.png"
                    _prepare_portrait_canvas(p_file, size=(WIDTH, HEIGHT), brand_name=brand_name).save(sp, format="PNG")
                    scene_pngs.append(sp)
            else:
                sp = tmp / "scene_0.png"
                render_scene_image(cfg.key, seed=job_seed, brand_name=brand_name, size=(WIDTH, HEIGHT)).save(sp, format="PNG")
                scene_pngs.append(sp)

            if overlay_mp4 is None:
                _render_particle_sheet(cfg.key, particles_png, seed=job_seed)

            fade_in = min(0.60, target_seconds * 0.15)
            fade_out = min(0.80, target_seconds * 0.20)
            fade_out_st = max(0.05, target_seconds - fade_out)
            af_chain = (
                f"afade=t=in:st=0:d={fade_in:.2f},"
                f"afade=t=out:st={fade_out_st:.3f}:d={fade_out:.2f},"
                "alimiter=limit=0.79:level=0:attack=5:release=50"
            )

            def _overlay_tail(base_lbl: str, ov_idx: int) -> str:
                if overlay_mp4 is not None:
                    return (
                        f"[{base_lbl}]format=rgb24[basergb];"
                        f"[{ov_idx}:v]fps={FPS},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                        f"crop={WIDTH}:{HEIGHT},setsar=1,format=rgb24[ov];"
                        "[basergb][ov]blend=all_mode=screen:all_opacity=0.12:shortest=1,"
                        "noise=alls=2:allf=t,vignette=PI/6,format=yuv420p[vout]"
                    )
                return (
                    f"[{ov_idx}:v]crop={WIDTH}:{HEIGHT}:0:'{HEIGHT}-mod(t*{HEIGHT}*2/{DEFAULT_LOOP_SEC:.1f},{HEIGHT})'[pt];"
                    f"[{base_lbl}][pt]overlay=0:0:shortest=1,noise=alls=2:allf=t,vignette=PI/6,format=yuv420p[vout]"
                )

            if target_seconds <= DEFAULT_LOOP_SEC:
                ov_args = (
                    ["-stream_loop", "-1", "-i", str(overlay_mp4)]
                    if overlay_mp4 is not None
                    else ["-loop", "1", "-framerate", str(FPS), "-t", f"{target_seconds:.3f}", "-i", str(particles_png)]
                )
                vf_chain = f"[0:v]setsar=1[bg];{_overlay_tail('bg', 1)}"
                run_ffmpeg(
                    [
                        "-loop", "1", "-framerate", str(FPS), "-t", f"{target_seconds:.3f}", "-i", str(scene_pngs[0]),
                        *ov_args,
                        "-stream_loop", "-1", "-i", str(norm_wav),
                        "-filter_complex", vf_chain,
                        "-map", "[vout]", "-map", "2:a:0",
                        "-t", f"{target_seconds:.3f}",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                        "-pix_fmt", "yuv420p", "-r", str(FPS), "-video_track_timescale", "30000",
                        "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", "2",
                        "-af", af_chain,
                        "-movflags", "+faststart",
                        str(partial),
                    ],
                    cwd=td,
                )
            else:
                loop_v = tmp / "loop_v.mp4"
                loop_inputs: list[str] = []
                if len(scene_pngs) >= 2:
                    hold_sec = 24.0
                    fade_d = 3.0
                    n_scenes = len(scene_pngs)
                    loop_sec = n_scenes * hold_sec
                    loop_inputs.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{hold_sec:.3f}", "-i", str(scene_pngs[0])])
                    for sp in scene_pngs[1:]:
                        loop_inputs.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{hold_sec + fade_d:.3f}", "-i", str(sp)])
                    # Wrap back smoothly into scene 0 at end of loop cycle
                    loop_inputs.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{fade_d:.3f}", "-i", str(scene_pngs[0])])
                    ov_idx = n_scenes + 1
                    f_parts = ["[0:v]setsar=1[x0]"]
                    for j in range(1, n_scenes + 1):
                        in_lbl = f"[{j}:v]"
                        f_parts.append(f"{in_lbl}setsar=1[s{j}]")
                        off = j * hold_sec - fade_d
                        f_parts.append(f"[x{j-1}][s{j}]xfade=transition=fade:duration={fade_d:.1f}:offset={off:.3f}[x{j}]")
                    f_parts.append(_overlay_tail(f"x{n_scenes}", ov_idx))
                    vf_loop = ";".join(f_parts)
                else:
                    loop_sec = DEFAULT_LOOP_SEC
                    ov_idx = 1
                    loop_inputs.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{loop_sec:.3f}", "-i", str(scene_pngs[0])])
                    vf_loop = f"[0:v]setsar=1[bg];{_overlay_tail('bg', 1)}"

                ov_args = (
                    ["-stream_loop", "-1", "-i", str(overlay_mp4)]
                    if overlay_mp4 is not None
                    else ["-loop", "1", "-framerate", str(FPS), "-t", f"{loop_sec:.3f}", "-i", str(particles_png)]
                )
                run_ffmpeg(
                    [
                        *loop_inputs,
                        *ov_args,
                        "-filter_complex", vf_loop,
                        "-map", "[vout]", "-an",
                        "-t", f"{loop_sec:.3f}",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS), "-keyint_min", str(FPS),
                        "-sc_threshold", "0", "-video_track_timescale", "30000",
                        str(loop_v),
                    ],
                    cwd=td,
                )
                run_ffmpeg(
                    [
                        "-stream_loop", "-1", "-i", str(loop_v),
                        "-stream_loop", "-1", "-i", str(norm_wav),
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-t", f"{target_seconds:.3f}",
                        "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", "2",
                        "-af", af_chain,
                        "-movflags", "+faststart",
                        str(partial),
                    ],
                    cwd=td,
                )

            info = probe(str(partial))
            os.replace(partial, out)
    finally:
        partial.unlink(missing_ok=True)

    meta = build_ambient_metadata(
        cfg.key,
        duration_sec=float(info.duration or target_seconds),
        title=title,
        brand_name=brand_name,
        theme=theme,
        seed=job_seed,
        chapter_interval_sec=chapter_interval_sec,
        language=language,
        include_chapters=False,
        chapters_override=chapters_override,
        credits_lines=credits_lines,
    )

    resolved_thumbs_dir = Path(thumbs_dir) if thumbs_dir is not None else (out.parent / f"{out.stem}_thumbs")
    thumbs, thumb_prompt, hero_frame = generate_ambient_thumbnails(
        cfg.key,
        out_dir=resolved_thumbs_dir,
        title=meta.title,
        duration_sec=info.duration or target_seconds,
        brand_name=brand_name,
        seed=job_seed,
        portrait_path=resolved_portrait,
    )

    reclaim_memory()
    return AmbientResult(
        output_path=str(out),
        preset=cfg.key,
        duration=float(info.duration or target_seconds),
        width=int(info.width or WIDTH),
        height=int(info.height or HEIGHT),
        fps=int(round(info.fps or FPS)),
        title=meta.title,
        description=meta.description,
        tags=meta.tags,
        chapters=meta.chapters,
        chapter_text=meta.chapter_text,
        thumbnails=tuple(thumbs),
        hero_frame=hero_frame,
        thumbnail_prompt=thumb_prompt,
        language=meta.language,
        theme=theme or "",
        part_number=int(part_number or 1),
        portrait_path=str(resolved_portrait or ""),
    )
