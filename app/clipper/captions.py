"""Caption and hook graphics, written as an ASS subtitle file.

ASS beats ``drawtext`` here for two reasons. The whole file is plain text outside
the filter graph, so an apostrophe in a transcript cannot break the graph. It can
also style and animate a single word, which is what word-by-word captions need.
"""
from __future__ import annotations

import logging
import math
import os
import re
import struct
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Sequence

from PIL import ImageFont

from app.transformers.ffmpeg_transformer import is_renderable

from .transcribe import Word

log = logging.getLogger(__name__)

_WIDTH, _HEIGHT = 1080, 1920
_HOOK_SECONDS = 2.8
_MAX_WORDS_PER_GROUP = 3
_MAX_CHARS_PER_LINE = 16
_MAX_GROUP_GAP_SEC = 0.5
_MAX_HOLD_SEC = 0.35
_CAPTION_SIZE, _CAPTION_PAD, _HIGHLIGHT_GROW = 92, 24, 1.12
_HOOK_SIZE, _HOOK_PAD = 72, 40
_BEAT_SIZES = {"stat": 240, "card": 170}
_BEAT_PAD = 60
# Short lines let a graphic fill the width instead of shrinking to one long row.
_BEAT_LINE_CHARS = 10
_COUNT_STEPS = 10
_COUNT_SECONDS = 0.45
_DIM_SHAPE = f"m 0 0 l {_WIDTH} 0 l {_WIDTH} {_HEIGHT} l 0 {_HEIGHT}"
# A whole quantity, so "40°C" and "$5 BILLION" count up. "B2B", "COVID-19", "24/7" and bare
# years stay still: counting them would show readings nobody said.
COUNTABLE = re.compile(r"^(?!(?:19|20)\d\d(?:\s|$))([$€£₹]?)(\d[\d,]*(?:\.\d+)?)((?:%|°[CF]?|[KMBX])?(?:\s+[^\d\s]+)*)$")
# ASS colours are &HBBGGRR.
_YELLOW = "&H00E5FF&"


_LOWEST_CAPTION_Y = 1440  # lower than this, a second line runs under the title Shorts draws


@dataclass(frozen=True)
class Layout:
    """Where each kind of text sits on the canvas."""

    caption_y: int = 1240
    hook_y: int = 250
    # Above the title and buttons Shorts draws over the bottom of the frame.
    credit_y: int = 1480
    brand_y: int = 175
    beat_y: int = 720


def framed_layout(picture_height: int) -> Layout:
    """Text in the bands around a centred picture. A tall picture leaves little room, so text may overlap it."""
    top = (_HEIGHT - picture_height) // 2
    bottom = top + picture_height
    credit_y = min(bottom + 34, 1530)
    # Two caption lines need this much room below the credit. Without it they go over the picture's lower edge.
    below = credit_y + 136
    caption_y = below if below <= _LOWEST_CAPTION_Y else min(bottom, credit_y) - 150
    return Layout(
        caption_y=caption_y,
        hook_y=max(200, top - 300),
        credit_y=credit_y,
        brand_y=max(160, top - 48),
        beat_y=_HEIGHT // 2 - 85,
    )


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")
_MAX_ASS_CENTIS = 9 * 360000 + 59 * 6000 + 59 * 100 + 99


def _clock(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds <= 0:
        centis = 0
    else:
        centis = min(_MAX_ASS_CENTIS, int(round(seconds * 100)))
    return f"{centis // 360000}:{centis // 6000 % 60:02d}:{centis // 100 % 60:02d}.{centis % 100:02d}"


def _safe(text: str) -> str:
    """Strip what ASS would read as an override tag, control byte, or line break."""
    text = _CONTROL_CHARS.sub("", str(text or ""))
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return re.sub(r"[\s\u2028\u2029]+", " ", text).strip()


def _safe_style_token(raw: str, fallback: str = "Arial") -> str:
    """Keep Fontname and Style fields free of CSV delimiters, line breaks, and ASS tags."""
    first_line = re.split(r"[\r\n\u2028\u2029]", str(raw or ""), maxsplit=1)[0]
    cleaned = _safe(first_line).replace(",", "").replace(":", "").replace(";", "").strip()
    return cleaned or fallback


def _normalised_words(words: Sequence[Word]) -> list[Word]:
    cleaned: list[Word] = []
    last_start = 0.0
    for word in words:
        if not _safe(word.text):
            continue
        raw_start = word.start if math.isfinite(word.start) and word.start > 0 else 0.0
        start = max(last_start, raw_start)
        raw_end = word.end if math.isfinite(word.end) else start
        end = max(start, raw_end)
        cleaned.append(Word(word.text, start, end, word.confidence))
        last_start = start
    return cleaned


def group_words(words: Sequence[Word]) -> list[list[Word]]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        too_far = current and word.start - current[-1].end > _MAX_GROUP_GAP_SEC
        too_long = current and sum(len(w.text) + 1 for w in current) + len(word.text) > _MAX_CHARS_PER_LINE * 2
        if current and (too_far or too_long or len(current) >= _MAX_WORDS_PER_GROUP):
            groups.append(current)
            current = []
        current.append(word)
        if word.text.endswith((".", "?", "!", ",")):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _line_breaks(tokens: list[str]) -> list[str]:
    """Lay tokens onto at most two lines by inserting a hard break."""
    plain = [re.sub(r"\{[^}]*\}", "", t) for t in tokens]
    if sum(len(p) + 1 for p in plain) - 1 <= _MAX_CHARS_PER_LINE:
        return [" ".join(tokens)]
    best, best_diff = 1, 10 ** 6
    for split in range(1, len(tokens)):
        left = sum(len(p) + 1 for p in plain[:split])
        right = sum(len(p) + 1 for p in plain[split:])
        if abs(left - right) < best_diff:
            best, best_diff = split, abs(left - right)
    return [" ".join(tokens[:best]), " ".join(tokens[best:])]


@lru_cache(maxsize=4)
def _measuring_face(font_path: str):
    return ImageFont.truetype(font_path, 1000)


def _fitted_size(lines: Sequence[str], base: int, pad: int, grow: float, font_path: Optional[str]) -> int:
    """Largest size up to ``base`` at which the widest line stays inside the frame.

    Wrapping is off, so an over-long line would otherwise run off both edges. libass
    sizes a face by ascender plus descender, so that is the unit measured here.
    """
    if not font_path:
        return base
    try:
        face = _measuring_face(font_path)
    except OSError as exc:
        log.warning("could not measure font %s: %s", font_path, exc)
        return base
    height = sum(face.getmetrics())
    widest = max(face.getlength(line) / height for line in lines)
    if widest <= 0:
        return base
    return min(base, int((_WIDTH - 2 * pad) / (widest * grow)))


def _caption_events(words: Sequence[Word], font_path: Optional[str] = None, y: int = Layout.caption_y) -> list[str]:
    events: list[str] = []
    for group in group_words(_normalised_words(words)):
        plain = [_safe(w.text).upper() for w in group]
        # One size per group, so the text does not jump as the highlight moves.
        size = _fitted_size(_line_breaks(plain), _CAPTION_SIZE, _CAPTION_PAD, _HIGHLIGHT_GROW, font_path)
        for index, word in enumerate(group):
            tokens = []
            for other_index, text in enumerate(plain):
                if other_index == index:
                    # \\r resets to the style, so the fitted size has to come back with it.
                    text = f"{{\\c{_YELLOW}\\t(0,70,\\fscx112\\fscy112)}}{text}{{\\r\\fs{size}}}"
                tokens.append(text)
            line = "\\N".join(_line_breaks(tokens))
            start = word.start
            if index + 1 < len(group):
                end = group[index + 1].start
            else:
                end = word.end + _MAX_HOLD_SEC
            events.append(f"Dialogue: 1,{_clock(start)},{_clock(max(end, start + 0.05))},Caption,,0,0,0,,"
                          f"{{\\an5\\pos({_WIDTH // 2},{y})\\fs{size}}}{line}")
    # Later events must not sit on top of the next word when a hold runs long.
    return _trim_overlaps(events)


def _trim_overlaps(events: list[str]) -> list[str]:
    trimmed = []
    for index, event in enumerate(events):
        if index + 1 < len(events):
            parts = event.split(",", 3)
            next_start = events[index + 1].split(",", 2)[1]
            if _seconds(parts[2]) > _seconds(next_start):
                parts[2] = next_start if _seconds(next_start) >= _seconds(parts[1]) else parts[1]
                event = ",".join(parts)
        trimmed.append(event)
    return trimmed


def _seconds(clock: str) -> float:
    hours, minutes, seconds = clock.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _hook_event(hook: str, duration: float, font_path: Optional[str] = None, y: int = Layout.hook_y) -> list[str]:
    if not math.isfinite(duration) or duration <= 0:
        return []
    text = _safe(hook)
    if not text or not is_renderable(text):
        return []
    end = min(_HOOK_SECONDS, duration)
    broken = _line_breaks(text.upper().split())
    size = _fitted_size(broken, _HOOK_SIZE, _HOOK_PAD, 1.0, font_path)
    lines = "\\N".join(broken)
    return [f"Dialogue: 2,{_clock(0)},{_clock(end)},Hook,,0,0,0,,"
            f"{{\\an8\\pos({_WIDTH // 2},{y})\\fs{size}\\fad(60,180)\\fscx70\\fscy70"
            f"\\t(0,160,\\fscx100\\fscy100)}}{lines}"]


def beat_is_drawable(text: str) -> bool:
    """Whether a graphic can really be shown. is_renderable alone refuses plain numbers."""
    text = _safe(text)
    if any(c.isalpha() for c in text):
        return is_renderable(text)
    return any("0" <= c <= "9" for c in text)


def _count_frames(text: str) -> list[str]:
    """The readings a number passes through as it counts up, ending on itself."""
    match = COUNTABLE.match(text)
    if not match:
        return [text]
    prefix, number, suffix = match.groups()
    digits = number.replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return [text]
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    grouped = "," in number
    frames = []
    for step in range(1, _COUNT_STEPS):
        current = value * step / _COUNT_STEPS
        shown = f"{current:,.{decimals}f}" if grouped else f"{current:.{decimals}f}"
        frames.append(f"{prefix}{shown}{suffix}")
    return frames + [text]


def _beat_lines(text: str) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= _BEAT_LINE_CHARS:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    return lines


def _beat_events(beats: Sequence[tuple], font_path: Optional[str], y: int = Layout.beat_y) -> list[str]:
    """Graphics for (output_time, text, style, label, seconds). Layer 0 sits under captions."""
    events: list[str] = []
    centre = _WIDTH // 2
    for when, text, style, label, seconds in beats:
        if not math.isfinite(when) or not math.isfinite(seconds) or seconds <= 0:
            continue
        when = max(0.0, when)
        text = _safe(text)
        if style not in _BEAT_SIZES or not beat_is_drawable(text):
            continue
        start, end = _clock(when), _clock(when + seconds)
        broken = _beat_lines(text)
        size = _fitted_size(broken, _BEAT_SIZES[style], _BEAT_PAD, 1.0, font_path)
        lines = "\\N".join(broken)
        if style == "card":
            # A full-screen card replaces the picture while the voice carries on.
            events.append(f"Dialogue: 0,{start},{end},Beat,,0,0,0,,{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{_YELLOW}}}{_DIM_SHAPE}{{\\p0}}")
            events.append(f"Dialogue: 3,{start},{end},Beat,,0,0,0,,"
                          f"{{\\an5\\move({centre},{_HEIGHT // 2 + 160},{centre},{_HEIGHT // 2},0,120)\\fs{size}"
                          f"\\bord0\\shad0\\1c&H111111&}}{lines}")
            continue
        # Dim the footage behind the text so the graphic reads at a glance.
        events.append(f"Dialogue: 0,{start},{end},Beat,,0,0,0,,{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c&H000000&\\1a&H78&\\fad(50,120)}}{_DIM_SHAPE}{{\\p0}}")
        frames = _count_frames(text)
        step = _COUNT_SECONDS / len(frames)
        for index, reading in enumerate(frames):
            a = when + index * step
            b = when + (index + 1) * step if index + 1 < len(frames) else when + seconds
            reading_size = min(size, _fitted_size([reading], _BEAT_SIZES[style], _BEAT_PAD, 1.0, font_path))
            events.append(f"Dialogue: 3,{_clock(a)},{_clock(b)},Beat,,0,0,0,,"
                          f"{{\\an5\\pos({centre},{y})\\fs{reading_size}\\1c{_YELLOW}}}{reading}")
        label_text = _safe(label)
        if label_text and beat_is_drawable(label_text):
            label_size = _fitted_size([label_text], 76, _BEAT_PAD, 1.0, font_path)
            events.append(f"Dialogue: 3,{start},{end},Beat,,0,0,0,,"
                          f"{{\\an5\\pos({centre},{y + 170})\\fs{label_size}\\fad(120,80)}}{label_text}")
    return events


def font_family(path: str) -> str:
    """Read a font file's family name, which is how ASS has to refer to it."""
    fallback = _safe_style_token(os.path.splitext(os.path.basename(path))[0], "Arial")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        base = struct.unpack(">I", data[12:16])[0] if data[:4] == b"ttcf" else 0
        for index in range(struct.unpack(">H", data[base + 4:base + 6])[0]):
            record = base + 12 + index * 16
            if data[record:record + 4] != b"name":
                continue
            table = struct.unpack(">I", data[record + 8:record + 12])[0]
            count, strings = struct.unpack(">HH", data[table + 2:table + 6])
            for row in range(count):
                platform, _enc, _lang, name_id, length, offset = struct.unpack(
                    ">6H", data[table + 6 + row * 12:table + 18 + row * 12])
                if name_id == 1 and platform in (0, 3):
                    raw = data[table + strings + offset:table + strings + offset + length]
                    return _safe_style_token(raw.decode("utf-16-be").strip(), fallback)
    except (OSError, struct.error, UnicodeDecodeError) as exc:
        log.debug("could not read font name from %s: %s", path, exc)
    return fallback


def _steady_text(style: str, text: str, duration: float, y: int) -> list[str]:
    """Small text on screen for the whole clip. A name in a script the font lacks is left out."""
    if not math.isfinite(duration) or duration <= 0:
        return []
    style = _safe_style_token(style, "Default")
    text = _safe(text)
    if not text or not is_renderable(text):
        return []
    return [f"Dialogue: 0,{_clock(0)},{_clock(duration)},{style},,0,0,0,,{{\\an5\\pos({_WIDTH // 2},{y})}}{text}"]


def build_ass(words: Sequence[Word], hook: str, duration: float, font: str,
              font_path: Optional[str] = None, beats: Sequence[tuple] = (), creator: str = "", brand: str = "",
              layout: Layout = Layout()) -> str:
    """Full ASS document. ``words`` and ``beats`` must already be in output time."""
    font = _safe_style_token(font, "Arial")
    creator = _safe(creator)
    events = (_steady_text("Credit", f"Credit: {creator}" if creator else "", duration, layout.credit_y)
              + _steady_text("Brand", _safe(brand), duration, layout.brand_y)
              + _beat_events(beats, font_path, layout.beat_y)
              + _hook_event(hook, duration, font_path, layout.hook_y)
              + _caption_events(words, font_path, layout.caption_y))
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {_WIDTH}",
        f"PlayResY: {_HEIGHT}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{font},92,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,3,5,40,40,0,1",
        f"Style: Hook,{font},72,&H00111111,&H000000FF,&H0000E5FF,&H0000E5FF,-1,0,0,0,100,100,0,0,3,20,0,8,60,60,0,1",
        f"Style: Brand,{font},48,&H00FFFFFF,&H000000FF,&H66000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,5,40,40,0,1",
        f"Style: Credit,{font},40,&H30FFFFFF,&H000000FF,&H99000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,5,40,40,0,1",
        f"Style: Beat,{font},190,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,12,4,5,40,40,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events,
        "",
    ])
