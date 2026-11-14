"""Rendering one moment into a finished 1080x1920 clip.

The filter graph is built from small pure functions so its shape can be tested,
but filter graphs only truly fail at ffmpeg's parser, so any change here needs
the ``slow`` tier in tests/test_clipper_render.py.

Motion graphics on every clip: an animated hook, word-by-word captions, a top
progress bar, an opening snap-zoom, and punch-ins that hide the jump cuts.
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from app.config import settings
from app.transformers.fonts import resolve_font

from .captions import Layout, build_ass, font_family, framed_layout
from .edit import BEAT_SECONDS, CARD, EditPlan
from .faces import face_centre, speaker_position
from .media import MediaError, MediaInfo, probe, read_ffmpeg, run_ffmpeg
from .moments import Moment
from .sfx import IMPACT, POP, WHOOSH, Cue, build_track
from .signals import Signals
from .timeline import FPS, Segment, keep_segments, on_grid, output_time, source_time, total_duration
from .transcribe import Word

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1080, 1920
_TARGET_RATIO = WIDTH / HEIGHT
_RATIO_TOLERANCE = 0.03
_PUNCH_ZOOM = 0.07
_SNAP_ZOOM = 0.10
_SNAP_SEC = 0.30
_PUNCH_MIN_GAP_SEC = 2.0
_VISUAL_PUNCHES = 3
_VISUAL_PUNCH_SEC = 1.2
# Subject moves this far across the frame (as a share of width): a static crop
# would lose it, so keep the whole picture over a blurred fill.
_WIDE_SPREAD = 0.22
# A framed picture has no spare width, so its zooms stay gentle and cut off less of it.
_FRAMED_ZOOM_SHARE = 0.5
# With the speaker found, the framed picture moves in on them: a wide two-shot becomes a single.
_FOCUS_ZOOM = 1.35
# Moves smaller than this are detector jitter, not a new speaker.
_FOCUS_STEADY = 0.08
_FOCUS_MIN_PIECE_SEC = 0.8
_MUSIC_FADE_IN = 0.8
# A clip ends a beat after its last word, and the voice fades, instead of stopping on the syllable.
_END_HOLD = 0.45
_END_FADE = 0.35
# Every mix is rendered stereo so a stereo music bed keeps its width; a mono source becomes centred dual-mono.
_STEREO = "aformat=channel_layouts=stereo"
# The share of a framed picture cut from the bottom when the source burns in its own captions,
# so ours can replace them instead of doubling them.
_CAPTION_BAND = 0.2
_MUSIC_FADE_OUT = 1.5
_BACKDROP_LOOK = "eq=brightness=-0.22:saturation=0.85"
# Share of the frame's height the backdrop is drawn from, starting this far down: clear of
# captions in the lower third and of logos in the top corners.
_BACKDROP_KEEP = 0.62
_BACKDROP_TOP = 0.08
_AUDIO_EDGE_FADE = 0.008
# Decode from this far before the clip: some containers do not seek to keyframes.
_PRE_ROLL_SEC = 10.0
_SHORTFALL_LIMIT_SEC = 0.5
# Boundaries are tested half a frame early, so each falls between two frames and
# rounding cannot move it onto the wrong side of one.
_HALF_FRAME = 0.5 / FPS
_SQUARE_PIXELS = "scale=trunc(iw*sar/2)*2:ih,setsar=1"
_CUT_SEARCH_FRAMES = 8
_CUT_WIDTH, _CUT_HEIGHT = 64, 36
# The edit's rhythm: how far each beat zooms, how soon the framing must change, and
# the slow push between changes. The high anchor keeps a head in frame as it grows.
_ZOOM_LEVELS = (1.0, 1.12, 1.26, 1.06, 1.18)
_RHYTHM_MIN_SEC = 1.0
_RHYTHM_MAX_SEC = 2.4
_PUSH_PER_SEC = 0.012
_FACE_ANCHOR = 0.16
_SHAKE_PX = 12
_SHAKE_SEC = 0.25
_FLASH_SEC = 0.07


@dataclass
class ClipPlan:
    segments: list[Segment]
    duration: float
    has_audio: bool
    crop_width: int
    crop_x_expr: str
    use_blur_fill: bool
    zoom_expr: str
    has_graphics: bool
    speed: float = 1.0
    shake_expr: str = "0"
    flash_expr: str = ""
    anchor_y: float = 0.5
    has_sfx: bool = False
    broll: tuple[tuple[float, float, str, float], ...] = ()  # output time, seconds, path, offset
    memes: tuple[tuple[float, float, str], ...] = ()  # output time, seconds, path (corner overlays)
    picture_height: int = 0  # above zero, the whole picture sits centred over its own backdrop
    picture_crop_bottom: float = 0.0  # share of the picture's height cut from the bottom
    music: str = ""  # a track looped under the clip, ducked under speech
    focus_x_expr: str = ""  # where the speaker's face is in the picture, 0 to 1, when known
    focus_y_expr: str = ""
    ass_path: str = "clip.ass"
    fonts_dir: str = "fonts"


_FILTER_QUOTE_CHARS = frozenset(":\\',;[] \t")


def escape_filter_path(path: str) -> str:
    """Escape a filesystem path for an ffmpeg filter option (e.g. ass='...':fontsdir='...')."""
    clean = "".join(ch for ch in str(path or "") if ch >= " " and ch != "\x7f")
    return clean.replace("\\", "/").replace(":", r"\:").replace("'", r"'\''")


def _filter_path_arg(path: str) -> str:
    raw = str(path or "")
    escaped = escape_filter_path(raw)
    if any(ch in _FILTER_QUOTE_CHARS for ch in raw):
        return f"'{escaped}'"
    return escaped


def _atempo_chain(speed: float) -> str:
    remaining = speed if math.isfinite(speed) and speed > 0 else 1.0
    stages: list[str] = []
    while remaining < 0.5 - 1e-9:
        stages.append("atempo=0.5")
        remaining /= 0.5
    while remaining > 2.0 + 1e-9:
        stages.append("atempo=2")
        remaining /= 2.0
    if not stages or abs(remaining - 1.0) > 1e-6:
        stages.append(f"atempo={remaining:g}")
    return ",".join(stages)


def _piecewise(pieces: Sequence[tuple[float, float]]) -> str:
    """Expression for a value that holds at each (start_time, value) until the next."""
    terms = []
    for index, (start, value) in enumerate(pieces):
        end = pieces[index + 1][0] if index + 1 < len(pieces) else None
        condition = f"gte(t,{start - _HALF_FRAME:.3f})" + (f"*lt(t,{end - _HALF_FRAME:.3f})" if end is not None else "")
        terms.append(f"{value:g}*{condition}")
    return "+".join(terms) or "0"


def _broll_shots(edit: EditPlan) -> tuple[tuple[float, float, str, float], ...]:
    shots = []
    for cutaway in edit.cutaways:
        when = edit.to_output(cutaway.at)
        if when is not None and cutaway.clip is not None:
            shots.append((when, cutaway.seconds, cutaway.clip.path, cutaway.clip.offset))
    return tuple(shots)


def _meme_shots(edit: EditPlan) -> tuple[tuple[float, float, str], ...]:
    shots = []
    for overlay in edit.meme_overlays:
        when = edit.to_output(overlay.at)
        if when is not None and overlay.clip is not None:
            shots.append((when, overlay.seconds, overlay.clip.path))
    return tuple(shots)


def rhythm_points(edit: EditPlan) -> list[float]:
    """When the framing changes, in output time: on cuts and graphics, and never long without one."""
    wanted = {0.0}
    for begin, _finish in edit.segments[1:]:
        when = edit.to_output(begin)
        if when is not None:
            wanted.add(when)
    for beat in edit.beats:
        when = edit.to_output(beat.at)
        if when is not None:
            wanted.add(when)
    for start, seconds, _path, _offset in _broll_shots(edit):
        wanted.update((start, start + seconds))
    points: list[float] = []
    for when in sorted(wanted):
        if not points or when - points[-1] >= _RHYTHM_MIN_SEC:
            points.append(when)
    # Fill long stretches, landing on word starts so a change falls with the speech.
    starts = [w.start for w in edit.output_words()]
    filled: list[float] = []
    for index, point in enumerate(points):
        filled.append(point)
        limit = points[index + 1] if index + 1 < len(points) else edit.duration
        current = point
        while limit - current > _RHYTHM_MAX_SEC:
            target = current + _RHYTHM_MAX_SEC * 0.8
            candidates = [s for s in starts if current + _RHYTHM_MIN_SEC <= s <= limit - _RHYTHM_MIN_SEC]
            next_point = min(candidates, key=lambda s: abs(s - target)) if candidates else target
            if next_point <= current + _RHYTHM_MIN_SEC / 2:
                break
            filled.append(next_point)
            current = next_point
    return filled


def _edit_zoom_expression(points: Sequence[float], duration: float) -> str:
    terms = []
    for index, start in enumerate(points):
        end = points[index + 1] if index + 1 < len(points) else duration + 1
        level = _ZOOM_LEVELS[index % len(_ZOOM_LEVELS)]
        # Half-open windows: both ends of between() would double the zoom on a shared frame.
        terms.append(f"gte(t,{start - _HALF_FRAME:.3f})*lt(t,{end - _HALF_FRAME:.3f})"
                     f"*({level:g}+{_PUSH_PER_SEC}*(t-{start:.3f}))")
    # Never below 1: a frame smaller than the canvas could not be cropped back to it.
    return f"{_SNAP_ZOOM}*max(0,1-t/{_SNAP_SEC})+max(1,min(1.5,{'+'.join(terms)}))"


def _edit_effects(edit: EditPlan) -> tuple[str, str]:
    """Camera shake and a white flash where graphics land."""
    shakes, flashes = [], []
    for beat in edit.beats:
        when = edit.to_output(beat.at)
        if when is None:
            continue
        flashes.append(f"between(t,{when - _HALF_FRAME:.3f},{when + _FLASH_SEC:.3f})")
        if beat.style != CARD:
            shakes.append(f"between(t,{when:.3f},{when + _SHAKE_SEC:.3f})")
    shake = f"{_SHAKE_PX}*sin(t*75)*min(1,{'+'.join(shakes)})" if shakes else "0"
    return shake, "+".join(flashes)


def sound_cues(edit: EditPlan, points: Sequence[float]) -> list[Cue]:
    cues = [Cue(0.0, IMPACT)]
    for index, when in enumerate(points[1:], 1):
        if index % 2 == 0:
            cues.append(Cue(max(0.0, when - 0.14), WHOOSH))
    for beat in edit.beats:
        when = edit.to_output(beat.at)
        if when is not None:
            cues.append(Cue(when, POP if beat.style == CARD else IMPACT))
    for start, seconds, _path, _offset in _broll_shots(edit):
        cues.append(Cue(max(0.0, start - 0.14), WHOOSH))
        cues.append(Cue(max(0.0, start + seconds - 0.14), WHOOSH))
    return cues


def _shot_edges(moment: Moment, signals: Signals) -> list[float]:
    return [moment.start] + [c for c in signals.cuts if math.isfinite(c) and moment.start < c < moment.end] + [moment.end]


def _crop_pieces(moment: Moment, segments: Sequence[Segment], signals: Signals, info: MediaInfo,
                 crop_width: int, exact_cuts: Optional[dict[float, float]] = None,
                 faces: Optional[dict[float, float]] = None, speed: float = 1.0) -> list[tuple[float, float]]:
    """One crop position per shot: on the person if there is one, else on the motion."""
    safe_speed = speed if math.isfinite(speed) and speed > 0 else 1.0
    info_w = max(2, int(info.width) // 2 * 2)
    cw = min(info_w, max(2, int(crop_width) // 2 * 2))
    exact = exact_cuts or {}
    known_faces = faces or {}
    edges = _shot_edges(moment, signals)
    pieces: list[tuple[float, float]] = []
    for first, last in zip(edges, edges[1:]):
        when = output_time(segments, exact.get(first, first))
        if when is None:
            continue
        when /= safe_speed
        centre = known_faces.get(first)
        if centre is None or not math.isfinite(centre):
            centre = signals.centre_between(first, last)
        if not math.isfinite(centre):
            centre = 0.5
        centre = min(1.0, max(0.0, centre))
        left = min(max(0.0, centre * info_w - cw / 2), float(max(0, info_w - cw)))
        left = float(int(left) // 2 * 2)
        if pieces and abs(pieces[-1][0] - when) < 1 / FPS:
            pieces[-1] = (pieces[-1][0], left)
        elif not pieces or pieces[-1][1] != left:
            pieces.append((when, left))
    if pieces:
        pieces[0] = (0.0, pieces[0][1])
    return pieces or [(0.0, float(max(0, (info_w - cw) // 2) // 2 * 2))]


def _zoom_windows(moment: Moment, segments: Sequence[Segment], signals: Signals) -> list[tuple[float, float]]:
    """When to punch in, in output time."""
    windows: list[tuple[float, float]] = []
    if len(segments) > 1:
        # Every other jump cut punches in, so the cut reads as an edit.
        elapsed, last_change, zoomed, opened_at = 0.0, 0.0, False, 0.0
        for begin, finish in segments:
            if elapsed - last_change >= _PUNCH_MIN_GAP_SEC:
                if zoomed:
                    windows.append((opened_at, elapsed))
                else:
                    opened_at = elapsed
                zoomed, last_change = not zoomed, elapsed
            elapsed += finish - begin
        if zoomed:
            windows.append((opened_at, elapsed))
        return windows
    # No jump cuts, so punch on the loudest beats instead.
    first, last = int(moment.start), max(int(moment.start) + 1, int(moment.end))
    loudest = sorted(range(first, min(last, signals.seconds)),
                     key=lambda s: signals.loudness[s], reverse=True)
    for second in loudest:
        when = output_time(segments, float(second))
        if when is None or any(abs(when - a) < _PUNCH_MIN_GAP_SEC * 2 for a, _ in windows):
            continue
        windows.append((when, when + _VISUAL_PUNCH_SEC))
        if len(windows) == _VISUAL_PUNCHES:
            break
    return sorted(windows)


def _zoom_expression(windows: Sequence[tuple[float, float]]) -> str:
    punches = "+".join(f"between(t,{a - _HALF_FRAME:.3f},{b - _HALF_FRAME:.3f})" for a, b in windows)
    expr = f"1+{_SNAP_ZOOM}*max(0,1-t/{_SNAP_SEC})"
    if punches:
        expr += f"+{_PUNCH_ZOOM}*min(1,{punches})"
    return expr


def _needs_side_crop(info: MediaInfo) -> bool:
    if info.width <= 0 or info.height <= 0:
        return False
    ratio = info.width / info.height
    return ratio > _TARGET_RATIO + _RATIO_TOLERANCE


def _picture_height(info: MediaInfo, crop_bottom: float = 0.0) -> int:
    """Height of the centred picture, or 0 when this source fills the canvas instead."""
    if settings.clip_layout != "framed" or not _needs_side_crop(info):
        return 0
    safe_crop = min(0.9, max(0.0, crop_bottom)) if math.isfinite(crop_bottom) else 0.0
    return max(2, round(WIDTH * info.height * (1 - safe_crop) / info.width / 2) * 2)


def with_end_hold(segments: Sequence[Segment], source_duration: float) -> list[Segment]:
    """Let the last segment run on briefly, so the clip does not stop dead on its final syllable."""
    held = list(segments)
    if held:
        begin, finish = held[-1]
        limit = source_duration if math.isfinite(source_duration) and source_duration > 0 else finish + _END_HOLD
        held[-1] = (begin, max(begin, on_grid(min(finish + _END_HOLD, limit))))
    return held


def speaker_pieces(source: str, info: MediaInfo, signals: Signals, moment: Moment, segments: Sequence[Segment],
                   edit: Optional[EditPlan], speed: float) -> list[tuple[float, float, float]]:
    """(clip time, x, y) of the person talking. It changes only where the framing changes anyway:
    at a beat of the edit or a cut in the footage, so a switch to the other speaker reads as an edit."""
    safe_speed = speed if math.isfinite(speed) and speed > 0 else 1.0
    duration = total_duration(segments) / safe_speed
    points = set(rhythm_points(edit)) if edit else {0.0}
    for cut in signals.cuts:
        if math.isfinite(cut) and moment.start < cut < moment.end:
            when = output_time(segments, cut)
            if when is not None:
                points.add(when / safe_speed)
    bounds: list[float] = []
    for point in sorted(p for p in points if 0 <= p < duration):
        if not bounds or point - bounds[-1] >= _FOCUS_MIN_PIECE_SEC:
            bounds.append(point)
    bounds = bounds or [0.0]
    pieces: list[tuple[float, float, float]] = []
    for index, start in enumerate(bounds):
        end = bounds[index + 1] if index + 1 < len(bounds) else duration
        found = speaker_position(source, source_time(segments, start * safe_speed), source_time(segments, end * safe_speed), info)
        if found is None:
            continue
        if pieces and abs(found[0] - pieces[-1][1]) < _FOCUS_STEADY and abs(found[1] - pieces[-1][2]) < _FOCUS_STEADY:
            continue
        pieces.append((start, found[0], found[1]))
    if pieces:
        pieces[0] = (0.0, pieces[0][1], pieces[0][2])
    return pieces


def plan_clip(moment: Moment, words: Sequence[Word], signals: Signals, info: MediaInfo, *,
              has_graphics: bool, exact_cuts: Optional[dict[float, float]] = None,
              faces: Optional[dict[float, float]] = None, edit: Optional[EditPlan] = None,
              focus: Sequence[tuple[float, float, float]] = (), crop_bottom: float = 0.0) -> ClipPlan:
    segments = edit.segments if edit else keep_segments(moment.start, moment.end, words, settings.clip_silence_gap_sec)
    segments = with_end_hold(segments, info.duration)
    raw_speed = edit.speed if edit else 1.0
    speed = raw_speed if math.isfinite(raw_speed) and raw_speed > 0 else 1.0
    safe_crop_bottom = min(0.9, max(0.0, crop_bottom)) if math.isfinite(crop_bottom) else 0.0
    picture_height = _picture_height(info, safe_crop_bottom)
    side_crop = _needs_side_crop(info) and not picture_height
    even_w = max(2, int(info.width) // 2 * 2) if info.width > 0 else WIDTH
    crop_width = max(2, int(info.height * _TARGET_RATIO) // 2 * 2) if side_crop else even_w
    use_blur = (side_crop and moment.kind == "visual" and not faces
                and signals.centre_spread(moment.start, moment.end) > _WIDE_SPREAD)
    pieces = (_crop_pieces(moment, segments, signals, info, crop_width, exact_cuts, faces, speed)
              if side_crop else [(0.0, 0.0)])
    duration = total_duration(segments) / speed
    if edit:
        zoom = _edit_zoom_expression(rhythm_points(edit), duration)
        shake, flash = _edit_effects(edit)
    else:
        zoom, shake, flash = _zoom_expression(_zoom_windows(moment, segments, signals)), "0", ""
    focus = focus if picture_height else ()
    if focus:
        zoom = f"{_FOCUS_ZOOM:g}+(({zoom})-1)*{_FRAMED_ZOOM_SHARE:g}"
    elif picture_height:
        zoom = f"1+(({zoom})-1)*{_FRAMED_ZOOM_SHARE:g}"
    return ClipPlan(
        segments=segments,
        duration=duration,
        has_audio=info.has_audio,
        crop_width=crop_width,
        crop_x_expr=_piecewise(pieces),
        use_blur_fill=use_blur,
        zoom_expr=zoom,
        has_graphics=has_graphics,
        speed=speed,
        shake_expr=shake,
        flash_expr=flash,
        anchor_y=_FACE_ANCHOR if edit and not picture_height else 0.5,
        has_sfx=bool(edit) and info.has_audio and settings.clip_sfx,
        broll=_broll_shots(edit) if edit else (),
        memes=_meme_shots(edit) if edit else (),
        picture_height=picture_height,
        picture_crop_bottom=safe_crop_bottom if picture_height else 0.0,
        focus_x_expr=_piecewise([(t, round(x, 3)) for t, x, _y in focus]) if focus else "",
        focus_y_expr=_piecewise([(t, round(y, 3)) for t, _x, y in focus]) if focus else "",
    )


def _framing(plan: ClipPlan, info: MediaInfo) -> str:
    """Filters taking [cut] to a full canvas [canvas], or to [backdrop] and [picture] when framed."""
    if plan.picture_height:
        blur = f",boxblur={settings.clip_backdrop_blur}:1" if settings.clip_backdrop_blur > 0 else ""
        return (f"[cut]split=2[backsrc][picsrc];"
                # The backdrop leaves out the bottom of the frame, where a source's own captions sit: blown
                # up behind the picture they would show every word a second time, huge and blurred.
                f"[backsrc]crop=iw:ih*{_BACKDROP_KEEP:g}:0:ih*{_BACKDROP_TOP:g},"
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase:flags=fast_bilinear,"
                f"crop={WIDTH}:{HEIGHT},{_BACKDROP_LOOK}{blur},setsar=1[backdrop];"
                + (f"[picsrc]crop=iw:trunc(ih*{1 - plan.picture_crop_bottom:g}/2)*2:0:0," if plan.picture_crop_bottom
                   else "[picsrc]")
                + f"scale={WIDTH}:{plan.picture_height}:flags=lanczos,setsar=1[picture]")
    if plan.use_blur_fill:
        return (f"[cut]split=2[bgsrc][fgsrc];"
                f"[bgsrc]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={WIDTH}:{HEIGHT},boxblur=25:2[bg];"
                f"[fgsrc]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[canvas]")
    even_w = max(2, int(info.width) // 2 * 2) if info.width > 0 else WIDTH
    even_h = max(2, int(info.height) // 2 * 2) if info.height > 0 else HEIGHT
    if plan.crop_width < even_w:
        return (f"[cut]crop={plan.crop_width}:{even_h}:x='{plan.crop_x_expr}':y=0,"
                f"scale={WIDTH}:{HEIGHT}:flags=lanczos,setsar=1[canvas]")
    return (f"[cut]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={WIDTH}:{HEIGHT},setsar=1[canvas]")


def _crop_offset(focus_expr: str, size: int, zoom: str, room: str, anchor: str) -> str:
    """Crop offset along one axis: centred on the speaker when known, else at a fixed share of the spare room."""
    if focus_expr:
        return f"({size}*({zoom})*({focus_expr})-{size // 2})"
    return f"{room}*{anchor}"


def build_filtergraph(plan: ClipPlan, info: MediaInfo, seek: float) -> str:
    count = len(plan.segments)
    video_labels = [f"[vs{i}]" for i in range(count)]
    audio_labels = [f"[as{i}]" for i in range(count)]
    # fps and aresample put both streams on one clock starting at the seek point.
    # Re-zeroing each on its own first frame would lose any real offset between them.
    parts = [f"[0:v]{_SQUARE_PIXELS},fps={FPS}:start_time=0"
             + (f",split={count}{''.join(video_labels)}" if count > 1 else "[vs0]")]
    if plan.has_audio:
        parts.append("[0:a]aresample=async=1:first_pts=0,apad"
                     + (f",asplit={count}{''.join(audio_labels)}" if count > 1 else "[as0]"))
    joined = []
    for index, (begin, finish) in enumerate(plan.segments):
        a, b = max(0.0, begin - seek), finish - seek
        parts.append(f"[vs{index}]trim=start={a:.4f}:end={b:.4f},setpts=PTS-STARTPTS[v{index}]")
        joined.append(f"[v{index}]")
        if plan.has_audio:
            fade_out = max(0.0, b - a - _AUDIO_EDGE_FADE)
            parts.append(f"[as{index}]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS,"
                         f"afade=t=in:d={_AUDIO_EDGE_FADE},afade=t=out:st={fade_out:.4f}:d={_AUDIO_EDGE_FADE}[a{index}]")
            joined.append(f"[a{index}]")
    audio_flag = 1 if plan.has_audio else 0
    safe_speed = plan.speed if math.isfinite(plan.speed) and plan.speed > 0 else 1.0
    video_out, audio_out = "[cut]", "[ajoined]" if plan.has_audio else ""
    if safe_speed != 1.0:
        video_out = "[joined]"
    parts.append(f"{''.join(joined)}concat=n={count}:v=1:a={audio_flag}{video_out}{audio_out}")
    if safe_speed != 1.0:
        parts.append(f"[joined]setpts=PTS/{safe_speed:g},fps={FPS}[cut]")
    parts.append(_framing(plan, info))
    # crop's own input size stays fixed at the first frame's, so its position has
    # to come from the zoom expression rather than from iw and ih.
    zoom = plan.zoom_expr
    shake = plan.shake_expr
    # crop only clamps against the first frame's size, which is wrong as the zoom changes,
    # so the position must stay inside this frame's real margins. A shake past them
    # reads outside the image, and ffmpeg crashes.
    height = max(2, plan.picture_height // 2 * 2) if plan.picture_height else HEIGHT
    room_x = f"(2*trunc({WIDTH // 2}*({zoom}))-{WIDTH})"
    room_y = f"(2*trunc({height // 2}*({zoom}))-{height})"
    chain = (f"{'[picture]' if plan.picture_height else '[canvas]'}"
             f"scale=w='2*trunc({WIDTH // 2}*({zoom}))':h='2*trunc({height // 2}*({zoom}))':eval=frame,"
             f"crop={WIDTH}:{height}:x='clip({_crop_offset(plan.focus_x_expr, WIDTH, zoom, room_x, '0.5')}+({shake}),0,{room_x})':"
             f"y='clip({_crop_offset(plan.focus_y_expr, height, zoom, room_y, f'{plan.anchor_y:g}')}+({shake}),0,{room_y})',setsar=1")
    # Cutaways replace the picture only, so a framed clip keeps its shape through them.
    # They go under the graphics, so captions stay on top.
    first_input = 3 if plan.has_sfx else 2
    top = (HEIGHT - height) // 2
    for index, (start, seconds, _path, _offset) in enumerate(plan.broll):
        parts.append(f"[{first_input + index}:v]scale={WIDTH}:{height}:force_original_aspect_ratio=increase,"
                     f"crop={WIDTH}:{height},setsar=1,fps={FPS},setpts=PTS-STARTPTS+{start:.3f}/TB[broll{index}]")
        parts.append(chain + f"[under{index}]")
        parts.append(f"[under{index}][broll{index}]overlay=eof_action=pass:repeatlast=0:"
                     f"enable='between(t,{start - _HALF_FRAME:.3f},{start + seconds - _HALF_FRAME:.3f})'[over{index}]")
        chain = f"[over{index}]null"
    # Meme GIFs sit top-right over the picture, under the captions, so reactions
    # flash without covering speech text or the Shorts buttons.
    meme_base = first_input + len(plan.broll)
    for index, (start, seconds, _path) in enumerate(plan.memes):
        parts.append(f"[{meme_base + index}:v]scale=300:-2:flags=lanczos,setsar=1,fps={FPS},"
                     f"setpts=PTS-STARTPTS+{start:.3f}/TB[meme{index}]")
        parts.append(chain + f"[munder{index}]")
        parts.append(f"[munder{index}][meme{index}]overlay=x='W-w-30':y=180:eof_action=pass:"
                     f"enable='between(t,{start - _HALF_FRAME:.3f},{start + seconds - _HALF_FRAME:.3f})'[mover{index}]")
        chain = f"[mover{index}]null"
    if plan.picture_height:
        parts.append(chain + "[framed]")
        chain = f"[backdrop][framed]overlay=x=0:y={top}"
    if plan.flash_expr:
        chain += f",drawbox=x=0:y=0:w=iw:h=ih:color=white@0.6:t=fill:enable='{plan.flash_expr}'"
    if plan.has_graphics:
        chain += f",ass={_filter_path_arg(plan.ass_path)}:fontsdir={_filter_path_arg(plan.fonts_dir)}"
    parts.append(chain + "[graphics]")
    parts.append(f"[graphics][1:v]overlay=x='-W+W*t/{max(plan.duration, 0.1):.3f}':y=0:shortest=1[vout]")
    if plan.has_audio:
        head = "[ajoined]"
        if safe_speed != 1.0:
            parts.append(f"{head}{_atempo_chain(safe_speed)}[atempoed]")
            head = "[atempoed]"
        # loudnorm emits large irregular blocks, so renumber them or -t cuts one off.
        fade_at = max(0.0, plan.duration - _END_FADE)
        normalised = (f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,asetpts=N/SR/TB,"
                      f"afade=t=out:st={fade_at:.3f}:d={_END_FADE}")
        # The whole mix is stereo so a stereo music bed keeps its width instead of being folded to
        # mono (earshot: "the mix is mono ... use stereo music and ambience"). The voice is centred
        # dual-mono and the music carries the sides; without music a clip is honestly near-mono.
        if plan.music:
            # The voice is levelled first and also keys the ducking, so music drops whenever someone talks.
            music_input = first_input + len(plan.broll) + len(plan.memes)
            fade_out = max(0.0, plan.duration - _MUSIC_FADE_OUT)
            parts.append(f"{head}{normalised},{_STEREO}[voice]")
            parts.append("[voice]asplit=2[vmix][vkey]")
            parts.append(f"[{music_input}:a]aresample=48000,{_STEREO},atrim=0:{plan.duration:.3f},asetpts=N/SR/TB,"
                         f"volume={settings.clip_music_db:g}dB,afade=t=in:d={_MUSIC_FADE_IN},"
                         f"afade=t=out:st={fade_out:.3f}:d={_MUSIC_FADE_OUT}[bed]")
            parts.append("[bed][vkey]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[ducked]")
            sfx = []
            if plan.has_sfx:
                parts.append(f"[2:a]{_STEREO}[sfxst]")
                sfx = ["[sfxst]"]
            mix = ["[vmix]", *sfx, "[ducked]"]
            parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0,"
                         f"alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,{_STEREO},asetpts=N/SR/TB[aout]")
        elif plan.has_sfx:
            # Effects go on after the voice is levelled, or a quiet recording would come
            # out with the thuds louder than the speech. level=0 stops the limiter
            # raising everything back up.
            parts.append(f"{head}{normalised},{_STEREO}[voice]")
            parts.append(f"[2:a]{_STEREO}[sfxst]")
            parts.append("[voice][sfxst]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                         f"alimiter=limit=0.84:attack=2:release=40:level=0:latency=1,{_STEREO},asetpts=N/SR/TB[aout]")
        else:
            parts.append(f"{head}{normalised},{_STEREO}[aout]")
    return ";".join(parts)


def _shift_words(words: Sequence[Word], segments: Sequence[Segment], start: float, end: float) -> list[Word]:
    """Words of this clip, on the clip's own clock."""
    shifted = []
    for word in words:
        if word.end <= start or word.start >= end:
            continue
        begin = output_time(segments, word.start)
        finish = output_time(segments, word.end)
        if begin is None:
            continue
        shifted.append(Word(word.text, begin, finish if finish is not None else total_duration(segments), word.confidence))
    return shifted


def _seek_args(seek: float) -> list[str]:
    # -noaccurate_seek keeps the frame before the seek point, so a sparse source does
    # not start on a later frame. At zero, -ss would also lose audio before the first frame.
    return ["-noaccurate_seek", "-ss", f"{seek:.3f}"] if seek > 0 else []


def _exact_cuts(source: str, seek: float, span: float, cuts: Sequence[float]) -> dict[float, float]:
    """Exact frame of each coarse cut, measured on the render's own clock.

    ``signals.cuts`` comes from a 4 fps scan. Moving the crop on that grid would leave
    the subject out of frame for a few frames at each cut.
    """
    if not cuts:
        return {}
    try:
        raw = read_ffmpeg([*_seek_args(seek), "-i", source, "-map", "0:v:0", "-an", "-t", f"{span:.3f}", "-vf",
                           f"{_SQUARE_PIXELS},fps={FPS}:start_time=0,"
                           f"scale={_CUT_WIDTH}:{_CUT_HEIGHT}:flags=fast_bilinear,format=gray",
                           "-f", "rawvideo", "-"])
    except MediaError as exc:
        log.warning("render: could not refine cut times: %s", exc)
        return {}
    pixels = np.frombuffer(raw, dtype=np.uint8)
    count = pixels.size // (_CUT_WIDTH * _CUT_HEIGHT)
    if count < 2:
        return {}
    frames = pixels[:count * _CUT_WIDTH * _CUT_HEIGHT].reshape(count, _CUT_HEIGHT, _CUT_WIDTH).astype(np.int16)
    diffs = np.abs(frames[1:] - frames[:-1]).mean(axis=(1, 2))
    exact: dict[float, float] = {}
    for cut in cuts:
        centre = round((cut - seek) * FPS)
        low, high = max(1, centre - _CUT_SEARCH_FRAMES), min(count, centre + _CUT_SEARCH_FRAMES + 1)
        if low < high:
            exact[cut] = seek + (low + int(np.argmax(diffs[low - 1:high - 1]))) / FPS
    return exact


def render_moment(source: str, info: MediaInfo, signals: Signals, moment: Moment,
                  words: Sequence[Word], out_path: str, edit: Optional[EditPlan] = None, creator: str = "",
                  music: Optional[str] = None, captions: bool = True, source_captioned: bool = False,
                  brand: Optional[str] = None) -> ClipPlan:
    """Write the finished clip to ``out_path`` and return its plan.

    ``source_captioned`` means the footage burns in its own captions. In the framed layout their band is cut
    from the bottom of the picture and ours replace them; otherwise ours are left out. ``captions=False``
    always leaves ours out. ``brand`` is the handle shown on screen: a managed channel passes its own, and
    ``None`` means ``BRAND_NAME``."""
    # Resolve the font from the clip's own text: a Devanagari clip gets a Devanagari font (if installed)
    # for its captions and overlays, while a Latin clip resolves the same font as before.
    sample = " ".join(w.text for w in words) + f" {moment.title or ''} {creator}"
    font = resolve_font(settings.clip_font, sample)
    # Only speech clips close up their pauses; captions apply to either kind.
    cut_on = words if moment.kind == "speech" else []
    if edit:
        segments = edit.segments
        clip_words = edit.output_words()
        beats = [(edit.to_output(b.at), b.text, b.style, b.label, BEAT_SECONDS[b.style]) for b in edit.beats]
    else:
        segments = keep_segments(moment.start, moment.end, cut_on, settings.clip_silence_gap_sec)
        clip_words = _shift_words(words, segments, moment.start, moment.end)
        beats = []
    segments = with_end_hold(segments, info.duration)
    crop_bottom = _CAPTION_BAND if source_captioned and _picture_height(info) else 0.0
    if not captions or (source_captioned and not crop_bottom):
        clip_words = []
    brand = settings.brand_name if brand is None else brand
    has_text = bool(font and os.path.isfile(font)) and bool(clip_words or moment.hook or creator or brand)
    if total_duration(segments) <= 0:
        raise MediaError("clip has no duration")

    seek = max(0.0, on_grid(segments[0][0] - _PRE_ROLL_SEC))
    exact_cuts = faces = None
    # Nothing to follow when the whole picture is on screen.
    if _needs_side_crop(info) and not _picture_height(info):
        edges = _shot_edges(moment, signals)
        exact_cuts = _exact_cuts(os.path.abspath(source), seek, segments[-1][1] - seek + 0.5, edges[1:-1])
        faces = {}
        for first, last in zip(edges, edges[1:]):
            centre = face_centre(os.path.abspath(source), first, last, info)
            if centre is not None:
                faces[first] = centre
    focus: list[tuple[float, float, float]] = []
    if _picture_height(info) and moment.kind == "speech":
        focus = speaker_pieces(os.path.abspath(source), info, signals, moment, segments, edit,
                               edit.speed if edit else 1.0)
        # Face heights were measured on the whole frame; the picture may have lost its bottom band.
        focus = [(t, x, min(1.0, y / (1 - crop_bottom))) for t, x, y in focus]
    plan = plan_clip(moment, cut_on, signals, info, has_graphics=has_text, exact_cuts=exact_cuts, faces=faces,
                     edit=edit, focus=focus, crop_bottom=crop_bottom)
    if music and info.has_audio:
        plan.music = os.path.abspath(music)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    # A row may already point at out_path, so a half-written file must never appear there.
    partial = f"{out_path}.tmp.mp4"
    try:
        with tempfile.TemporaryDirectory(prefix="clipper-render-", ignore_cleanup_errors=True) as tmp:
            # Relative names keep paths, and their colons and quotes, out of the graph.
            if has_text:
                fonts_dir = os.path.join(tmp, "fonts")
                os.makedirs(fonts_dir)
                shutil.copy(font, fonts_dir)
                with open(os.path.join(tmp, "clip.ass"), "w", encoding="utf-8") as handle:
                    handle.write(build_ass(clip_words, moment.hook, plan.duration, font_family(font), font, beats, creator=creator, brand=brand,
                                         layout=framed_layout(plan.picture_height) if plan.picture_height else Layout()))
            if plan.has_sfx:
                build_track(os.path.join(tmp, "sfx.wav"), plan.duration, sound_cues(edit, rhythm_points(edit)))
            # The bar needs an end: an endless source keeps ffmpeg running forever if
            # the moment turns out to have no frames.
            args = [*_seek_args(seek), "-i", os.path.abspath(source),
                    "-f", "lavfi", "-i", f"color=c=0xFFE500:s={WIDTH}x10:r={FPS}:d={plan.duration + 1:.3f}",
                    *(["-i", "sfx.wav"] if plan.has_sfx else []),
                    *[item for start, seconds, path, offset in plan.broll
                      for item in ("-ss", f"{offset:.3f}", "-t", f"{seconds + 0.2:.3f}", "-i", os.path.abspath(path))],
                    *[item for _start, _seconds, path in plan.memes
                      for item in ("-i", os.path.abspath(path))],
                    *(["-stream_loop", "-1", "-i", plan.music] if plan.music else []),
                    "-filter_complex", build_filtergraph(plan, info, seek),
                    "-map", "[vout]"]
            if plan.has_audio:
                args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-r", str(FPS), "-movflags", "+faststart", "-t", f"{plan.duration:.3f}",
                     os.path.abspath(partial)]
            run_ffmpeg(args, cwd=tmp)
        if not os.path.exists(partial) or os.path.getsize(partial) < 1000:
            raise MediaError("render produced no usable output")
        produced = probe(partial).duration
        if produced < plan.duration - _SHORTFALL_LIMIT_SEC:
            raise MediaError(f"render came out short: {produced:.1f}s of {plan.duration:.1f}s")
        os.replace(partial, out_path)
    except OSError as exc:
        raise MediaError(f"could not write the clip: {exc}") from exc
    finally:
        try:
            os.unlink(partial)
        except OSError:
            pass
    return plan
