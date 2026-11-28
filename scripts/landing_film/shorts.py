"""Web-encode the approved Shorts for landing-D scenes 4-5 (small H.264 MP4s with faststart, and posters).

    python scripts/landing_film/shorts.py --src downloaded/ --out web/

Assets for this project's own website only. --src holds the showcase artifacts as `gh run download` leaves them
(<artifact>/<path>); every source must match the sha256 of the copy the owner approved (shorts.json). The quality is the
best that keeps all three within the budget. Writes the MP4s, JPEG posters and shorts.json (a manifest the page reads,
with each file's content hash: /static/ is cached as immutable).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return round(float(out.strip()), 2)


def encode(src: Path, out: Path, w: int, h: int, crf: int) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                    "-vf", f"scale={w}:{h}:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-profile:v", "high",
                    "-preset", "slow", "-crf", str(crf), "-g", "60", "-c:a", "aac", "-b:a", "80k", "-ac", "2",
                    "-movflags", "+faststart", str(out)], check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=str(HERE / "shorts.json"))
    args = ap.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    w, h = (int(x) for x in cfg["size"].split("x"))

    sources = []
    for s in cfg["shorts"]:
        f = src / s["artifact"] / s["path"]
        got = digest(f.read_bytes())
        if got != s["sha256"]:
            raise SystemExit(f"{s['id']}: sha256 {got[:16]} is not the approved file's {s['sha256'][:16]}")
        sources.append((s, f))
    print("sources match the approved files", flush=True)

    crf = 23
    while True:
        for s, f in sources:
            encode(f, out / f"short-{s['id']}.mp4", w, h, crf)
        total = sum((out / f"short-{s['id']}.mp4").stat().st_size for s, _ in sources)
        print(f"crf {crf}: {total / 1e6:.2f} MB", flush=True)
        if total <= cfg["budget_bytes"] or crf >= 33:
            break
        crf += 2

    manifest = {"size": [w, h], "crf": crf, "shorts": []}
    for s, f in sources:
        video = out / f"short-{s['id']}.mp4"
        poster = out / f"short-{s['id']}.jpg"
        Image.open(src / s["artifact"] / s["poster"]).convert("RGB").resize((w, h), Image.LANCZOS).save(
            poster, quality=82, optimize=True, progressive=True)
        manifest["shorts"].append({
            "id": s["id"], "file": video.name, "bytes": video.stat().st_size, "v": digest(video.read_bytes())[:10],
            "poster": poster.name, "poster_v": digest(poster.read_bytes())[:10], "duration": duration(video),
            "title": s["title"], "hook": s["hook"], "credit": s["credit"], "handoff_s": s.get("handoff_s", 0.0)})
    (out / "shorts.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=1, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
