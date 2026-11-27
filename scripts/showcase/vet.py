"""Build a vetting pack for one showcase source: cheap frame grabs and checks a person reads before approving it.

    python scripts/showcase/vet.py --source-id eye_on_neptune --src source.mp4 --srt source.srt --out vet/

What it writes (no AI calls, no keys):
* ``vet_report.json``: sha256 and size, stream info, the kept segments as planned (snapped to pauses if set),
  a music check (a narration-only track falls silent between sentences; every 15 s of a kept segment should hold
  at least one pause at -50 dB), the source captions inside the kept segments, and hits for dated wording
  (months, "tonight", "anniversary", years) or "NASA".
* contact sheets of the whole original (one frame every 2 s), strips of both top corners every 4 s (insignia
  bugs and logos), strips of the bottom credit band every 2 s, and frames around every planned cut point.

Approving a source (``approved: true`` and a pinned ``sha256`` in the edit list) stays a human decision.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ffmpeg, label, parse_srt, probe, silences, stamp, write_json  # noqa: E402
from rebuild_working_copy import EDL, load_source, plan_segments  # noqa: E402

DATED = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
                   r"tonight|tomorrow|this (?:week|month|year|summer|winter|fall|spring)|next (?:week|month|year)|"
                   r"anniversary|(?:19|20)\d\d)\b", re.IGNORECASE)
NASA_WORD = re.compile(r"\bnasa\b", re.IGNORECASE)
MUSIC_BIN_SEC = 15.0


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def music_check(segments: list[dict], quiet: list[tuple[float, float]]) -> list[dict]:
    out = []
    for seg in segments:
        a, b = seg["orig_start"], seg["orig_end"]
        bins, t = [], a
        while t < b - 1.0:
            end = min(b, t + MUSIC_BIN_SEC)
            has_pause = any(s < end and e > t for s, e in quiet)
            bins.append({"from": round(t, 2), "to": round(end, 2), "pause": has_pause})
            t = end
        out.append({"segment": [a, b], "bins_without_pause": sum(1 for x in bins if not x["pause"]),
                    "bins": bins})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--edl", default=str(EDL))
    ap.add_argument("--source-id", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--srt", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    doc = load_source(args.edl, args.source_id)
    src = Path(args.src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    report: dict = {"source_id": args.source_id, "title": doc["title"], "url": doc["url"],
                    "sha256": sha256_of(src), "bytes": src.stat().st_size,
                    "pinned_sha256": doc.get("sha256"), "pinned_bytes": doc.get("bytes"),
                    "streams": info["streams"], "duration": float(info["format"]["duration"]),
                    "approved": bool(doc.get("approved")), "open_items": doc.get("open_items", [])}
    try:
        plan = plan_segments(doc, src)
    except ValueError as exc:  # still build the pack, from the unsnapped times, and say why
        report["plan_error"] = str(exc)
        print(f"::warning title=Edit list does not fit the source::{exc}")
        plan = plan_segments(doc, src, snap_to_silence=False)
    report["plan"] = plan
    audio = int(doc.get("audio_stream", 0))
    quiet50 = silences(src, noise_db=-50, min_sec=0.25, audio_stream=audio)
    quiet60 = silences(src, noise_db=-60, min_sec=0.25, audio_stream=audio)
    report["pauses"] = {"at_-50dB": len(quiet50), "at_-60dB": len(quiet60),
                        "intervals_-60dB": [[round(a, 2), round(b, 2)] for a, b in quiet60]}
    report["music_check"] = music_check(plan["segments"], quiet50)
    report["music_verdict"] = ("narration only (a pause in every 15 s of every kept segment)"
                               if all(m["bins_without_pause"] == 0 for m in report["music_check"])
                               else "SUSPECT: some kept 15 s windows never fall silent (a music bed?)")

    kept_cues, flags = [], []
    if args.srt and Path(args.srt).is_file():
        cues = parse_srt(Path(args.srt).read_text(encoding="utf-8", errors="replace"))
        for start, end, text in cues:
            for seg in plan["segments"]:
                if start < seg["orig_end"] and end > seg["orig_start"]:
                    kept_cues.append({"start": start, "end": end, "text": text})
                    for rx, kind in ((DATED, "dated"), (NASA_WORD, "says NASA")):
                        for m in rx.finditer(text):
                            flags.append({"kind": kind, "word": m.group(0), "at": start, "text": text})
                    break
    report["kept_captions"] = kept_cues
    report["wording_flags"] = flags

    ts = stamp()
    ffmpeg(["-y", "-i", str(src), "-vf", f"fps=0.5,scale=320:-2,{ts},tile=6x6", "-fps_mode", "vfr",
            str(out / "sheet_%02d.jpg")])
    for name, crop in (("corner_top_right", "crop=iw*0.32:ih*0.2:iw*0.68:0"),
                       ("corner_top_left", "crop=iw*0.32:ih*0.2:0:0")):
        ffmpeg(["-y", "-i", str(src), "-vf", f"fps=0.25,{crop},scale=400:-2,{ts},tile=4x8", "-fps_mode", "vfr",
                str(out / f"{name}_%02d.jpg")])
    ffmpeg(["-y", "-i", str(src), "-vf", f"fps=0.5,crop=iw:ih*0.14:0:ih*0.86,scale=1280:-2,{ts},tile=1x20",
            "-fps_mode", "vfr", str(out / "credit_band_%02d.jpg")])
    for k, seg in enumerate(plan["segments"]):
        for edge in ("orig_start", "orig_end"):
            t = seg[edge]
            ffmpeg(["-y", "-ss", f"{max(0.0, t - 0.5):.3f}", "-t", "1.0", "-i", str(src), "-vf",
                    f"fps=10,scale=320:-2,{label(edge + ' ' + str(t) + 's')},tile=10x1", "-frames:v", "1",
                    str(out / f"cut_{k + 1}_{edge}.jpg")])
    write_json(out / "vet_report.json", report)

    print(f"[vet] {args.source_id}: sha256 {report['sha256']} ({report['bytes']} bytes); pinned "
          f"{report['pinned_sha256'] or 'none'}")
    print(f"[vet] music: {report['music_verdict']}")
    for flag in flags:
        print(f"[vet] wording flag ({flag['kind']}) at {flag['at']:.1f}s: {flag['text']}")
    if doc.get("sha256") and doc["sha256"] != report["sha256"]:
        print("::error title=Source changed::sha256 differs from the edit list; re-vet before cutting")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
