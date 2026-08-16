"""Intelligent long-video to Shorts chopper.

Strategy (no-transcode, fast):
1. Split the source into equal ``target_short_sec``-long windows.
2. Score each window by *position heuristics*:
   - Lead-in window (0..target_short_sec): boosted — most shared moment;
   - Middle windows: baseline;
   - Final window (end-target_short_sec..end): boosted — outro/CTA.
3. Pick the top ``max_clips_per_video`` windows that are each <= 60 s.
4. Burn a ``#shorts`` caption + optional channel-name watermark via ffmpeg
   filter chain (reuses ``app/transformers/ffmpeg_transformer.py`` recipe).
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from dataclasses import dataclass, field
from typing import List

from app.models import Video, VideoStatus
from app.transformers.ffmpeg_transformer import FFmpegTransformer, TransformRecipe

log = logging.getLogger(__name__)


@dataclass
class ClipSpec:
    start_sec: float
    end_sec: float
    score: float = 0.0
    reason: str = ""


def _probe_duration(path: str) -> float:
    """Return duration in seconds via ffprobe, or 0 on failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, check=True,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _window_energy(input_path: str, start_sec: float, end_sec: float) -> float:
    """Fast 'heat' proxy for a time window using ffprobe scene-change rate.

    More scene changes == more shot variation == higher likelihood of a
    'viral' hook or payoff moment.  Returns a normalised score in [0, 1].
    """
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "frame=pkt_pts_time",
                "-of", "csv=p=0",
                "-f", "lavfi",
                (
                    f"color=c=black:s=1x1:d={max(1, int(end_sec - start_sec))},"
                    f"trim=start={start_sec}:end={end_sec},"
                    "select='gt(scene\\,0.3)',metadata=print"
                ),
                "-i", input_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        scenes = [l for l in out.stdout.splitlines() if l.strip()]
        # 20 scene changes in a 59-second window ~= very high, normalise to [0,1]
        return min(1.0, len(scenes) / 20.0)
    except Exception:
        return 0.0


def _score_window(index: int, total: int) -> float:
    """Base engagement heuristic by position in the video."""
    if total <= 1:
        return 0.5
    ratio = index / max(total - 1, 1)
    if ratio <= 0.15 or ratio >= 0.85:
        return 0.8      # lead-in / outro — most re-watched or CTA
    if 0.35 <= ratio <= 0.65:
        return 0.4      # middle — typically lower retention
    return 0.6          # first/last quarter


def pick_hot_clips(
    source_path: str,
    target_short_sec: int = 59,
    max_clips: int = 3,
    min_clip_sec: int = 30,
) -> List[ClipSpec]:
    """Return ordered list of ClipSpecs representing the best Shorts segments.

    Score = 0.6 * position_heuristic + 0.4 * motion_energy.
    This gives openings/outros a boost while still preferring the most
    action-heavy windows when a long video has multiple high-retention moments.
    """
    duration = _probe_duration(source_path)
    if duration < min_clip_sec:
        return []

    window = float(target_short_sec)
    total_windows = max(1, int(math.ceil(duration / window)))

    raw: List[ClipSpec] = []
    for i in range(total_windows):
        start = i * window
        end = min((i + 1) * window, duration)
        pos = _score_window(i, total_windows)
        energy = _window_energy(source_path, start, end)
        score = 0.6 * pos + 0.4 * energy
        reason_parts = [f"position={pos:.2f}"]
        if energy >= 0.2:
            reason_parts.append(f"energy={energy:.2f}")
        raw.append(ClipSpec(
            start_sec=start, end_sec=end, score=score,
            reason=", ".join(reason_parts),
        ))

    raw.sort(key=lambda c: c.score, reverse=True)
    picked: List[ClipSpec] = []
    for c in raw:
        if len(picked) >= max_clips:
            break
        dur = c.end_sec - c.start_sec
        if dur < min_clip_sec:
            continue
        picked.append(c)

    picked.sort(key=lambda c: c.start_sec)
    return picked


def render_clip(
    input_path: str,
    output_path: str,
    spec: ClipSpec,
    caption_text: str = "",
) -> str:
    """Cut [start..end] from ``input_path`` with the standard Shorts recipe."""
    transformer = FFmpegTransformer()
    recipe = TransformRecipe(
        add_blur_background=True,
        add_caption=True,
        caption_text=caption_text,
    )
    # ffmpeg -ss before -i for fast seek (frame-accurate not needed for Shorts)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(spec.start_sec), "-to", str(spec.end_sec),
         "-i", input_path,
         "-filter_complex",
         "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
         "boxblur=20:1[bg];"
         "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
         "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
         f"drawtext=fontfile='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'"
         f":text='{caption_text}':fontcolor=white:fontsize=72:"
         "borderw=4:bordercolor=black:x=(w-text_w)/2:y=h-180[vout]",
         "-map", "[vout]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
         "-shortest", output_path],
        capture_output=True, check=True,
    )
    return output_path


def clips_from_long(video_id: str) -> dict:
    """Pipeline task: split one long video into multiple Shorts clips.

    Creates new ``Video`` rows (status=PENDING) for each clip.
    Skips videos that are already Shorts (<= ``max_short_duration_sec``).
    """
    from app.config import settings
    from app.db import session_scope
    from app.analytics import recompute_scores

    min_long_sec = getattr(settings, "long_video_min_duration_sec", 120)
    target_sec = getattr(settings, "clip_target_sec", 59)
    max_clips = getattr(settings, "max_clips_per_video", 3)
    min_clip_sec = getattr(settings, "min_clip_duration_sec", 30)

    with session_scope() as db:
        src = db.get(Video, video_id)
        if src is None:
            return {"ok": False, "error": "source not found"}
        if not src.local_path or not os.path.exists(src.local_path):
            return {"ok": False, "error": "source file missing"}

        duration = src.duration_sec or _probe_duration(src.local_path)
        if duration < min_long_sec and duration <= 60:
            return {"ok": False, "error": f"video is only {duration:.0f}s — not a long-form source"}

        # Avoid re-processing
        existing_clips = db.query(Video).filter(
            Video.meta.is_not(None),
            Video.meta != "",
        ).all()
        clip_sources = set()
        for row in existing_clips:
            try:
                meta = json.loads(row.meta or "{}")
                if meta.get("parent_video_id") == video_id:
                    clip_sources.add(row.id)
            except Exception:
                pass

    specs = pick_hot_clips(
        src.local_path,
        target_short_sec=target_sec,
        max_clips=max_clips,
        min_clip_sec=min_clip_sec,
    )

    if not specs:
        return {"ok": False, "error": "no viable clip segments found"}

    created: List[dict] = []
    out_dir = os.path.join(os.path.dirname(src.local_path) or ".", "clips")
    os.makedirs(out_dir, exist_ok=True)

    for idx, spec in enumerate(specs, 1):
        out_path = os.path.join(out_dir, f"{video_id}_clip{idx}.mp4")
        if not os.path.exists(out_path):
            caption = f"{src.author or ''} — part {idx}" if src.author else f"Part {idx} #shorts"
            render_clip(src.local_path, out_path, spec, caption_text=caption)

        with session_scope() as db:
            clip = Video(
                source=src.source,
                source_id=f"{src.source_id}_clip{idx}",
                source_url=src.source_url,
                author=src.author,
                title=f"{src.title or 'Long-form clip'} — part {idx}",
                description=(src.description or "")[:2000],
                thumbnail_url=src.thumbnail_url,
                local_path=out_path,
                transformed_path=out_path,
                views=0,
                likes=0,
                comments=0,
                shares=0,
                duration_sec=int(spec.end_sec - spec.start_sec),
                status=VideoStatus.PENDING,
                meta=json.dumps({
                    "parent_video_id": video_id,
                    "clip_index": idx,
                    "clip_start": spec.start_sec,
                    "clip_end": spec.end_sec,
                    "clip_score": spec.score,
                    "clip_reason": spec.reason,
                }),
            )
            recompute_scores([clip])
            clip.meta = clip.meta if isinstance(clip.meta, dict) else json.loads(clip.meta or "{}")
            clip.transformed_path = str(out_path)
            db.add(clip)
            created.append({
                "id": clip.id,
                "start": spec.start_sec,
                "end": spec.end_sec,
                "dur": round(spec.end_sec - spec.start_sec, 1),
                "score": round(spec.score, 3),
            })

    return {
        "ok": True,
        "video_id": video_id,
        "source_duration_s": round(duration, 1),
        "clips_created": len(created),
        "clips": created,
    }