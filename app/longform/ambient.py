"""Claim-free procedural 16:9 Lofi, Rain, Night Ambient, and Deep Study video generator."""
from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from app.clipper.media import probe, run_ffmpeg
from app.metadata import compose, finished_title
from app.transformers.fonts import resolve_font
from app.winproc import reclaim_memory

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
    warnings: tuple[str, ...] = ()

    @property
    def duration_sec(self) -> int:
        return int(round(self.duration))


AMBIENT_PRESETS: dict[str, AmbientPreset] = {
    "lofi_chill": AmbientPreset(
        key="lofi_chill",
        label="Lofi Chill Beats",
        bpm=75.0,
        musical_key="F maj9",
        chords_midi=(
            (53, 57, 60, 64, 67),
            (52, 55, 59, 62, 67),
            (50, 53, 57, 60, 64),
            (48, 52, 55, 59, 62),
        ),
        bass_midi=(41, 40, 38, 36),
        pulse_mode="lofi_beat",
        rain_level=0.14,
        vinyl_level=0.22,
        brown_level=0.20,
        shimmer_level=0.14,
        sky_top=(24, 19, 43),
        sky_bottom=(74, 43, 75),
        glow_color=(255, 175, 95),
        accent_color=(255, 206, 138),
        scene_style="rooftop_window",
        mood_summary="Warm late-night lofi Rhodes chords, soft tape texture, and gentle rooftop rain.",
        title_templates=(
            "Late Night Lofi Beats to Study & Relax [{dur}]",
            "Warm Rooftop Lofi Chill Mix for Focus [{dur}]",
            "Midnight Lofi Study Session — Calm Beats [{dur}]",
        ),
        tags=(
            "lofi chill beats", "lofi study music", "chillhop instrumental", "late night lofi",
            "study beats", "relaxing lofi", "background music", "coding music",
            "reading ambience", "calm instrumentals", "sleep lofi", "cozy night vibes",
            "focus music", "no copyright lofi", "ambient lofi",
        ),
    ),
    "rain_focus": AmbientPreset(
        key="rain_focus",
        label="Rainy Focus Ambience",
        bpm=60.0,
        musical_key="D min9",
        chords_midi=(
            (50, 53, 57, 60, 64),
            (46, 50, 53, 57, 62),
            (43, 46, 50, 53, 57),
            (45, 50, 52, 57, 60),
        ),
        bass_midi=(38, 34, 31, 33),
        pulse_mode="rain_swell",
        rain_level=0.65,
        vinyl_level=0.06,
        brown_level=0.40,
        shimmer_level=0.12,
        sky_top=(14, 26, 43),
        sky_bottom=(31, 59, 77),
        glow_color=(127, 209, 232),
        accent_color=(172, 232, 247),
        scene_style="rainy_glass",
        mood_summary="Steady stereo rain on glass layered over soft warm ambient chords for deep concentration.",
        title_templates=(
            "Rain on Window & Soft Ambient Pads for Deep Focus [{dur}]",
            "Cozy Night Rain Ambience to Study, Code & Sleep [{dur}]",
            "Calm Rainy Evening Focus Soundscape [{dur}]",
        ),
        tags=(
            "rain ambience", "rain sounds focus", "rainy night study", "calm rain music",
            "ambient rain", "sleep rain sounds", "deep concentration", "white noise rain",
            "cozy rain window", "reading rain", "work focus music", "relaxing rainstorm",
            "coding ambience", "stress relief sound", "original soundscape",
        ),
    ),
    "night_ambient": AmbientPreset(
        key="night_ambient",
        label="Midnight Starfield Ambient",
        bpm=60.0,
        musical_key="C min9",
        chords_midi=(
            (48, 51, 55, 58, 62),
            (44, 48, 51, 55, 60),
            (41, 44, 48, 51, 55),
            (43, 48, 50, 55, 58),
        ),
        bass_midi=(36, 32, 29, 31),
        pulse_mode="nocturne_drone",
        rain_level=0.06,
        vinyl_level=0.05,
        brown_level=0.28,
        shimmer_level=0.36,
        sky_top=(7, 11, 24),
        sky_bottom=(23, 34, 68),
        glow_color=(196, 215, 255),
        accent_color=(218, 230, 255),
        scene_style="starry_horizon",
        mood_summary="Slow nocturnal synth pads, subtle shimmer harmonics, and tranquil midnight horizons.",
        title_templates=(
            "Midnight Starfield Ambient — Deep Sleep & Reflection [{dur}]",
            "Quiet Night Sky Ambient Pads for Calm Focus [{dur}]",
            "Slow Nocturne Soundscape Under the Stars [{dur}]",
        ),
        tags=(
            "night ambient music", "sleep ambient", "starfield soundscape", "midnight calm",
            "space ambient", "meditation pads", "deep rest music", "nocturne ambient",
            "calm night sounds", "quiet study music", "atmospheric drone", "mindfulness audio",
            "relaxation music", "peaceful night", "ambient soundscape",
        ),
    ),
    "deep_study": AmbientPreset(
        key="deep_study",
        label="Deep Study Flow",
        bpm=75.0,
        musical_key="A min9",
        chords_midi=(
            (45, 48, 52, 55, 59),
            (41, 45, 48, 52, 57),
            (48, 52, 55, 59, 64),
            (43, 47, 50, 52, 59),
        ),
        bass_midi=(33, 29, 36, 31),
        pulse_mode="study_pulse",
        rain_level=0.16,
        vinyl_level=0.10,
        brown_level=0.34,
        shimmer_level=0.20,
        sky_top=(13, 27, 34),
        sky_bottom=(42, 33, 31),
        glow_color=(245, 194, 107),
        accent_color=(255, 221, 158),
        scene_style="study_desk",
        mood_summary="Balanced warm felt-piano chords, subtle room ambience, and steady study flow.",
        title_templates=(
            "Deep Study Flow — Warm Ambient Music for Work [{dur}]",
            "Late Night Desk Lamp Study Session [{dur}]",
            "Distraction-Free Ambient Flow for Reading & Coding [{dur}]",
        ),
        tags=(
            "deep study music", "study flow", "focus instrumental", "productivity music",
            "coding soundtrack", "reading music", "warm ambient", "pomodoro music",
            "concentration beats", "calm desk ambience", "flow state audio", "study session",
            "minimalist ambient", "relaxing focus", "original ambient",
        ),
    ),
}

_CHAPTER_LABELS: dict[str, tuple[str, ...]] = {
    "lofi_chill": ("Midnight Rooftop", "Warm Tape Drift", "Neon Twilight", "Quiet Alley Rain", "Amber Horizon", "Late Espresso"),
    "rain_focus": ("First Drops", "Window Mist", "Steady Downpour", "Deep Concentration", "Quiet Streetlights", "Calm Afterglow"),
    "night_ambient": ("Starlight Drift", "Moonlit Ridge", "Deep Orbit", "Quiet Horizon", "Still Waters", "Dawn Whisper"),
    "deep_study": ("Desk Lamp Glow", "Quiet Pages", "Flow State", "Second Wind", "Clear Mind", "Closing Notes"),
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
) -> AmbientMetadata:
    """Build claim-free SEO metadata and chapters (no #Shorts, no viral/trending filler)."""
    cfg = _get_preset(preset)
    dur_badge = _duration_badge(duration_sec)
    idx = _seed_int(seed or f"{cfg.key}:{int(round(duration_sec))}") % len(cfg.title_templates)
    raw_title = (title or "").strip()
    if not raw_title:
        base = cfg.title_templates[idx].format(dur=dur_badge)
        if theme and theme.strip():
            raw_title = f"{theme.strip()} — {base}"
        else:
            raw_title = base

    interval = float(chapter_interval_sec) if chapter_interval_sec else (300.0 if duration_sec >= 900.0 else max(15.0, round(duration_sec / 4.0)))
    labels = _CHAPTER_LABELS.get(cfg.key, ("Drift", "Focus", "Calm", "Night"))
    chapters: list[tuple[float, str]] = [(0.0, f"Intro — {cfg.label}")]
    t = interval
    step_i = 0
    while t < max(0.0, duration_sec - 8.0):
        chapters.append((round(t, 2), labels[step_i % len(labels)]))
        t += interval
        step_i += 1
    chapter_text = _format_chapters(chapters)

    brand_line = f"Created for {brand_name.strip()}." if brand_name.strip() else ""
    theme_line = f"Theme: {theme.strip()}." if theme.strip() else ""
    body_parts = [
        cfg.mood_summary,
        theme_line,
        "100% original procedural audio & visuals — claim-free and safe for study, coding, reading, and sleep.",
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
    if include_chapters and chapter_text and "Chapters:" not in clean_desc:
        clean_desc = f"{clean_desc}\n\nChapters:\n{chapter_text}"
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

    buf = np.zeros((n_samples, 2), dtype=np.float64)
    actual_bar_sec = target_cycle / float(bars)

    # 1. Warm Rhodes/felt chord pads + deep bass (periodic across target_cycle so tail matches head).
    for bar_idx, (chord, bass) in enumerate(zip(cfg.chords_midi, cfg.bass_midi)):
        # Cover bar_idx and wrap bar 0 into the crossfade tail.
        repeats = (bar_idx,) if bar_idx > 0 else (0, bars)
        for rep in repeats:
            t0 = rep * actual_bar_sec
            t1 = (rep + 1) * actual_bar_sec
            if t0 >= total_sec:
                continue
            i0 = max(0, int(round(t0 * SAMPLE_RATE)))
            i1 = min(n_samples, int(round(t1 * SAMPLE_RATE)))
            if i1 <= i0:
                continue
            lt = t[i0:i1] - t0
            dur = float(i1 - i0) / float(SAMPLE_RATE)
            att = min(0.42, dur * 0.25)
            rel = min(0.50, dur * 0.28)
            env = np.ones_like(lt)
            n_att = max(1, int(att * SAMPLE_RATE))
            n_rel = max(1, int(rel * SAMPLE_RATE))
            if n_att < len(env):
                env[:n_att] = 0.5 * (1.0 - np.cos(np.pi * np.linspace(0.0, 1.0, n_att)))
            if n_rel < len(env):
                env[-n_rel:] = 0.5 * (1.0 + np.cos(np.pi * np.linspace(0.0, 1.0, n_rel)))

            chord_sig_l = np.zeros_like(lt)
            chord_sig_r = np.zeros_like(lt)
            for note_i, midi in enumerate(chord):
                f = _midi_to_hz(midi)
                det = 0.45 if (note_i % 2 == 0) else -0.45
                phase_off = 0.37 * note_i
                w_l = 2.0 * np.pi * (f + det) * lt + phase_off
                w_r = 2.0 * np.pi * (f - det) * lt + phase_off + 0.15
                tone_l = np.sin(w_l) + 0.28 * np.sin(2.0 * w_l) + 0.08 * np.sin(3.0 * w_l)
                tone_r = np.sin(w_r) + 0.28 * np.sin(2.0 * w_r) + 0.08 * np.sin(3.0 * w_r)
                amp = 0.10 / max(1.0, math.sqrt(len(chord)))
                chord_sig_l += amp * tone_l
                chord_sig_r += amp * tone_r

            if cfg.shimmer_level > 0:
                f_hi = _midi_to_hz(chord[-1] + 12)
                sh_env = env * (0.65 + 0.35 * np.sin(2.0 * np.pi * 0.25 * lt))
                sh_l = 0.035 * cfg.shimmer_level * np.sin(2.0 * np.pi * (f_hi + 0.3) * lt)
                sh_r = 0.035 * cfg.shimmer_level * np.sin(2.0 * np.pi * (f_hi - 0.3) * lt)
                chord_sig_l += sh_env * sh_l
                chord_sig_r += sh_env * sh_r

            f_b = _midi_to_hz(bass)
            bass_tone = 0.13 * (np.sin(2.0 * np.pi * f_b * lt) + 0.22 * np.sin(4.0 * np.pi * f_b * lt))
            buf[i0:i1, 0] += env * chord_sig_l + env * bass_tone
            buf[i0:i1, 1] += env * chord_sig_r + env * bass_tone

    # 2. Gentle pulse layer (never harsh transients).
    beat_sec = actual_bar_sec / 4.0
    if cfg.pulse_mode == "lofi_beat":
        n_beats = int(total_sec / beat_sec)
        for b in range(n_beats):
            bt = b * beat_sec
            bi = int(round(bt * SAMPLE_RATE))
            if b % 4 in (0, 2):
                klen = min(int(0.14 * SAMPLE_RATE), n_samples - bi)
                if klen > 16:
                    kt = np.arange(klen, dtype=np.float64) / float(SAMPLE_RATE)
                    freq_curve = 42.0 + 52.0 * np.exp(-kt / 0.032)
                    phase = 2.0 * np.pi * np.cumsum(freq_curve) / float(SAMPLE_RATE)
                    kenv = np.exp(-kt / 0.055) * np.minimum(1.0, kt / 0.004)
                    kick = 0.18 * kenv * np.sin(phase)
                    buf[bi:bi + klen, 0] += kick
                    buf[bi:bi + klen, 1] += kick
            else:
                hlen = min(int(0.065 * SAMPLE_RATE), n_samples - bi)
                if hlen > 16:
                    ht = np.arange(hlen, dtype=np.float64) / float(SAMPLE_RATE)
                    henv = np.exp(-ht / 0.018) * np.minimum(1.0, ht / 0.003)
                    brush = 0.045 * henv[:, None] * rng.standard_normal((hlen, 2))
                    buf[bi:bi + hlen] += brush
    elif cfg.pulse_mode == "study_pulse":
        n_beats = int(total_sec / beat_sec)
        for b in range(0, n_beats, 2):
            bt = b * beat_sec
            bi = int(round(bt * SAMPLE_RATE))
            plen = min(int(0.16 * SAMPLE_RATE), n_samples - bi)
            if plen > 16:
                pt = np.arange(plen, dtype=np.float64) / float(SAMPLE_RATE)
                penv = np.sin(np.pi * pt / (plen / float(SAMPLE_RATE))) ** 2
                thump = 0.075 * penv * np.sin(2.0 * np.pi * 65.0 * pt)
                buf[bi:bi + plen, 0] += thump
                buf[bi:bi + plen, 1] += thump

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
    """Create a vertically tileable 1920x2160 RGBA sheet for seamless particle/rain drift."""
    from PIL import Image, ImageDraw

    cfg = _get_preset(preset)
    rng = random.Random(_seed_int(f"particles:{seed or cfg.key}"))
    tile = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)

    if cfg.rain_level >= 0.12:
        count = int(120 + 260 * cfg.rain_level)
        for _ in range(count):
            x = rng.randint(0, WIDTH - 1)
            y = rng.randint(0, HEIGHT - 1)
            length = rng.randint(24, 58)
            dx = -4
            alpha = rng.randint(45, 135)
            col = (*cfg.accent_color, alpha)
            for dy_wrap in (0, -HEIGHT, HEIGHT):
                draw.line((x, y + dy_wrap, x + dx, y + dy_wrap + length), fill=col, width=2)
    else:
        for _ in range(95):
            x = rng.randint(0, WIDTH - 1)
            y = rng.randint(0, HEIGHT - 1)
            r = rng.randint(2, 5)
            alpha = rng.randint(50, 150)
            for dy_wrap in (0, -HEIGHT, HEIGHT):
                draw.ellipse((x - r, y + dy_wrap - r, x + r, y + dy_wrap + r), fill=(*cfg.accent_color, alpha))

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


def _short_headline(title: str, fallback: str) -> str:
    cleaned = title.split("[")[0].split("—")[0].strip()
    words = [w for w in cleaned.split() if not w.startswith("#")][:4]
    return (" ".join(words) or fallback).upper()


def generate_ambient_thumbnails(
    preset: str,
    *,
    out_dir: Path,
    title: str,
    duration_sec: float = 1800.0,
    brand_name: str = "",
    seed: str = "",
) -> tuple[list[dict], str, str]:
    """Generate hero_raw.jpg + 3 distinct 1280x720 Pillow thumbnails (plus optional AI v4)."""
    from PIL import Image, ImageDraw, ImageEnhance

    cfg = _get_preset(preset)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    scene = render_scene_image(cfg.key, seed=seed or title, brand_name="", size=(THUMB_W, THUMB_H))
    hero_path = _save_jpeg_under_limit(scene, out_dir / "hero_raw.jpg")
    headline = _short_headline(title, cfg.label)
    dur_badge = _duration_badge(duration_sec).upper()

    variants: list[dict] = []

    # v1: Mood + Title (soft bottom gradient + bold headline + dynamic duration badge)
    v1 = scene.copy().convert("RGBA")
    scrim1 = Image.new("RGBA", (1, THUMB_H), (0, 0, 0, 0))
    s1_px = scrim1.load()
    start_y = int(THUMB_H * 0.35)
    for y in range(start_y, THUMB_H):
        frac = max(0.0, (y - start_y) / max(1, THUMB_H - start_y))
        s1_px[0, y] = (8, 10, 18, int(round(190 * (frac ** 1.35))))
    v1 = Image.alpha_composite(v1, scrim1.resize((THUMB_W, THUMB_H), Image.Resampling.BILINEAR))
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    f_badge = _pil_font(30, dur_badge)
    bb1 = od.textbbox((0, 0), dur_badge, font=f_badge)
    bw1 = (bb1[2] - bb1[0]) + 48
    od.rounded_rectangle((48, 42, 48 + bw1, 100), radius=14, fill=(*cfg.glow_color, 235))
    od.text((72, 54), dur_badge, font=f_badge, fill=(18, 16, 24, 255))
    f_big = _pil_font(68, headline)
    od.text((52, int(THUMB_H * 0.68)), headline, font=f_big, fill=(255, 255, 255, 250))
    sub = f"{cfg.label} · 100% Original"
    od.text((54, int(THUMB_H * 0.83)), sub, font=_pil_font(30, sub), fill=(*cfg.accent_color, 240))
    v1 = Image.alpha_composite(v1, overlay)
    variants.append({"id": "v1", "label": "Mood + Title", "path": _save_jpeg_under_limit(v1, out_dir / "thumb_v1.jpg")})

    # v2: Focus Edition (punch crop + contrast boost + smooth horizontal gradient + measured pill)
    crop_w, crop_h = int(THUMB_W * 0.90), int(THUMB_H * 0.90)
    dx, dy = (THUMB_W - crop_w) // 2, (THUMB_H - crop_h) // 2
    v2 = scene.crop((dx, dy, dx + crop_w, dy + crop_h)).resize((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
    v2 = ImageEnhance.Contrast(v2).enhance(1.16).convert("RGBA")
    scrim2 = Image.new("RGBA", (THUMB_W, 1), (0, 0, 0, 0))
    s2_px = scrim2.load()
    end_x = int(THUMB_W * 0.85)
    for x in range(end_x):
        frac = max(0.0, 1.0 - (x / max(1, end_x)))
        s2_px[x, 0] = (10, 14, 24, int(round(150 * (frac ** 1.4))))
    v2 = Image.alpha_composite(v2, scrim2.resize((THUMB_W, THUMB_H), Image.Resampling.BILINEAR))
    ov2 = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(ov2)
    pill2 = "NO COPYRIGHT · DEEP FOCUS"
    f_p2 = _pil_font(24, pill2)
    bb2 = d2.textbbox((0, 0), pill2, font=f_p2)
    pw2 = (bb2[2] - bb2[0]) + 44
    d2.rounded_rectangle((48, 44, 48 + pw2, 98), radius=12, fill=(24, 32, 48, 225), outline=(*cfg.accent_color, 220), width=2)
    d2.text((70, 56), pill2, font=f_p2, fill=(255, 255, 255, 245))
    d2.text((52, int(THUMB_H * 0.42)), headline, font=_pil_font(64, headline), fill=(*cfg.accent_color, 255))
    d2.text((54, int(THUMB_H * 0.56)), f"{dur_badge} AMBIENT SESSION", font=_pil_font(30), fill=(240, 244, 255, 235))
    v2 = Image.alpha_composite(v2, ov2)
    variants.append({"id": "v2", "label": "Focus Edition", "path": _save_jpeg_under_limit(v2, out_dir / "thumb_v2.jpg")})

    # v3: Clean Atmospheric (color pop + minimal top-right pill only)
    v3 = ImageEnhance.Color(scene).enhance(1.25).convert("RGBA")
    ov3 = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    d3 = ImageDraw.Draw(ov3)
    pill_txt = f"{cfg.label.upper()} · {dur_badge}"
    pf = _pil_font(24, pill_txt)
    tb = d3.textbbox((0, 0), pill_txt, font=pf)
    pw = (tb[2] - tb[0]) + 44
    d3.rounded_rectangle((THUMB_W - pw - 42, 38, THUMB_W - 42, 90), radius=24, fill=(14, 18, 30, 200), outline=(*cfg.glow_color, 210), width=2)
    d3.text((THUMB_W - pw - 20, 51), pill_txt, font=pf, fill=(255, 255, 255, 245))
    if brand_name.strip():
        d3.text((48, THUMB_H - 68), brand_name.strip(), font=_pil_font(26, brand_name), fill=(245, 248, 255, 190))
    v3 = Image.alpha_composite(v3, ov3)
    variants.append({"id": "v3", "label": "Clean Atmospheric", "path": _save_jpeg_under_limit(v3, out_dir / "thumb_v3.jpg")})

    prompt = (
        f"Cozy 16:9 YouTube ambient illustration matching {cfg.label} ({cfg.mood_summary}), "
        f"clean atmospheric lighting, rich depth, no text overlays, no faces, title theme: {headline}"
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
) -> AmbientResult:
    """Generate a 16:9 1080p 30fps claim-free procedural ambient MP4, thumbnails, and SEO metadata."""
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

    meta = build_ambient_metadata(
        cfg.key,
        duration_sec=target_seconds,
        title=title,
        brand_name=brand_name,
        theme=theme,
        seed=job_seed,
        chapter_interval_sec=chapter_interval_sec,
        language=language,
        include_chapters=False,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="ambient_") as td:
            tmp = Path(td)
            raw_wav = tmp / "raw_loop.wav"
            norm_wav = tmp / "norm_loop.wav"
            scene_png = tmp / "scene_2048.png"
            particles_png = tmp / "particles_2160.png"

            audio_cycle = min(target_seconds, 12.8 if cfg.bpm == 75.0 else DEFAULT_LOOP_SEC)
            if target_seconds <= DEFAULT_LOOP_SEC:
                audio_cycle = max(2.0, target_seconds)
            _synthesize_raw_loop_wav(cfg.key, raw_wav, cycle_sec=audio_cycle, seed=job_seed)
            _normalize_loop_wav(raw_wav, norm_wav)

            scene_img = render_scene_image(cfg.key, seed=job_seed, brand_name=brand_name, size=(2048, 1152))
            scene_img.save(scene_png, format="PNG")
            _render_particle_sheet(cfg.key, particles_png, seed=job_seed)

            fade_in = min(0.60, target_seconds * 0.15)
            fade_out = min(0.80, target_seconds * 0.20)
            fade_out_st = max(0.05, target_seconds - fade_out)
            af_chain = (
                f"afade=t=in:st=0:d={fade_in:.2f},"
                f"afade=t=out:st={fade_out_st:.3f}:d={fade_out:.2f},"
                "alimiter=limit=0.89:level=0:attack=5:release=50"
            )

            if target_seconds <= DEFAULT_LOOP_SEC:
                vf_chain = (
                    f"[0:v]crop={WIDTH}:{HEIGHT}:x='64+48*sin(2*PI*t/{DEFAULT_LOOP_SEC:.1f})':"
                    f"y='36+24*cos(2*PI*t/{DEFAULT_LOOP_SEC:.1f})',setsar=1[bg];"
                    f"[1:v]crop={WIDTH}:{HEIGHT}:0:'{HEIGHT}-mod(t*{HEIGHT}*2/{DEFAULT_LOOP_SEC:.1f},{HEIGHT})'[pt];"
                    "[bg][pt]overlay=0:0:shortest=1,vignette=PI/6,format=yuv420p[vout]"
                )
                run_ffmpeg(
                    [
                        "-loop", "1", "-framerate", str(FPS), "-t", f"{target_seconds:.3f}", "-i", str(scene_png),
                        "-loop", "1", "-framerate", str(FPS), "-t", f"{target_seconds:.3f}", "-i", str(particles_png),
                        "-stream_loop", "-1", "-i", str(norm_wav),
                        "-filter_complex", vf_chain,
                        "-map", "[vout]", "-map", "2:a:0",
                        "-t", f"{target_seconds:.3f}",
                        "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
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
                vf_loop = (
                    f"[0:v]crop={WIDTH}:{HEIGHT}:x='64+48*sin(2*PI*t/{DEFAULT_LOOP_SEC:.1f})':"
                    f"y='36+24*cos(2*PI*t/{DEFAULT_LOOP_SEC:.1f})',setsar=1[bg];"
                    f"[1:v]crop={WIDTH}:{HEIGHT}:0:'{HEIGHT}-mod(t*{HEIGHT}*2/{DEFAULT_LOOP_SEC:.1f},{HEIGHT})'[pt];"
                    "[bg][pt]overlay=0:0:shortest=1,vignette=PI/6,format=yuv420p[vout]"
                )
                run_ffmpeg(
                    [
                        "-loop", "1", "-framerate", str(FPS), "-t", f"{DEFAULT_LOOP_SEC:.3f}", "-i", str(scene_png),
                        "-loop", "1", "-framerate", str(FPS), "-t", f"{DEFAULT_LOOP_SEC:.3f}", "-i", str(particles_png),
                        "-filter_complex", vf_loop,
                        "-map", "[vout]", "-an",
                        "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
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

    resolved_thumbs_dir = Path(thumbs_dir) if thumbs_dir is not None else (out.parent / f"{out.stem}_thumbs")
    thumbs, thumb_prompt, hero_frame = generate_ambient_thumbnails(
        cfg.key,
        out_dir=resolved_thumbs_dir,
        title=meta.title,
        duration_sec=info.duration or target_seconds,
        brand_name=brand_name,
        seed=job_seed,
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
    )
