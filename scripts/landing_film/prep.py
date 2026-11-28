"""Footage for the landing-D opening film: the textures film.py maps onto the glass tiles.

    python scripts/landing_film/prep.py --src source.mp4 [--srt source.srt] --out inputs/ [--tile 512x288]

Assets for this project's own website only (never customer work). Runs on GitHub Actions
(.github/workflows/landing-film.yml), not the owner's laptop.

1. Rebuilds the black_holes working copy from scripts/showcase/edit_decision_list.json (its kept segments, then its
   crop), scaled down: the film needs tile textures, not the full picture.
2. Lays the strip out (cells.json): each sentence of film.json is split into cells of about `cell_seconds`, so every
   sentence boundary falls in a gap between two tiles and the four cuts can part the strip cleanly.
3. Writes one atlas per film frame (atlas/a0001.jpg ...): every tile's picture at that frame. Each tile plays its
   own seconds of the source, slowed to last the whole film, so the footage moves whenever the film does and a
   tile's picture is a pure function of the frame (any frame range renders identically alone).
4. Writes each kept clip's vertical face (faces/c0/v0001.jpg ...): the 9:16 framed layout the clipper renders
   (the source at full width over a zoomed, dimmed copy of itself), without captions, for the upright decks.
5. Logs where every cut sits against the audio's silences and NASA's .srt, for a person to check.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageFilter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from layout import COLS, cells_from  # noqa: E402

ROOT = HERE.parents[1]
EDL = ROOT / "scripts" / "showcase" / "edit_decision_list.json"
CONFIG = HERE / "film.json"


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-3000:])
        raise SystemExit(f"failed: {' '.join(cmd[:6])} ...")
    return proc.stderr


def build_working_copy(src: Path, doc: dict, out: Path, size: str) -> None:
    w, h = size.split("x")
    parts, labels = [], []
    for i, seg in enumerate(doc["segments"]):
        parts.append(f"[0:v]trim=start={seg['start']}:end={seg['end']},setpts=PTS-STARTPTS[v{i}]")
        labels.append(f"[v{i}]")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=1:a=0[v]"
    c = doc.get("crop")
    crop = f"crop={c['w']}:{c['h']}:{c['x']}:{c['y']}," if c else ""
    graph += f";[v]{crop}scale={w}:{h}:flags=lanczos,fps=30,format=yuv420p[vc]"
    run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-filter_complex", graph, "-map", "[vc]", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", str(out)])


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def decode(work: Path, folder: Path, size: str, start: float = 0.0, length: float | None = None) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(work)]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    w, h = size.split("x")
    cmd += ["-vf", f"fps=30,scale={w}:{h}:flags=lanczos", "-q:v", "2", str(folder / "w%05d.jpg")]
    run(cmd)
    return len(list(folder.glob("w*.jpg")))


def framed(frame: Image.Image, w: int, h: int) -> Image.Image:
    """The clipper's framed layout (CLIP_LAYOUT=framed): the wide source at full width, centred, over a zoomed,
    blurred and dimmed copy of itself. Captions are left off: they belong to scene 4."""
    sw, sh = frame.size
    scale = h / sh
    bg = frame.resize((max(w, round(sw * scale)), h), Image.LANCZOS)
    left = (bg.width - w) // 2
    bg = bg.crop((left, 0, left + w, h)).filter(ImageFilter.GaussianBlur(w / 30))
    bg = Image.eval(bg, lambda v: int(v * 0.6))
    fg_h = round(sh * w / sw)
    fg = frame.resize((w, fg_h), Image.LANCZOS)
    bg.paste(fg, (0, (h - fg_h) // 2))
    return bg


def silences(src: Path, stream: int) -> list[tuple[float, float]]:
    err = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-map", f"0:a:{stream}",
               "-af", "silencedetect=noise=-45dB:d=0.12", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", err)]
    return list(zip(starts, ends))


def srt_cues(path: Path) -> list[tuple[float, float, str]]:
    def secs(t: str) -> float:
        h, m, s = t.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig")):
        m = re.search(r"(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)\s*\n(.*)", block, re.S)
        if m:
            cues.append((secs(m.group(1)), secs(m.group(2)), " ".join(m.group(3).split())))
    return cues


def to_orig(doc: dict, t: float) -> float:
    work = 0.0
    for seg in doc["segments"]:
        length = seg["end"] - seg["start"]
        if t < work + length or seg is doc["segments"][-1]:
            return seg["start"] + t - work
        work += length
    raise ValueError(t)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--src", required=True, help="the NASA original (sha256 checked by the workflow)")
    ap.add_argument("--srt", default="", help="NASA's captions, for the cut report only")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--edl", default=str(EDL))
    ap.add_argument("--tile", default="512x288")
    ap.add_argument("--face", default="540x960")
    ap.add_argument("--frames", default="", help="only these film frames' atlases (a local check), e.g. 1,128,256")
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    doc = json.loads(Path(args.edl).read_text(encoding="utf-8"))["sources"][cfg["source_id"]]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    src = Path(args.src)
    F = cfg["film"]["frames"]
    face_from = cfg["film"]["face_from"]

    # 1. working copy
    work = out / "work.mp4"
    build_working_copy(src, doc, work, "1024x576")
    got = duration(work)
    print(f"working copy: {got:.2f} s (edit list expects {doc['expected_duration']})", flush=True)
    if abs(got - doc["expected_duration"]) > 0.25:
        raise SystemExit("working copy duration differs from the edit list")
    if abs(got - cfg["duration"]) > 0.25:
        raise SystemExit("film.json duration differs from the working copy")

    # 2. the strip
    cells = cells_from(cfg)
    rows = math.ceil(len(cells) / COLS)
    tw, th = (int(x) for x in args.tile.split("x"))
    layout = {"cols": COLS, "rows": rows, "tile": [tw, th], "frames": F, "cells": cells,
              "clips": cfg["clips"], "face_from": face_from}
    (out / "cells.json").write_text(json.dumps(layout, indent=1), encoding="utf-8")
    print(f"strip: {len(cells)} tiles in {rows} atlas rows; "
          + ", ".join(f"clip {c}: tiles {[x['i'] for x in cells if x['clip'] == c]}" for c in range(len(cfg['clips']))),
          flush=True)

    # 3. atlases
    frames_dir = out / "_frames"
    n = decode(work, frames_dir, args.tile)
    cache: dict[int, Image.Image] = {}

    def frame_at(t: float) -> Image.Image:
        k = min(n, max(1, round(t * 30) + 1))
        if k not in cache:
            if len(cache) > 400:
                cache.clear()
            cache[k] = Image.open(frames_dir / f"w{k:05d}.jpg").convert("RGB")
        return cache[k]

    atlas_dir = out / "atlas"
    atlas_dir.mkdir(exist_ok=True)
    wanted = [int(x) for x in args.frames.split(",") if x] or list(range(1, F + 1))
    for f in wanted:
        u = (f - 1) / (F - 1)
        atlas = Image.new("RGB", (COLS * tw, rows * th))
        for c in cells:
            t = c["a"] + (c["b"] - c["a"]) * (0.02 + 0.96 * u)
            atlas.paste(frame_at(t), ((c["i"] % COLS) * tw, (c["i"] // COLS) * th))
        atlas.save(atlas_dir / f"a{f:04d}.jpg", quality=90)
    print(f"atlases: {len(wanted)} at {COLS * tw}x{rows * th}", flush=True)

    # 4. faces
    fw, fh = (int(x) for x in args.face.split("x"))
    for ci, clip in enumerate(cfg["clips"]):
        w0, w1 = clip["face"]
        folder = out / f"_face{ci}"
        m = decode(work, folder, "1024x576", start=w0, length=w1 - w0 + 0.1)
        face_dir = out / "faces" / f"c{ci}"
        face_dir.mkdir(parents=True, exist_ok=True)
        count = F - face_from + 1
        for j in range(count):
            k = min(m, 1 + round(j / max(1, count - 1) * (w1 - w0) * 30))
            framed(Image.open(folder / f"w{k:05d}.jpg").convert("RGB"), fw, fh).save(
                face_dir / f"v{j + 1:04d}.jpg", quality=90)
        print(f"face {ci} ({clip['id']}): {count} frames from {w0}-{w1} s", flush=True)

    # 5. the cut report
    stream = int(doc.get("audio_stream", 0))
    quiet = silences(src, stream)
    cues = srt_cues(Path(args.srt)) if args.srt and Path(args.srt).is_file() else []
    for at, is_cut in cfg["bounds"]:
        if not is_cut:
            continue
        o = to_orig(doc, at)
        inside = [s for s in quiet if s[0] - 0.1 <= o <= s[1]]
        near = min(quiet, key=lambda s: min(abs(o - s[0]), abs(o - s[1])))
        cue = next((c for c in cues if c[0] <= o <= c[1] + 0.3), None)
        print(f"cut {at:7.2f} s (original {o:7.2f}): "
              + (f"in silence {inside[0][0]:.2f}-{inside[0][1]:.2f}" if inside else
                 f"nearest silence {near[0]:.2f}-{near[1]:.2f}")
              + (f"; srt cue {cue[0]:.2f}-{cue[1]:.2f} {cue[2][:60]!r}" if cue else ""), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
