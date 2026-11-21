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
from app.clipper.media import MediaError, MediaInfo, probe, run_ffmpeg
from app.clipper.render import _filter_path_arg
from app.config import settings
from app.transformers.fonts import resolve_font
from app.winproc import background_subprocess_kwargs, reclaim_memory

from .planner import CompilationPlan, SegmentPlan

log = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1920, 1080, 30
_TRANSITION_SEC = 0.30
_END_SCREEN_SEC = 8.0


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
    font_name = "Arial"
    if font_path:
        stem = Path(font_path).stem.replace(",", " ")
        if stem:
            font_name = stem
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
        f"Style: Pill,{font_name},34,&H00FFFFFF,&H000000FF,&H40000000,&HB0121216,1,0,0,0,100,100,0,0,3,14,0,7,48,48,44,1",
        f"Style: LowerThird,{font_name},38,&H00FFFFFF,&H000000FF,&H50000000,&HB8121216,1,0,0,0,100,100,0,0,3,16,0,1,60,60,64,1",
        f"Style: Brand,{font_name},28,&HA8FFFFFF,&H000000FF,&H60000000,&H00000000,1,0,0,0,100,100,0,0,1,2,1,9,44,44,36,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    st = _ass_clock(start_sec)
    et = _ass_clock(start_sec + hold_sec)
    if badge_text:
        lines.append(f"Dialogue: 1,{st},{et},Pill,,0,0,0,,{{\\fad(150,250)}}{_ass_safe(badge_text)}")
    if lower_third:
        lines.append(f"Dialogue: 2,{st},{et},LowerThird,,0,0,0,,{{\\fad(200,300)}}{_ass_safe(lower_third)}")
    if brand:
        lines.append(f"Dialogue: 0,0:00:00.00,9:59:59.00,Brand,,0,0,0,,{_ass_safe(brand)}")
    ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    """0.30s dark flash + synthesized whoosh transition clip matching 1080p stereo stream format."""
    from app.clipper import sfx

    wav = tmp_dir / "stinger.wav"
    sfx.build_track(str(wav), _TRANSITION_SEC, [sfx.Cue(0.02, sfx.WHOOSH)])
    args = [
        "-f", "lavfi", "-i", f"color=c=0x0A0A0E:s={WIDTH}x{HEIGHT}:r={FPS}:d={_TRANSITION_SEC:.3f}",
        "-i", str(wav),
        "-filter_complex",
        f"[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=white@0.22:t=fill:enable='lt(t,0.06)',setsar=1[v];"
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{_TRANSITION_SEC:.3f},asetpts=N/SR/TB[a]",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{_TRANSITION_SEC:.3f}",
        *_common_encode_args(),
        str(out_path),
    ]
    run_ffmpeg(args, cwd=str(tmp_dir))


def _short_caption(text: str, max_words: int = 6, max_chars: int = 44) -> str:
    """Distill a moment title into a crisp lower-third caption without #Shorts clutter."""
    clean = " ".join(str(text or "").replace("#Shorts", "").replace("#shorts", "").split()).strip(" -|:,—")
    if not clean:
        return ""
    words = clean.split()[:max_words]
    out = " ".join(words).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(" -|:,—") or out[:max_chars]
    return out


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
) -> float:
    info: MediaInfo = probe(seg.source_path)
    src_start = max(0.0, float(seg.start))
    src_end = min(info.duration or seg.end, float(seg.end))
    if src_end <= src_start + 0.2:
        raise MediaError(f"segment {index} span invalid ({src_start:.2f}..{src_end:.2f})")

    if is_cold_open:
        keeps = [(src_start, src_end)]
    elif gentle_trim and info.has_audio:
        silences = timeline.detect_silences(seg.source_path, src_start, src_end, noise_db=-32.0, min_duration=0.85)
        keeps = timeline.audio_keep_segments(src_start, src_end, silences, pad_sec=0.18, min_cut_sec=0.85)
    else:
        keeps = [(src_start, src_end)]

    ass_file = tmp_dir / f"overlay_{index}_{'co' if is_cold_open else 'main'}.ass"
    badge = ("COMING UP · FINALE" if format_kind == "vs" else "COMING UP · #1") if is_cold_open else ""
    cap = _short_caption(seg.moment_title)
    if is_cold_open:
        lower = ""
        hold = max(1.5, min(3.8, (src_end - src_start) - 0.3))
    elif format_kind == "vs":
        lower = f"ROUND {index} · {seg.creator}".strip()
        if cap:
            lower = f"{lower} — {cap}"
        hold = 3.2
    else:
        rank = display_rank if display_rank is not None else index
        lower = f"#{rank} · {seg.creator}".strip()
        if cap:
            lower = f"{lower} — {cap}"
        hold = 3.2

    font_ref = resolve_font(settings.clip_font, f"{badge} {lower} {brand_name}")
    fonts_dir = str(Path(font_ref).parent) if font_ref else str(tmp_dir)
    _write_ass_overlay(
        ass_file,
        badge_text=badge,
        lower_third=lower,
        brand=brand_name,
        start_sec=0.25 if is_cold_open else 0.4,
        hold_sec=hold,
        sample_text=f"{badge} {lower} {brand_name}",
        fonts_dir=fonts_dir,
    )

    seek_pad = max(0.0, keeps[0][0])
    parts: list[str] = []
    joined: list[str] = []
    for i, (ka, kb) in enumerate(keeps):
        ra = max(0.0, ka - seek_pad)
        rb = max(ra + 0.1, kb - seek_pad)
        parts.append(f"[0:v]trim=start={ra:.3f}:end={rb:.3f},setpts=PTS-STARTPTS[v{i}]")
        if info.has_audio:
            parts.append(f"[0:a]atrim=start={ra:.3f}:end={rb:.3f},asetpts=PTS-STARTPTS[a{i}]")
            joined.extend([f"[v{i}]", f"[a{i}]"])
        else:
            joined.append(f"[v{i}]")

    if info.has_audio:
        parts.append(f"{''.join(joined)}concat=n={len(keeps)}:v=1:a=1[vcat][acat]")
    else:
        parts.append(f"{''.join(joined)}concat=n={len(keeps)}:v=1:a=0[vcat]")

    ass_arg = f"ass={_filter_path_arg(str(ass_file))}:fontsdir={_filter_path_arg(fonts_dir)}"
    parts.append(
        f"[vcat]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS},{ass_arg}[vout]"
    )

    inputs = ["-ss", f"{seek_pad:.3f}", "-i", os.path.abspath(seg.source_path)]
    if info.has_audio:
        parts.append(
            "[acat]acompressor=threshold=0.12:ratio=3:attack=20:release=200:makeup=2,"
            "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo,"
            "afade=t=in:d=0.05,asetpts=N/SR/TB[aout]"
        )
    else:
        seg_len = sum(b - a for a, b in keeps)
        inputs += ["-f", "lavfi", "-t", f"{seg_len:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        parts.append("[1:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=N/SR/TB[aout]")

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


def _render_end_tail(tail_frame_src: Path, out_path: Path, tmp_dir: Path, *, seconds: float = _END_SCREEN_SEC) -> float:
    """Clean 8s dimmed backdrop tail with gentle silent/ambient stereo track for YouTube end-screen elements."""
    frame_jpg = tmp_dir / "end_still.jpg"
    run_ffmpeg(["-sseof", "-0.4", "-i", str(tail_frame_src), "-frames:v", "1", "-q:v", "2", str(frame_jpg)], cwd=str(tmp_dir))
    run_ffmpeg(
        [
            "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(frame_jpg),
            "-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-filter_complex",
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},setsar=1,"
            f"gblur=sigma=18,eq=brightness=-0.18:saturation=0.85,fps={FPS},"
            f"fade=t=in:st=0:d=0.4,fade=t=out:st={max(0.0, seconds - 0.6):.3f}:d=0.6[v];"
            "[1:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=N/SR/TB[a]",
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
) -> RenderResult:
    """Render cold-open, transition stingers, segments, and end screen into a native 1920x1080 MP4."""
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".tmp.mp4")
    warnings = list(plan.warnings)

    with tempfile.TemporaryDirectory(prefix="longform-render-", ignore_cleanup_errors=True) as td:
        tmp_dir = Path(td)
        stinger = tmp_dir / "trans.mp4"
        _render_stinger(stinger, tmp_dir)
        stinger_dur = _probe_duration(stinger, _TRANSITION_SEC)

        parts_to_concat: list[Path] = []
        chapters: list[tuple[float, str]] = [(0.0, "Intro / Preview")]
        thumb_stamps: list[float] = []
        cursor = 0.0

        cold_file = tmp_dir / "part_00_cold_open.mp4"
        co_dur = _render_segment(
            plan.cold_open, cold_file, tmp_dir,
            index=0, is_cold_open=True, format_kind=plan.format_kind, brand_name=brand_name,
        )
        parts_to_concat.append(cold_file)
        cursor += co_dur

        total_segs = len(plan.segments)
        for idx, seg in enumerate(plan.segments, start=1):
            parts_to_concat.append(stinger)
            cursor += stinger_dur
            seg_file = tmp_dir / f"part_{idx:02d}.mp4"
            rank = (total_segs - idx + 1) if plan.format_kind != "vs" else idx
            dur = _render_segment(
                seg, seg_file, tmp_dir,
                index=idx, display_rank=rank, is_cold_open=False,
                format_kind=plan.format_kind, brand_name=brand_name,
            )
            parts_to_concat.append(seg_file)
            prefix = f"Round {idx}" if plan.format_kind == "vs" else f"#{rank}"
            label = f"{prefix} {seg.creator} — {seg.moment_title}".strip(" —")
            chapters.append((cursor, label))
            thumb_stamps.append(cursor + min(max(1.0, dur * 0.45), max(1.0, dur - 0.5)))
            cursor += dur
            reclaim_memory()

        if include_end_screen and parts_to_concat:
            tail_file = tmp_dir / "part_tail.mp4"
            _render_end_tail(parts_to_concat[-1], tail_file, tmp_dir, seconds=_END_SCREEN_SEC)
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
        timestamps_for_thumbs=tuple(thumb_stamps[:6]),
        warnings=tuple(warnings),
    )
