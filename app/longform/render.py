"""Native 16:9 1080p compilation renderer: cold-open, transition stingers, lower-thirds, -14 LUFS audio, exact chapters."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from app.clipper import timeline
from app.clipper.captions import font_family
from app.clipper.media import MediaError, MediaInfo, probe, run_ffmpeg
from app.clipper.render import _filter_path_arg
from app.config import settings
from app.transformers.fonts import resolve_font
from app.winproc import background_subprocess_kwargs, reclaim_memory

from .planner import CompilationPlan, SegmentPlan

log = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1920, 1080, 30
_MOTION_WIDTH, _MOTION_HEIGHT = 1996, 1124
_TRANSITION_SEC = 0.16
_END_SCREEN_SEC = 8.0
_ALIMITER = "alimiter=limit=0.84:attack=2:release=40:level=0:latency=1"
_KINETIC_POP = r"\fscx114\fscy114\t(0,140,\fscx100\fscy100)"
_BRAND_ASS_STYLE = (
    "Style: Brand,{font_name},30,&H38FFFFFF,&H000000FF,&H20000000,&H00000000,"
    "1,0,0,0,100,100,0.5,0,1,3,1.5,9,44,64,44,1"
)
_DANGLING_WORDS = frozenset({
    "for", "to", "and", "&", "or", "the", "a", "an", "of", "in", "on", "at", "with", "from", "by", "vs",
    "ki", "ke", "ka", "ko", "mein", "par", "se", "ne",
})


@dataclass(frozen=True)
class RenderResult:
    output_path: str
    duration: float
    chapters: tuple[tuple[float, str], ...]
    chapter_text: str
    timestamps_for_thumbs: tuple[float, ...]
    warnings: tuple[str, ...] = ()


def format_chapters(chapters: Sequence[tuple[float, str]]) -> str:
    """Format ``(seconds, title)`` pairs into clean YouTube timestamp chapter lines."""
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
        label = " ".join(str(raw_label or "Moment").split()).strip()[:80] or "Moment"
        lines.append(f"{stamp} {label}")
    return "\n".join(lines)


def _ass_safe(text: str) -> str:
    clean = str(text or "").replace("\\", "").replace("{", "(").replace("}", ")")
    return " ".join(clean.split()).strip()


def _ass_clock(seconds: float) -> str:
    centis = max(0, int(round(max(0.0, float(seconds)) * 100)))
    return f"{centis // 360000}:{centis // 6000 % 60:02d}:{centis // 100 % 60:02d}.{centis % 100:02d}"


def _ass_font_name(font_path: str) -> str:
    """Resolve the real TTF family name for ASS styles so libass matches fontsdir."""
    if not font_path:
        return "Arial"
    fam = font_family(font_path).replace(",", " ").strip()
    return fam or Path(font_path).stem.replace(",", " ").strip() or "Arial"


def _write_ass_overlay(
    ass_path: Path,
    *,
    badge_text: str = "",
    lower_third: str = "",
    brand: str = "",
    start_sec: float = 0.4,
    hold_sec: float = 3.2,
    sample_text: str = "",
    fonts_dir: str = "",
) -> None:
    font_path = resolve_font(settings.clip_font, sample_text or f"{badge_text} {lower_third} {brand}")
    font_name = _ass_font_name(font_path)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {WIDTH}",
        f"PlayResY: {HEIGHT}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Pill,{font_name},30,&H00FFFFFF,&H000000FF,&H40000000,&HC4101016,1,0,0,0,100,100,0,0,3,14,0,7,48,48,38,1",
        _BRAND_ASS_STYLE.format(font_name=font_name),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    st = _ass_clock(start_sec)
    et = _ass_clock(start_sec + hold_sec)
    # Place act/chapter pills cleanly at top-left (Alignment=7) so bottom creator subtitles never collide.
    banner = " — ".join(part for part in (badge_text, lower_third) if part).strip()
    if banner:
        lines.append(f"Dialogue: 1,{st},{et},Pill,,0,0,0,,{{{_KINETIC_POP}\\fad(180,260)}}{_ass_safe(banner)}")
        if badge_text:
            lines.append(
                f"Dialogue: 2,{st},{et},Pill,,0,0,0,,"
                "{\\p1\\pos(48,30)\\c&H24D2FF&\\bord0\\shad0\\fad(180,260)}m 0 0 l 72 0 l 72 4 l 0 4{\\p0}"
            )
    if brand:
        lines.append(f"Dialogue: 0,0:00:00.00,9:59:59.00,Brand,,0,0,0,,{_ass_safe(brand)}")
    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_bridge_ass(
    ass_path: Path,
    *,
    badge_text: str,
    headline: str,
    brand: str = "",
    hold_sec: float = 4.5,
    sample_text: str = "",
) -> None:
    """Write editorial upper-left documentary bridge typography away from bottom creator subtitles."""
    font_path = resolve_font(settings.clip_font, sample_text or f"{badge_text} {headline} {brand}")
    font_name = _ass_font_name(font_path)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {WIDTH}",
        f"PlayResY: {HEIGHT}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: BridgeBadge,{font_name},30,&H00121216,&H000000FF,&H0024D2FF,&HFF24D2FF,1,0,0,0,100,100,1,0,3,14,0,7,64,64,64,1",
        f"Style: BridgeTitle,{font_name},52,&H00FFFFFF,&H000000FF,&HB0000000,&H00000000,1,0,0,0,100,100,0,0,1,4,3,7,64,220,126,1",
        _BRAND_ASS_STYLE.format(font_name=font_name),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    st = _ass_clock(0.08)
    et = _ass_clock(max(0.4, float(hold_sec) - 0.08))
    if badge_text:
        lines.append(f"Dialogue: 1,{st},{et},BridgeBadge,,0,0,0,,{{{_KINETIC_POP}\\fad(160,200)}}{_ass_safe(badge_text)}")
        lines.append(
            f"Dialogue: 1,{st},{et},BridgeBadge,,0,0,0,,"
            "{\\p1\\pos(64,112)\\c&H24D2FF&\\bord0\\shad0\\fad(160,200)}m 0 0 l 96 0 l 96 5 l 0 5{\\p0}"
        )
    if headline:
        lines.append(f"Dialogue: 2,{st},{et},BridgeTitle,,0,0,0,,{{{_KINETIC_POP}\\fad(200,220)}}{_ass_safe(headline)}")
    if brand:
        lines.append(f"Dialogue: 0,0:00:00.00,9:59:59.00,Brand,,0,0,0,,{_ass_safe(brand)}")
    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_outro_ass(
    ass_path: Path,
    *,
    brand: str = "",
    language: str = "en",
    hold_sec: float = _END_SCREEN_SEC,
    sample_text: str = "",
) -> str:
    """Write upper-center outro CTA + @Brand watermark leaving lower end-screen cards unobstructed."""
    hi = str(language or "en").strip().lower().startswith("hi")
    badge = "आगे भी देखें" if hi else "UP NEXT"
    headline = "अगला वीडियो देखें और सब्सक्राइब करें!" if hi else "Watch Next & Subscribe for More!"
    sample = sample_text or f"{badge} {headline} {brand}"
    font_path = resolve_font(settings.clip_font, sample)
    font_name = _ass_font_name(font_path)
    fonts_dir = str(Path(font_path).parent) if font_path else str(ass_path.parent)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {WIDTH}",
        f"PlayResY: {HEIGHT}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: OutroBadge,{font_name},30,&H00121216,&H000000FF,&H0024D2FF,&HFF24D2FF,1,0,0,0,100,100,0,0,3,14,0,8,64,64,92,1",
        f"Style: OutroTitle,{font_name},52,&H00FFFFFF,&H000000FF,&HB0000000,&H00000000,1,0,0,0,100,100,0,0,1,4,3,8,100,100,164,1",
        _BRAND_ASS_STYLE.format(font_name=font_name),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    st = _ass_clock(0.25)
    et = _ass_clock(max(1.0, float(hold_sec) - 0.35))
    lines.append(f"Dialogue: 1,{st},{et},OutroBadge,,0,0,0,,{{\\fad(220,280)}}{_ass_safe(badge)}")
    lines.append(f"Dialogue: 2,{st},{et},OutroTitle,,0,0,0,,{{\\fad(260,280)}}{_ass_safe(headline)}")
    if brand:
        lines.append(f"Dialogue: 0,0:00:00.00,9:59:59.00,Brand,,0,0,0,,{_ass_safe(brand)}")
    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fonts_dir


def _find_bright_offset(video_path: str, start: float, end: float, need_sec: float = 2.0) -> float:
    """Pick a visually lit offset inside [start, end] so bridges/cold-opens never start on black frames."""
    base = max(0.0, float(start))
    if not video_path or not os.path.isfile(video_path):
        return base
    try:
        if os.path.getsize(video_path) < 10_000:
            return base
    except OSError:
        return base

    hi = max(base, float(end or base) - max(0.5, float(need_sec)))
    span = max(0.0, hi - base)
    candidates = [base + min(1.0, span * 0.25), base + span * 0.55, hi]
    deduped: list[float] = []
    for c in candidates:
        rc = round(max(0.0, c), 2)
        if all(abs(rc - x) >= 0.4 for x in deduped):
            deduped.append(rc)

    best_offset, best_luma = base, -1.0
    try:
        from PIL import Image, ImageStat

        with tempfile.TemporaryDirectory(prefix="lf-luma-", ignore_cleanup_errors=True) as td:
            for idx, cand in enumerate(deduped):
                jpg = Path(td) / f"f{idx}.jpg"
                run_ffmpeg(
                    ["-ss", f"{cand:.2f}", "-i", os.path.abspath(video_path),
                     "-vf", "scale=160:90", "-frames:v", "1", "-q:v", "6", str(jpg)],
                    cwd=td,
                )
                if not jpg.is_file() or jpg.stat().st_size == 0:
                    continue
                with Image.open(jpg) as im:
                    luma = float(ImageStat.Stat(im.convert("L")).mean[0])
                if luma >= 38.0:
                    return cand
                if luma > best_luma:
                    best_luma, best_offset = luma, cand
    except Exception:
        return base
    return best_offset if best_luma >= 0 else base


def _probe_duration(path: Path, fallback: float) -> float:
    """Read exact container duration via ffprobe (safe for <1s stingers unlike media.probe)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            **background_subprocess_kwargs(low_priority=True),
        )
        val = float((proc.stdout or "").strip())
        if val > 0:
            return val
    except Exception:
        pass
    return fallback


def _common_encode_args() -> list[str]:
    threads = ["-threads", str(settings.ffmpeg_threads)] if settings.ffmpeg_threads > 0 else []
    return [
        *threads,
        "-c:v", "libx264", "-preset", "superfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-video_track_timescale", "30000",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    ]


def _render_stinger(out_path: Path, tmp_dir: Path) -> None:
    """0.16s soft dark cross-fade transition clip when no narrator bridge separates acts."""
    args = [
        "-f", "lavfi", "-i", f"color=c=0x0A0A0E:s={WIDTH}x{HEIGHT}:r={FPS}:d={_TRANSITION_SEC:.3f}",
        "-f", "lavfi", "-t", f"{_TRANSITION_SEC:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-filter_complex",
        f"[0:v]fade=t=in:st=0:d=0.06,fade=t=out:st={max(0.0, _TRANSITION_SEC - 0.06):.3f}:d=0.06,setsar=1[v];"
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{_TRANSITION_SEC:.3f},asetpts=N/SR/TB[a]",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{_TRANSITION_SEC:.3f}",
        *_common_encode_args(),
        str(out_path),
    ]
    run_ffmpeg(args, cwd=str(tmp_dir))


def _short_caption(text: str, max_words: int = 6, max_chars: int = 38) -> str:
    """Distill a moment title into a crisp, non-truncated caption without dangling words or #Shorts clutter."""
    clean = " ".join(str(text or "").replace("#Shorts", "").replace("#shorts", "").replace("...", "").split())
    clean = clean.strip(" -|:,—.")
    if not clean:
        return ""
    words = clean.split()[:max_words]
    out = " ".join(words).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(" -|:,—.") or out[:max_chars]
    parts = out.split()
    while len(parts) > 2 and parts[-1].strip(".,:;!?—-\"'").lower() in _DANGLING_WORDS:
        parts.pop()
    return " ".join(parts).rstrip(" -|:,—.")


def _build_entry_sfx(
    wav_path: Path,
    *,
    cue_at: float,
    kind: str,
    seed: str,
    language: str = "",
) -> Optional[Path]:
    """Generate a short entry SFX WAV via clipper.sfx (numpy imported lazily)."""
    if not settings.clip_sfx:
        return None
    try:
        from app.clipper import sfx

        sfx_kind = sfx.IMPACT if kind == "impact" else sfx.WHOOSH
        sfx.build_track(str(wav_path), max(1.0, cue_at + 1.0), [sfx.Cue(cue_at, sfx_kind)], seed=seed, language=language)
        if wav_path.is_file() and wav_path.stat().st_size > 44:
            return wav_path
    except Exception as exc:
        log.debug("longform.render: sfx skipped (%s): %s", kind, exc)
    return None


def _pick_real_music(seed: str) -> Optional[Path]:
    """Pick an owner/CC track from content/music/, never a _synth sine bed."""
    if not settings.clip_music:
        return None
    try:
        from app.clipper import music

        cand = music.pick(seed)
        if cand is not None and "_synth" not in cand.parts and cand.is_file():
            return cand
    except Exception:
        pass
    return None


def _render_cold_open_with_hook(
    cold_open: SegmentPlan,
    climax: SegmentPlan,
    hook_wav: Path,
    hook_dur: float,
    out_path: Path,
    tmp_dir: Path,
    *,
    format_kind: str,
    brand_name: str,
    music_path: Optional[Path] = None,
) -> float:
    """Render live bright climax teaser footage with integrated Sarvam hook narration (no frozen intro slide)."""
    dur = max(4.8, min(11.5, float(hook_dur or 4.0) + 0.65))
    info: MediaInfo = probe(climax.source_path)
    max_end = min(info.duration or climax.end, climax.end) if info.duration else climax.end
    win_start = max(climax.start, min(cold_open.start, max(climax.start, max_end - dur)))
    seek = _find_bright_offset(climax.source_path, win_start, min(max_end, win_start + dur + 6.0), dur)

    badge = "COMING UP · FINALE" if format_kind == "vs" else "COMING UP · #1"
    ass_file = tmp_dir / "overlay_cold_hook.ass"
    font_ref = resolve_font(settings.clip_font, f"{badge} {brand_name}")
    fonts_dir = str(Path(font_ref).parent) if font_ref else str(tmp_dir)
    _write_ass_overlay(
        ass_file,
        badge_text=badge,
        lower_third="",
        brand=brand_name,
        start_sec=0.20,
        hold_sec=max(2.0, dur - 0.5),
        sample_text=f"{badge} {brand_name}",
        fonts_dir=fonts_dir,
    )
    ass_arg = f"ass={_filter_path_arg(str(ass_file))}:fontsdir={_filter_path_arg(fonts_dir)}"

    inputs = [
        "-ss", f"{max(0.0, seek):.3f}", "-i", os.path.abspath(climax.source_path),
        "-i", str(hook_wav),
    ]
    next_idx = 2
    has_music = music_path is not None and Path(music_path).is_file()
    music_idx = -1
    if has_music:
        music_idx = next_idx
        next_idx += 1
        inputs += ["-stream_loop", "-1", "-i", str(music_path)]
    sfx_wav = _build_entry_sfx(
        tmp_dir / "cold_sfx.wav", cue_at=0.15, kind="impact",
        seed=f"co:{climax.source_id}", language=climax.language,
    )
    sfx_idx = -1
    if sfx_wav is not None:
        sfx_idx = next_idx
        next_idx += 1
        inputs += ["-i", str(sfx_wav)]

    fade_out_v = max(0.2, dur - 0.20)
    fade_out_a = max(0.2, dur - 0.20)
    parts = [
        f"[0:v]tpad=stop_mode=clone:stop_duration=3,trim=0:{dur:.3f},"
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS},"
        f"fade=t=in:st=0:d=0.18,fade=t=out:st={fade_out_v:.3f}:d=0.20,{ass_arg}[vout]",
        f"[1:a]adelay=180|180,loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000,"
        f"aformat=channel_layouts=stereo,apad=whole_dur={dur:.3f},atrim=0:{dur:.3f},asetpts=N/SR/TB[vo]",
    ]
    mix_labels = ["[vo]"]
    if has_music:
        parts.append(
            f"[{music_idx}:a]volume=0.08,aresample=48000,aformat=channel_layouts=stereo,"
            f"afade=t=in:d=0.18,afade=t=out:st={fade_out_a:.3f}:d=0.20,"
            f"atrim=0:{dur:.3f},asetpts=N/SR/TB[bed]"
        )
        mix_labels.append("[bed]")
    elif info.has_audio:
        parts.append(
            f"[0:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,volume=0.06,lowpass=f=900,"
            f"aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={dur:.3f},atrim=0:{dur:.3f},"
            f"afade=t=in:d=0.18,afade=t=out:st={fade_out_a:.3f}:d=0.20,asetpts=N/SR/TB[bed]"
        )
        mix_labels.append("[bed]")
    if sfx_idx >= 0:
        parts.append(
            f"[{sfx_idx}:a]volume=0.75,aresample=48000,aformat=channel_layouts=stereo,"
            f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f},asetpts=N/SR/TB[sfx]"
        )
        mix_labels.append("[sfx]")
    if len(mix_labels) > 1:
        parts.append(
            f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0,"
            f"{_ALIMITER},aformat=channel_layouts=stereo,asetpts=N/SR/TB[aout]"
        )
    else:
        parts.append(f"[vo]{_ALIMITER},aformat=channel_layouts=stereo,asetpts=N/SR/TB[aout]")

    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", ";".join(parts),
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{dur:.3f}",
            *_common_encode_args(),
            str(out_path),
        ],
        cwd=str(tmp_dir),
    )
    return max(0.2, _probe_duration(out_path, dur))


def _render_segment(
    seg: SegmentPlan,
    out_path: Path,
    tmp_dir: Path,
    *,
    index: int,
    display_rank: Optional[int] = None,
    is_cold_open: bool,
    format_kind: str,
    brand_name: str,
    gentle_trim: bool = True,
    show_pill: bool = True,
) -> float:
    from .planner import display_creator_name

    info: MediaInfo = probe(seg.source_path)
    src_start = max(0.0, float(seg.start))
    src_end = min(info.duration or seg.end, float(seg.end))
    if src_end <= src_start + 0.2:
        raise MediaError(f"segment {index} span invalid ({src_start:.2f}..{src_end:.2f})")

    if is_cold_open:
        keeps = [(src_start, src_end)]
    elif gentle_trim and info.has_audio:
        silences = timeline.detect_silences(seg.source_path, src_start, src_end, noise_db=-34.0, min_duration=1.15)
        keeps = timeline.audio_keep_segments(src_start, src_end, silences, pad_sec=0.24, min_cut_sec=1.15)
    else:
        keeps = [(src_start, src_end)]

    ass_file = tmp_dir / f"overlay_{index}_{'co' if is_cold_open else 'main'}.ass"
    badge = ("COMING UP · FINALE" if format_kind == "vs" else "COMING UP · #1") if is_cold_open else ""
    cap = _short_caption(seg.moment_title)
    creator_label = display_creator_name(seg.creator)
    if is_cold_open:
        lower = ""
        hold = max(1.5, min(3.8, (src_end - src_start) - 0.3))
    elif not show_pill:
        lower = ""
        hold = 0.0
    elif format_kind == "vs":
        lower = f"ROUND {index} · {creator_label}".strip()
        if cap:
            lower = f"{lower} — {cap}"
        hold = 3.2
    else:
        rank = display_rank if display_rank is not None else index
        lower = f"#{rank} · {creator_label}".strip()
        if cap:
            lower = f"{lower} — {cap}"
        hold = 3.2

    has_overlay = bool(badge or lower or brand_name)
    fonts_dir = str(tmp_dir)
    if has_overlay:
        font_ref = resolve_font(settings.clip_font, f"{badge} {lower} {brand_name}")
        fonts_dir = str(Path(font_ref).parent) if font_ref else str(tmp_dir)
        _write_ass_overlay(
            ass_file,
            badge_text=badge,
            lower_third=lower,
            brand=brand_name,
            start_sec=0.25 if is_cold_open else 0.35,
            hold_sec=hold,
            sample_text=f"{badge} {lower} {brand_name}",
            fonts_dir=fonts_dir,
        )

    seek_pad = max(0.0, keeps[0][0])
    parts: list[str] = []
    joined: list[str] = []
    n_keeps = len(keeps)
    if n_keeps > 1:
        parts.append(f"[0:v]split={n_keeps}{''.join(f'[vin{i}]' for i in range(n_keeps))}")
        if info.has_audio:
            parts.append(f"[0:a]asplit={n_keeps}{''.join(f'[ain{i}]' for i in range(n_keeps))}")
    for i, (ka, kb) in enumerate(keeps):
        ra = max(0.0, ka - seek_pad)
        rb = max(ra + 0.1, kb - seek_pad)
        vin = f"[vin{i}]" if n_keeps > 1 else "[0:v]"
        parts.append(f"{vin}trim=start={ra:.3f}:end={rb:.3f},setpts=PTS-STARTPTS[v{i}]")
        if info.has_audio:
            ain = f"[ain{i}]" if n_keeps > 1 else "[0:a]"
            seg_d = rb - ra
            if n_keeps > 1 and seg_d > 0.04:
                fade = min(0.008, seg_d * 0.1)
                parts.append(
                    f"{ain}atrim=start={ra:.3f}:end={rb:.3f},asetpts=PTS-STARTPTS,"
                    f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0.0, seg_d - fade):.3f}:d={fade:.3f}[a{i}]"
                )
            else:
                parts.append(f"{ain}atrim=start={ra:.3f}:end={rb:.3f},asetpts=PTS-STARTPTS[a{i}]")
            joined.extend([f"[v{i}]", f"[a{i}]"])
        else:
            joined.append(f"[v{i}]")

    if info.has_audio:
        parts.append(f"{''.join(joined)}concat=n={len(keeps)}:v=1:a=1[vcat][acat]")
    else:
        parts.append(f"{''.join(joined)}concat=n={len(keeps)}:v=1:a=0[vcat]")

    seg_len = max(0.4, sum(b - a for a, b in keeps))
    fade_out_v = max(0.2, seg_len - 0.16)
    fade_out_a = max(0.2, seg_len - 0.16)
    ass_suffix = f",ass={_filter_path_arg(str(ass_file))}:fontsdir={_filter_path_arg(fonts_dir)}" if has_overlay else ""
    pan_sign = "+" if index % 2 == 0 else "-"
    y_share = 0.50 if index % 2 == 0 else 0.46
    parts.append(
        f"[vcat]scale={_MOTION_WIDTH}:{_MOTION_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT}:x='(iw-ow)*0.5*(0.5{pan_sign}0.5*sin(t*0.18))':y='(ih-oh)*{y_share:.2f}',"
        f"setsar=1,fps={FPS},fade=t=in:st=0:d=0.14,fade=t=out:st={fade_out_v:.3f}:d=0.16{ass_suffix}[vout]"
    )

    inputs = ["-ss", f"{seek_pad:.3f}", "-i", os.path.abspath(seg.source_path)]
    next_idx = 1
    null_idx = -1
    if not info.has_audio:
        null_idx = next_idx
        next_idx += 1
        inputs += ["-f", "lavfi", "-t", f"{seg_len:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    sfx_wav = _build_entry_sfx(
        tmp_dir / f"seg_{index}_{'co' if is_cold_open else 'main'}_sfx.wav",
        cue_at=0.15 if is_cold_open else 0.10,
        kind="impact" if is_cold_open else "whoosh",
        seed=f"{seg.source_id}:{index}",
        language=seg.language,
    )
    sfx_idx = -1
    if sfx_wav is not None:
        sfx_idx = next_idx
        next_idx += 1
        inputs += ["-i", str(sfx_wav)]

    seg_music = None if is_cold_open else _pick_real_music(f"{seg.source_id}:{index}")
    music_idx = -1
    if seg_music is not None:
        music_idx = next_idx
        next_idx += 1
        inputs += ["-stream_loop", "-1", "-i", str(seg_music)]

    mix_labels: list[str] = []
    if info.has_audio:
        parts.append(
            "[acat]acompressor=threshold=0.12:ratio=3:attack=20:release=200:makeup=2,"
            "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo[voice]"
        )
        mix_labels.append("[voice]")
    else:
        parts.append(f"[{null_idx}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=N/SR/TB[voice]")
        mix_labels.append("[voice]")

    if sfx_idx >= 0:
        parts.append(
            f"[{sfx_idx}:a]volume=0.72,aresample=48000,aformat=channel_layouts=stereo,"
            f"apad=whole_dur={seg_len:.3f},atrim=0:{seg_len:.3f},asetpts=N/SR/TB[sfx]"
        )
        mix_labels.append("[sfx]")
    if music_idx >= 0:
        parts.append(
            f"[{music_idx}:a]volume=0.045,lowpass=f=2400,aresample=48000,aformat=channel_layouts=stereo,"
            f"afade=t=in:d=0.25,afade=t=out:st={fade_out_a:.3f}:d=0.25,"
            f"atrim=0:{seg_len:.3f},asetpts=N/SR/TB[bed]"
        )
        mix_labels.append("[bed]")

    if len(mix_labels) > 1:
        parts.append(
            f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0,"
            f"{_ALIMITER},aformat=channel_layouts=stereo,"
            f"afade=t=in:d=0.14,afade=t=out:st={fade_out_a:.3f}:d=0.16,asetpts=N/SR/TB[aout]"
        )
    elif info.has_audio:
        parts.append(
            f"[voice]{_ALIMITER},aformat=channel_layouts=stereo,"
            f"afade=t=in:d=0.14,afade=t=out:st={fade_out_a:.3f}:d=0.16,asetpts=N/SR/TB[aout]"
        )
    else:
        parts.append("[voice]asetpts=N/SR/TB[aout]")

    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", ";".join(parts),
            "-map", "[vout]", "-map", "[aout]",
            *_common_encode_args(),
            str(out_path),
        ],
        cwd=str(tmp_dir),
    )
    return max(0.1, probe(str(out_path)).duration)


def _render_end_tail(
    tail_src: Path,
    out_path: Path,
    tmp_dir: Path,
    *,
    seconds: float = _END_SCREEN_SEC,
    outro_wav: Optional[Path] = None,
    seek_sec: float = 0.0,
    music_path: Optional[Path] = None,
    brand_name: str = "",
    language: str = "en",
) -> float:
    """Clean 8s live-motion outro highlight backdrop with CTA overlay & Sarvam voiceover for end-screen cards."""
    src_info = probe(str(tail_src))
    has_vo = outro_wav is not None and Path(outro_wav).is_file()
    has_music = music_path is not None and Path(music_path).is_file()
    fade_out_a = max(0.2, seconds - 0.5)
    fade_out_v = max(0.2, seconds - 0.6)
    outro_ass = tmp_dir / "outro.ass"
    fonts_dir = _write_outro_ass(outro_ass, brand=brand_name, language=language, hold_sec=seconds)
    ass_arg = f"ass={_filter_path_arg(str(outro_ass))}:fontsdir={_filter_path_arg(fonts_dir)}"

    if src_info.duration and src_info.duration >= 4.0:
        need_src = max(2.0, seconds / 1.25)
        avail_start = max(0.0, min(float(seek_sec), max(0.0, src_info.duration - need_src)))
        bright_seek = _find_bright_offset(str(tail_src), avail_start, src_info.duration, need_src)
        inputs = ["-ss", f"{bright_seek:.3f}", "-i", str(tail_src)]
        filters = [
            f"[0:v]setpts=1.25*(PTS-STARTPTS),scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,eq=brightness=-0.18:contrast=1.06:saturation=0.88,"
            f"tpad=stop_mode=clone:stop_duration=3,trim=0:{seconds:.3f},fps={FPS},"
            f"fade=t=in:st=0:d=0.4,fade=t=out:st={fade_out_v:.3f}:d=0.6,{ass_arg}[v]",
        ]
        if has_vo:
            inputs += ["-i", str(outro_wav)]
            filters.append(
                f"[1:a]adelay=200|200,loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000,"
                f"aformat=channel_layouts=stereo,apad=whole_dur={seconds:.3f},atrim=0:{seconds:.3f},asetpts=N/SR/TB[vo]"
            )
            if has_music:
                inputs += ["-stream_loop", "-1", "-i", str(music_path)]
                filters.append(
                    f"[2:a]volume=0.08,aresample=48000,aformat=channel_layouts=stereo,"
                    f"afade=t=in:d=0.3,afade=t=out:st={fade_out_a:.3f}:d=0.5,atrim=0:{seconds:.3f},asetpts=N/SR/TB[bed]"
                )
                filters.append(f"[vo][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{_ALIMITER},aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]")
            elif src_info.has_audio:
                filters.append(
                    f"[0:a]atrim=0:{seconds:.3f},asetpts=PTS-STARTPTS,volume=0.12,lowpass=f=1800,"
                    f"aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={seconds:.3f},atrim=0:{seconds:.3f},"
                    f"afade=t=in:d=0.3,afade=t=out:st={fade_out_a:.3f}:d=0.5,asetpts=N/SR/TB[bed]"
                )
                filters.append(f"[vo][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{_ALIMITER},aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]")
            else:
                filters.append(f"[vo]{_ALIMITER},aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]")
        else:
            inputs += ["-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            filters.append("[1:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]")
    else:
        frame_jpg = tmp_dir / "end_still.jpg"
        run_ffmpeg(["-sseof", "-0.4", "-i", str(tail_src), "-frames:v", "1", "-q:v", "2", str(frame_jpg)], cwd=str(tmp_dir))
        inputs = ["-loop", "1", "-t", f"{seconds:.3f}", "-i", str(frame_jpg)]
        if has_vo:
            inputs += ["-i", str(outro_wav)]
            audio_filter = (
                f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000,{_ALIMITER},aformat=channel_layouts=stereo,"
                f"apad=whole_dur={seconds:.3f},atrim=0:{seconds:.3f},asetpts=N/SR/TB[a]"
            )
        else:
            inputs += ["-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            audio_filter = "[1:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]"
        filters = [
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},setsar=1,"
            f"eq=brightness=-0.18:saturation=0.88,fps={FPS},"
            f"fade=t=in:st=0:d=0.4,fade=t=out:st={fade_out_v:.3f}:d=0.6,{ass_arg}[v]",
            audio_filter,
        ]

    run_ffmpeg(
        [
            *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", "[a]",
            "-t", f"{seconds:.3f}",
            *_common_encode_args(),
            str(out_path),
        ],
        cwd=str(tmp_dir),
    )
    return max(0.1, probe(str(out_path)).duration)


def render_compilation(
    plan: CompilationPlan,
    out_path: str,
    *,
    brand_name: str = "",
    include_end_screen: bool = True,
    include_voiceover: Optional[bool] = None,
) -> RenderResult:
    """Render live-action cold-open hook, motion bridges, segments & live-motion outro into 1080p MP4."""
    from app.clipper import music
    from .planner import display_creator_name

    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".tmp.mp4")
    warnings = list(plan.warnings)
    if include_voiceover is None:
        if not bool(settings.longform_voiceover):
            use_vo = False
        elif settings.use_mock:
            from . import voiceover as vo_mod

            use_vo = vo_mod.has_any_tts_key()
        else:
            use_vo = True
    else:
        use_vo = bool(include_voiceover)
    # Never pull _synth sine drones into 16:9 documentary videos; only real owner tracks.
    owner_tracks = music.library()
    owner_music: Optional[Path] = owner_tracks[0] if owner_tracks else None

    with tempfile.TemporaryDirectory(prefix="longform-render-", ignore_cleanup_errors=True) as td:
        tmp_dir = Path(td)
        stinger = tmp_dir / "trans.mp4"
        stinger_created = False

        voiceover = None
        script: dict = {}
        if use_vo:
            missing_srcs = [str(s.source_path) for s in (plan.cold_open, *plan.segments) if not os.path.isfile(str(s.source_path))]
            if missing_srcs:
                warnings.append("voiceover skipped: missing source clip file")
            else:
                from . import voiceover as vo_mod

                try:
                    voiceover = vo_mod
                    script = voiceover.generate_commentary_script(plan)
                except vo_mod.SarvamTTSQuotaError:
                    raise
                except Exception as exc:
                    warnings.append(f"voiceover skipped: {exc}")
                    voiceover = None

        parts_to_concat: list[Path] = []
        chapters: list[tuple[float, str]] = [(0.0, "Intro / Preview")]
        thumb_stamps: list[float] = []
        cursor = 0.0
        climax = max(plan.segments, key=lambda s: s.score) if plan.segments else plan.cold_open

        cold_file = tmp_dir / "part_00_cold_open.mp4"
        cold_rendered_with_vo = False
        hook_text = (script.get("hook") or script.get("intro") or "").strip() if voiceover is not None else ""
        if voiceover is not None and hook_text:
            hook_wav = tmp_dir / "hook.wav"
            try:
                vo_dur = voiceover.synthesize_wav(hook_text, hook_wav, language=plan.language)
            except voiceover.SarvamTTSQuotaError:
                raise
            except Exception as exc:
                warnings.append(f"hook voiceover skipped: {exc}")
                vo_dur = None
            if vo_dur:
                co_dur = _render_cold_open_with_hook(
                    plan.cold_open,
                    climax,
                    hook_wav,
                    vo_dur,
                    cold_file,
                    tmp_dir,
                    format_kind=plan.format_kind,
                    brand_name=brand_name,
                    music_path=owner_music,
                )
                cold_rendered_with_vo = True

        if not cold_rendered_with_vo:
            co_dur = _render_segment(
                plan.cold_open, cold_file, tmp_dir,
                index=0, is_cold_open=True, format_kind=plan.format_kind, brand_name=brand_name,
            )
        parts_to_concat.append(cold_file)
        cursor += co_dur

        total_segs = len(plan.segments)
        bridges = list(script.get("bridges") or [])
        for idx, seg in enumerate(plan.segments, start=1):
            rank = (total_segs - idx + 1) if plan.format_kind != "vs" else idx
            prefix = f"Round {idx}" if plan.format_kind == "vs" else f"#{rank}"
            cname = display_creator_name(seg.creator)
            label = f"{prefix} {cname} — {seg.moment_title}".strip(" —")
            clean_headline = _short_caption(seg.moment_title, max_words=6, max_chars=38) or "Key Moment"
            badge = f"ROUND {idx} · {cname.upper()}" if plan.format_kind == "vs" else f"#{rank} · {cname.upper()}"

            bridge_card: Optional[Path] = None
            bridge_dur = 0.0
            if voiceover is not None and idx - 1 < len(bridges) and bridges[idx - 1]:
                b_wav = tmp_dir / f"bridge_{idx:02d}.wav"
                try:
                    vo_dur = voiceover.synthesize_wav(bridges[idx - 1], b_wav, language=plan.language)
                except voiceover.SarvamTTSQuotaError:
                    raise
                except Exception as exc:
                    warnings.append(f"bridge {idx} voiceover skipped: {exc}")
                    vo_dur = None
                if vo_dur:
                    try:
                        cand_card = tmp_dir / f"part_{idx:02d}_vo.mp4"
                        bridge_dur = voiceover.render_narrator_bridge(
                            b_wav,
                            Path(seg.source_path),
                            cand_card,
                            tmp_dir,
                            badge_text=badge,
                            headline=clean_headline,
                            creator_label=display_creator_name(seg.creator),
                            brand_name=brand_name,
                            duration=vo_dur,
                            preview_start=float(seg.start),
                            preview_end=float(seg.end),
                            music_path=owner_music,
                        )
                        bridge_card = cand_card
                    except Exception as exc:
                        warnings.append(f"bridge {idx} render skipped: {exc}")

            if bridge_card is not None:
                chapters.append((cursor, label))
                parts_to_concat.append(bridge_card)
                cursor += bridge_dur
            else:
                if not stinger_created:
                    _render_stinger(stinger, tmp_dir)
                    stinger_created = True
                parts_to_concat.append(stinger)
                cursor += _probe_duration(stinger, _TRANSITION_SEC)
                chapters.append((cursor, label))

            seg_file = tmp_dir / f"part_{idx:02d}.mp4"
            dur = _render_segment(
                seg, seg_file, tmp_dir,
                index=idx, display_rank=rank, is_cold_open=False,
                format_kind=plan.format_kind, brand_name=brand_name,
                show_pill=(bridge_card is None),
            )
            parts_to_concat.append(seg_file)
            t_early = cursor + min(max(1.5, dur * 0.35), max(1.5, dur - 1.2))
            t_late = cursor + min(max(2.0, dur * 0.65), max(2.0, dur - 0.8))
            thumb_stamps.append(t_early)
            if abs(t_late - t_early) >= 4.0:
                thumb_stamps.append(t_late)
            cursor += dur
            reclaim_memory()

        if include_end_screen and parts_to_concat:
            outro_wav_path: Optional[Path] = None
            if voiceover is not None and script.get("outro"):
                cand_wav = tmp_dir / "outro.wav"
                try:
                    if voiceover.synthesize_wav(script["outro"], cand_wav, language=plan.language):
                        outro_wav_path = cand_wav
                except voiceover.SarvamTTSQuotaError:
                    raise
                except Exception as exc:
                    warnings.append(f"outro voiceover skipped: {exc}")
            tail_file = tmp_dir / "part_tail.mp4"
            tail_src = Path(climax.source_path) if os.path.isfile(str(climax.source_path)) else parts_to_concat[-1]
            tail_seek = max(float(climax.start), float(climax.start) + climax.duration * 0.45) if tail_src == Path(climax.source_path) else 0.0
            _render_end_tail(
                tail_src,
                tail_file,
                tmp_dir,
                seconds=_END_SCREEN_SEC,
                outro_wav=outro_wav_path,
                seek_sec=tail_seek,
                music_path=owner_music,
                brand_name=brand_name,
                language=plan.language,
            )
            parts_to_concat.append(tail_file)

        concat_txt = tmp_dir / "concat.txt"
        concat_txt.write_text(
            "\n".join(f"file '{str(p.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for p in parts_to_concat) + "\n",
            encoding="utf-8",
        )
        run_ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", "-movflags", "+faststart", str(partial)],
            cwd=str(tmp_dir),
        )
        if not partial.is_file() or partial.stat().st_size < 1000:
            raise MediaError("longform render produced no usable output")
        final_info = probe(str(partial))
        os.replace(partial, out)

    chapter_text = format_chapters(chapters)
    return RenderResult(
        output_path=str(out),
        duration=round(final_info.duration or cursor, 2),
        chapters=tuple(chapters),
        chapter_text=chapter_text,
        timestamps_for_thumbs=tuple(thumb_stamps[:8]),
        warnings=tuple(warnings),
    )
