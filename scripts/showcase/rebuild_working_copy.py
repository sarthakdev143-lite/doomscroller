"""Build and verify a showcase working copy from the NASA original, as the edit decision list describes.

    python scripts/showcase/rebuild_working_copy.py --source-id black_holes --src source.mp4 \
        --out-dir work/ --checks checks/ [--plan-only]

Steps: plan the kept segments (snapping each cut to the nearest pause when ``snap_to_silence`` is set), cut and
crop them into one near-lossless file named after the source's title (the clipper titles a local file from its
name), then verify it: the duration, dark joins where the edit list expects them, and frame grabs for a person
to look at (first frames, every join, the last frame, both top corners and the bottom credit band).
Exits non-zero when a check fails. Heavy work: run it on GitHub Actions, not the owner's laptop.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (duration, ffmpeg, frame_luma, github_output, label, probe, silences, stamp,  # noqa: E402
                     write_json)

EDL = Path(__file__).resolve().parent / "edit_decision_list.json"
SNAP_TOLERANCE_SEC = 1.5
DARK_LUMA_MAX = 40.0  # 8-bit mean luma; limited-range black is 16


def load_source(edl: str, source_id: str) -> dict:
    sources = json.loads(Path(edl).read_text(encoding="utf-8"))["sources"]
    if source_id not in sources:
        raise SystemExit(f"unknown source id {source_id!r}")
    return sources[source_id]


def plan_segments(src_doc: dict, src_path: Path, snap_to_silence: bool | None = None) -> dict:
    """The kept segments in original time (snapped if asked), the joins in working time, and the time map.

    Raises ValueError when a cut point has no pause near it or a segment is empty."""
    segments = [(float(s["start"]), float(s["end"])) for s in src_doc["segments"]]
    snapped = []
    if src_doc.get("snap_to_silence") if snap_to_silence is None else snap_to_silence:
        quiet = silences(src_path, noise_db=-50, min_sec=0.2, audio_stream=int(src_doc.get("audio_stream", 0)))

        def snap(t: float) -> float:
            best = None
            for a, b in quiet:
                gap = 0.0 if a <= t <= b else min(abs(t - a), abs(t - b))
                if gap <= SNAP_TOLERANCE_SEC and (best is None or gap < best[0]):
                    best = (gap, (a + b) / 2)
            if best is None:
                raise ValueError(f"no pause within {SNAP_TOLERANCE_SEC}s of cut point {t:.2f}s; fix the edit list")
            return round(best[1], 3)

        for start, end in segments:
            snapped.append((snap(start), snap(end)))
        segments = snapped
    for start, end in segments:
        if end <= start:
            raise ValueError(f"empty segment {start}-{end}")
    work, joins, table = 0.0, [], []
    for i, (start, end) in enumerate(segments):
        table.append({"orig_start": start, "orig_end": end, "work_start": round(work, 3),
                      "work_end": round(work + end - start, 3), "offset": round(start - work, 3)})
        work += end - start
        if i < len(segments) - 1:
            joins.append(round(work, 3))
    return {"segments": table, "joins": joins, "planned_duration": round(work, 3),
            "work_to_orig": "orig = work + offset of the segment whose [work_start, work_end) holds work"}


def build(src_doc: dict, src_path: Path, plan: dict, out: Path) -> None:
    info = probe(src_path)
    video = next(s for s in info["streams"] if s.get("codec_type") == "video")
    width, height = int(video["width"]), int(video["height"])
    audio = int(src_doc.get("audio_stream", 0))
    parts, labels = [], []
    for i, seg in enumerate(plan["segments"]):
        a, b = seg["orig_start"], seg["orig_end"]
        parts.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a:{audio}]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=1:a=1[v][a]"
    crop = src_doc.get("crop")
    if crop:
        w, h, x, y = int(crop["w"]), int(crop["h"]), int(crop["x"]), int(crop["y"])
        if w % 2 or h % 2 or x < 0 or y < 0 or x + w > width or y + h > height:
            raise SystemExit(f"crop {w}x{h}+{x}+{y} does not fit a {width}x{height} frame (or is odd)")
        graph += f";[v]crop={w}:{h}:{x}:{y},format=yuv420p[vc]"
    else:
        graph += ";[v]format=yuv420p[vc]"
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(["-y", "-i", str(src_path), "-filter_complex", graph, "-map", "[vc]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
            "-ac", "2", "-movflags", "+faststart", str(out)])


def grabs(work: Path, plan: dict, checks: Path) -> None:
    checks.mkdir(parents=True, exist_ok=True)
    ts = stamp()
    ffmpeg(["-y", "-i", str(work), "-vf", "select='lt(n,4)',scale=480:-2,tile=4x1", "-frames:v", "1",
            str(checks / "first_frames.jpg")])
    ffmpeg(["-y", "-sseof", "-0.2", "-i", str(work), "-frames:v", "1", "-vf", "scale=640:-2",
            str(checks / "last_frame.jpg")])
    for k, j in enumerate(plan["joins"]):
        ffmpeg(["-y", "-ss", f"{max(0.0, j - 0.5):.3f}", "-t", "1.0", "-i", str(work), "-vf",
                f"fps=10,scale=320:-2,{label('join ' + str(j) + 's')},tile=10x1", "-frames:v", "1",
                str(checks / f"join_{k + 1}.jpg")])
    corners = {"corner_top_right": "crop=iw*0.32:ih*0.2:iw*0.68:0", "corner_top_left": "crop=iw*0.32:ih*0.2:0:0"}
    for name, crop in corners.items():
        ffmpeg(["-y", "-i", str(work), "-vf", f"fps=0.25,{crop},scale=400:-2,{ts},tile=4x8",
                "-fps_mode", "vfr", str(checks / f"{name}_%02d.jpg")])
    ffmpeg(["-y", "-i", str(work), "-vf", f"fps=0.5,crop=iw:ih*0.14:0:ih*0.86,scale=1280:-2,{ts},tile=1x20",
            "-fps_mode", "vfr", str(checks / "credit_band_%02d.jpg")])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--edl", default=str(EDL))
    ap.add_argument("--source-id", required=True)
    ap.add_argument("--src", required=True, help="the downloaded NASA original")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--checks", required=True, help="where the verification grabs and report go")
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args(argv)

    src_doc = load_source(args.edl, args.source_id)
    src_path = Path(args.src)
    checks = Path(args.checks)
    try:
        plan = plan_segments(src_doc, src_path)
    except ValueError as exc:
        print(f"::error title=Edit list does not fit the source::{exc}")
        return 1
    write_json(checks / "plan.json", plan)
    print(f"[rebuild] segments: {[(s['orig_start'], s['orig_end']) for s in plan['segments']]}; "
          f"joins at {plan['joins']}; planned {plan['planned_duration']}s")
    if args.plan_only:
        return 0

    work = Path(args.out_dir) / f"{src_doc['title']}.mp4"
    build(src_doc, src_path, plan, work)
    report: dict = {"working_copy": work.name, "plan": plan, "problems": []}
    actual = duration(work)
    expected = src_doc.get("expected_duration") or plan["planned_duration"]
    report["duration"] = {"actual": round(actual, 3), "expected": expected}
    if abs(actual - float(expected)) > 0.15:
        report["problems"].append(f"duration {actual:.3f}s, expected {expected}s")
    report["joins"] = []
    for j in plan["joins"]:
        lumas = frame_luma(work, j - 0.25, 0.5)
        peak = round(max(lumas), 1) if lumas else None
        report["joins"].append({"at": j, "max_luma": peak, "frames": len(lumas)})
        if src_doc.get("dark_joins") and (peak is None or peak > DARK_LUMA_MAX):
            report["problems"].append(f"join at {j}s is not dark (max luma {peak})")
    grabs(work, plan, checks)
    write_json(checks / "working_copy_checks.json", report)
    github_output(working_copy=str(work))
    for problem in report["problems"]:
        print(f"::error title=Working copy check failed::{problem}")
    print(f"[rebuild] duration {actual:.3f}s (expected {expected}); joins {report['joins']}")
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
