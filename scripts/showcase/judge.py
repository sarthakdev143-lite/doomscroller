"""Evidence for judging showcase clips: black and flash frames, contact sheets and caption-sync frame grabs.

    python scripts/showcase/judge.py --run-dir run/

For every ``clips/clip_*_manifest.json`` it writes into ``run/judge/``: ``<clip>_sheet.jpg`` (a frame every 2 s),
``<clip>_caps.jpg`` (six frames taken at caption word midpoints, each labelled with the word that should be on
screen) and one ``judge.json`` with the title, hook, what the clip says, its source segments (working-copy
seconds), cutaways, beats, black stretches and big brightness jumps. The verdict (keep or reject) is a person's:
this only gathers the evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ffmpeg, font_arg, frame_luma, stamp, write_json  # noqa: E402

FLASH_JUMP = 45.0  # mean-luma change between consecutive frames that reads as a flash


def _word_text(w: object) -> str:
    if isinstance(w, dict):
        return str(w.get("text") or w.get("w") or w.get("word") or "")
    return str(w)


def judge_clip(manifest_path: Path, out: Path) -> dict:
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    clips = manifest_path.parent
    clip = clips / m["clip"]
    stem = clip.stem
    lo = float(m.get("excerpt_offset_s") or 0.0)
    edit = m.get("edit") or {}
    words = edit.get("words") or m.get("words") or []
    entry: dict = {
        "clip": m["clip"], "duration_s": m.get("duration_s"), "title": m.get("title"),
        "hook": (m.get("moment") or {}).get("hook"), "kind": (m.get("moment") or {}).get("kind"),
        "score": m.get("score"), "hashtags": m.get("hashtags"),
        "says": " ".join(_word_text(w) for w in words).strip(),
        "source_segments_work_s": [[round(a + lo, 2), round(b + lo, 2)] for a, b in edit.get("segments") or []],
        "cutaways": [{"at": c.get("start_s"), "seconds": c.get("duration_s"), "query": c.get("query"),
                      "stock_id": c.get("stock_id")} for c in m.get("cutaways") or []],
        "beats": [{"at": b.get("start_s"), "style": b.get("style"), "text": b.get("text")} for b in m.get("beats") or []],
        "captions": len(m.get("captions") or []),
    }
    err = ffmpeg(["-i", str(clip), "-vf", "blackdetect=d=0.12:pix_th=0.06", "-an", "-f", "null", "-"])
    entry["black"] = [[float(a), float(b)] for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    lumas = frame_luma(clip, 0.0, float(m.get("duration_s") or 0) + 1.0)
    fps = 30.0
    entry["luma_range"] = [round(min(lumas)), round(max(lumas))] if lumas else None
    entry["flash_jumps"] = [{"at": round(i / fps, 2), "from": round(lumas[i - 1]), "to": round(lumas[i])}
                            for i in range(1, len(lumas)) if abs(lumas[i] - lumas[i - 1]) > FLASH_JUMP][:20]
    rows = max(1, int((float(m.get("duration_s") or 2) / 2 + 7) // 8))
    ffmpeg(["-y", "-i", str(clip), "-vf", f"fps=0.5,scale=216:-2,{stamp(12)},tile=8x{rows}", "-frames:v", "1",
            str(out / f"{stem}_sheet.jpg")])
    caps = m.get("captions") or []
    picks = [caps[int(k * (len(caps) - 1) / 5)] for k in range(6)] if len(caps) >= 6 else list(caps)
    font = font_arg()
    grabbed = []
    for k, c in enumerate(picks):
        t = (float(c["start_s"]) + float(c["end_s"])) / 2
        word = re.sub(r"[^A-Za-z0-9 ]", "", str(c.get("text", ""))) or "-"
        target = out / f"{stem}_cap{k}.png"
        text = f"expect {word} at {t:.2f}s"  # no quotes or colons, so it is safe inline
        ffmpeg(["-y", "-ss", f"{t:.3f}", "-i", str(clip), "-frames:v", "1", "-vf",
                f"scale=360:-2,drawtext=text='{text}':x=4:y=4:fontcolor=yellow:fontsize=16:box=1:boxcolor=black@0.6"
                + (f":{font}" if font else ""), str(target)])
        grabbed.append({"word": c.get("text"), "at": round(t, 3)})
    if picks:
        inputs: list[str] = []
        for k in range(len(picks)):
            inputs += ["-i", str(out / f"{stem}_cap{k}.png")]
        ffmpeg(["-y", *inputs, "-filter_complex", f"hstack=inputs={len(picks)}", str(out / f"{stem}_caps.jpg")]
               if len(picks) > 1 else ["-y", *inputs, str(out / f"{stem}_caps.jpg")])
        for k in range(len(picks)):
            (out / f"{stem}_cap{k}.png").unlink(missing_ok=True)
    entry["caption_grabs"] = grabbed
    return entry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    out = run / "judge"
    out.mkdir(parents=True, exist_ok=True)
    manifests = sorted((run / "clips").glob("clip_*_manifest.json"))
    if not manifests:
        print("::warning::no clip manifests to judge")
        return 0
    report = []
    for manifest in manifests:
        entry = judge_clip(manifest, out)
        report.append(entry)
        print(f"[judge] {entry['clip']} {entry['duration_s']}s | {entry['title']} | hook: {entry['hook']}")
        print(f"[judge]   says: {entry['says'][:300]}")
        print(f"[judge]   black: {entry['black']} | flashes: {entry['flash_jumps'][:5]} | cutaways: "
              f"{[c['query'] for c in entry['cutaways']]}")
    write_json(out / "judge.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
