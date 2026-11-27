"""Small ffmpeg/ffprobe helpers shared by the showcase scripts (no app imports)."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def font_arg() -> str:
    """``fontfile=...`` for drawtext, escaped for the filter graph (a Windows drive colon breaks it otherwise)."""
    for candidate in _FONT_CANDIDATES:
        if os.path.isfile(candidate):
            return "fontfile='" + candidate.replace("\\", "/").replace(":", "\\:") + "'"
    return ""  # fontconfig default


def label(text_expr: str, size: int = 14) -> str:
    """A drawtext filter in the top-left corner; ``text_expr`` must not contain quotes or colons."""
    font = font_arg()
    return (f"drawtext=text='{text_expr}':x=4:y=4:fontcolor=yellow:fontsize={size}:box=1:boxcolor=black@0.55"
            + (f":{font}" if font else ""))


def stamp(size: int = 13) -> str:
    """A drawtext filter printing each frame's timestamp (h:m:s.ms) in the top-left corner."""
    font = font_arg()
    return ("drawtext=text='%{pts\\:hms}':x=3:y=3:fontcolor=yellow:fontsize=" + str(size)
            + ":box=1:boxcolor=black@0.55" + (f":{font}" if font else ""))


def ffmpeg(args: list[str], check: bool = True) -> str:
    """Run ffmpeg quietly; returns stderr (where filters such as silencedetect report)."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}):\n{tail}")
    return proc.stderr


def probe(path: Path) -> dict:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                           "stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate:format=duration,size",
                           "-of", "json", str(path)], capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True)
    return json.loads(proc.stdout)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def silences(path: Path, noise_db: int = -50, min_sec: float = 0.25, audio_stream: int = 0) -> list[tuple[float, float]]:
    """Silent stretches of one audio stream as (start, end) seconds."""
    err = ffmpeg(["-i", str(path), "-map", f"0:a:{audio_stream}", "-af",
                  f"silencedetect=noise={noise_db}dB:d={min_sec}", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    total = duration(path)
    pairs = []
    for i, s in enumerate(starts):
        pairs.append((max(0.0, s), ends[i] if i < len(ends) else total))
    return pairs


def frame_luma(path: Path, start: float, seconds: float) -> list[float]:
    """Mean luma (0-255) of every frame in [start, start + seconds)."""
    err = ffmpeg(["-ss", f"{max(0.0, start):.3f}", "-t", f"{seconds:.3f}", "-i", str(path), "-an", "-vf",
                  "scale=160:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-f", "null", "-"])
    return [float(x) for x in re.findall(r"YAVG=([\d.]+)", err)]


_SRT_TIMING = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    """SRT cues as (start, end, text); tolerant of a BOM, CRLF and stray numbering."""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.lstrip("﻿").strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            m = _SRT_TIMING.search(line)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                cues.append((start, end, " ".join(lines[i + 1:])))
                break
    return cues


def write_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def github_output(**values: object) -> None:
    """Append ``key=value`` lines to $GITHUB_OUTPUT when running on Actions; always echo them."""
    target: Optional[str] = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={'' if v is None else str(v).lower() if isinstance(v, bool) else v}" for k, v in values.items()]
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    for line in lines:
        print(line)
