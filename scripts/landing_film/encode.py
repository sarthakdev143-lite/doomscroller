"""Pack the rendered opening film for the web, and make review files for a person.

    python scripts/landing_film/encode.py --frames rendered/ --web web/ --review review/ [--mode full|stills]

Assets for this project's own website only. rendered/<camera>/f0001.png (+ data/d0001.png, the light pass) come
from film.py. For each camera, writes to web/ (copied into saas/web/static/media/d/ once accepted):

  <camera>-k.bin, <camera>-1.bin ...  the frames as WebP, concatenated into a few packs. Pack k (critical: the
                                      loader counts its bytes) holds the intro and every fourth frame after it, so
                                      the film can be scrubbed the moment the loader ends; the rest follow in order.
  <camera>-n.bin                      the light pass (camera-space normal + gloss) at a third of the size, every
                                      second frame, for the pointer light.
  <camera>-s1.jpg .. -s3.jpg           one still per beat, for reduced motion and browsers without WebGL.
  film.json                            the manifest: beats, sizes, each frame's pack/offset/length, and a content
                                      hash per file (the page fetches file?v=hash: /static/ is cached immutable).

and to review/: an MP4 of each camera's film, a contact sheet, and the stills as rendered.
Mode "stills" only makes the review sheet of whatever frames were rendered.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BUDGET = {"desk": 6.0e6, "phone": 5.0e6}   # bytes of frames per camera (critical + rest)
DATA_BUDGET = 0.6e6


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


def webp(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=quality, method=6)
    return buf.getvalue()


def sheet(paths: list[Path], out: Path, width: int = 2400, cols: int = 5) -> None:
    ims = [Image.open(p).convert("RGB") for p in paths]
    if not ims:
        return
    tw = width // cols
    th = round(tw * ims[0].height / ims[0].width)
    rows = (len(ims) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * tw, rows * th), (20, 20, 20))
    for k, (p, im) in enumerate(zip(paths, ims)):
        im = im.resize((tw, th), Image.LANCZOS)
        ImageDraw.Draw(im).text((8, 6), p.stem, fill=(255, 80, 60))
        canvas.paste(im, ((k % cols) * tw, (k // cols) * th))
    canvas.save(out, quality=88)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--frames", required=True)
    ap.add_argument("--web", required=True)
    ap.add_argument("--review", required=True)
    ap.add_argument("--mode", default="full", choices=["full", "stills"])
    ap.add_argument("--config", default=str(HERE / "film.json"))
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    film = cfg["film"]
    F, beats = film["frames"], film["beats"]
    src, web, review = Path(args.frames), Path(args.web), Path(args.review)
    web.mkdir(parents=True, exist_ok=True)
    review.mkdir(parents=True, exist_ok=True)
    cams = [c for c in ("desk", "phone") if (src / c).is_dir()]
    if not cams:
        raise SystemExit(f"no rendered frames under {src}")

    if args.mode == "stills":
        for cam in cams:
            pngs = sorted((src / cam).glob("f*.png"))
            sheet(pngs, review / f"{cam}-stills.jpg", cols=5 if cam == "desk" else 10)
            for p in pngs:
                Image.open(p).convert("RGB").save(review / f"{cam}-{p.stem}.jpg", quality=92)
            print(f"{cam}: {len(pngs)} stills", flush=True)
        return 0

    manifest = {"fps": film["fps"], "frames": F, "beats": beats, "face_from": film["face_from"], "cams": {}}
    # pack order: the critical pack first (intro, then every 4th frame), the rest in film order
    intro_end = beats["intro"][1]
    critical = list(range(1, intro_end + 1)) + [f for f in range(intro_end + 1, F + 1) if f % 4 == 0]
    rest = [f for f in range(1, F + 1) if f not in set(critical)]
    fill = [rest[i::3] for i in range(3)]                       # interleaved: each pack refines the whole film
    for cam in cams:
        folder = src / cam
        missing = [f for f in range(1, F + 1) if not (folder / f"f{f:04d}.png").is_file()]
        if missing:
            raise SystemExit(f"{cam}: {len(missing)} frames missing, first {missing[:5]}")
        imgs = {f: Image.open(folder / f"f{f:04d}.png").convert("RGB") for f in range(1, F + 1)}
        w, h = imgs[1].size
        quality, enc = 74, {}
        for _ in range(5):
            enc = {f: webp(im, quality) for f, im in imgs.items()}
            total = sum(len(b) for b in enc.values())
            print(f"{cam}: q{quality} -> {total / 1e6:.2f} MB ({total / F / 1e3:.1f} kB/frame)", flush=True)
            if total <= BUDGET[cam] or quality <= 50:
                break
            quality -= 6
        entry = {"w": w, "h": h, "quality": quality, "packs": [], "frames": [None] * (F + 1)}
        for name, order in [("k", critical)] + [(str(i + 1), fill[i]) for i in range(3)]:
            blob, offset = bytearray(), 0
            for f in order:
                entry["frames"][f] = [len(entry["packs"]), len(blob), len(enc[f])]
                blob += enc[f]
            data = bytes(blob)
            fname = f"{cam}-{name}.bin"
            (web / fname).write_bytes(data)
            entry["packs"].append({"file": fname, "bytes": len(data), "v": digest(data), "count": len(order)})
        entry["frames"] = entry["frames"][1:]

        # the light pass: a third of the size, every second frame
        dfolder = folder / "data"
        enc_txt = (dfolder / "encoding.txt").read_text().strip() if (dfolder / "encoding.txt").is_file() else "linear"
        dw, dh = round(w / 3 / 2) * 2, round(h / 3 / 2) * 2
        blob, frames_d = bytearray(), []
        dq = 82
        for f in range(1, F + 1, 2):
            p = dfolder / f"d{f:04d}.png"
            if not p.is_file():
                raise SystemExit(f"{cam}: light pass frame {f} missing")
            b = webp(Image.open(p).convert("RGB").resize((dw, dh), Image.BILINEAR), dq)
            frames_d.append([len(blob), len(b)])
            blob += b
        data = bytes(blob)
        if len(data) > DATA_BUDGET:
            print(f"warning: {cam} light pass {len(data) / 1e6:.2f} MB is over budget", flush=True)
        (web / f"{cam}-n.bin").write_bytes(data)
        entry["light"] = {"file": f"{cam}-n.bin", "bytes": len(data), "v": digest(data), "every": 2, "w": dw, "h": dh,
                          "encoding": enc_txt, "frames": frames_d}

        stills = []
        for k, beat in enumerate(("s1", "s2", "s3"), start=1):
            a, b = beats[beat]
            f = (a + b) // 2
            out = web / f"{cam}-s{k}.jpg"
            imgs[f].save(out, quality=84, optimize=True, progressive=True)
            stills.append({"file": out.name, "frame": f, "bytes": out.stat().st_size, "v": digest(out.read_bytes())})
        entry["stills"] = stills
        manifest["cams"][cam] = entry

        crit = entry["packs"][0]["bytes"]
        allb = sum(p["bytes"] for p in entry["packs"])
        print(f"{cam}: critical {crit / 1e6:.2f} MB, all frames {allb / 1e6:.2f} MB, light {len(data) / 1e6:.2f} MB, "
              f"stills {sum(s['bytes'] for s in stills) / 1e6:.2f} MB", flush=True)

        # review
        fps = film["fps"]
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
                        "-i", str(folder / "f%04d.png"), "-c:v", "libx264", "-preset", "slow", "-crf", "17",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(review / f"{cam}-film.mp4")], check=True)
        sheet([folder / f"f{f:04d}.png" for f in range(1, F + 1, 8)], review / f"{cam}-sheet.jpg",
              cols=8 if cam == "desk" else 16)
        # what the page will actually show: the WebP frames, decoded
        for f in (beats["s1"][0], beats["s2"][0], beats["cut"][0] + 9, beats["s3"][1]):
            Image.open(io.BytesIO(enc[f])).save(review / f"{cam}-as-shipped-f{f:04d}.png")

    (web / "film.json").write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    print("manifest:", web / "film.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
