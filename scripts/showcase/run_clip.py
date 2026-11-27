"""One showcase cutting run through the SaaS seam ``app.clipper.standalone.clip_to_directory``.

    python scripts/showcase/run_clip.py --src "work/Shedding Light on Black Holes.mp4" --run-dir run/ \
        [--count 8] [--max-clip-seconds 45] [--caption-detector-override]

Fixed options for the website's showcase: memes off (Giphy content is third-party), music off, no watermark,
no brand, framed layout, captions, B-roll and the CC0 sound pack on, English. Writes into ``--run-dir``:
``clips/`` (the pipeline's output), ``result.json`` or ``error.json``, ``transcript.json`` (the pipeline's own
word timings, captured by wrapping the module-level ``transcribe``), ``already_captioned_verdict.json`` and
``run.log`` (INFO; httpx and friends at WARNING because a stock-footage key can travel in a URL). Keys come
from the environment only (repository secrets on Actions) and are never printed. Scrub ``run.log`` with
``scrub_artifacts.py`` before publishing it anywhere.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envload  # noqa: E402  (sets DOOMSCROLLER_ENV_FILE before app.* is imported)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--src", required=True, help="the working copy (its file name is the title the AI sees)")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--max-clip-seconds", type=int, default=45)
    ap.add_argument("--caption-detector-override", action="store_true",
                    help="log subtitles.already_captioned's verdict but keep our word captions (see the edit list)")
    args = ap.parse_args(argv)

    names = envload.load()
    run = Path(args.run_dir).resolve()
    run.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=str(run / "run.log"), encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("showcase")
    if names:
        log.info("exported from SHOWCASE_ENV_FILE: %s", ", ".join(sorted(names)))  # names only

    from app.clipper import ai, standalone
    from app.logging_config import scrub_secrets

    if not ai.available():
        print("::error title=AI not configured::CLIP_AI_* settings are incomplete; a run without AI makes rule-based "
              "titles and wastes the attempt", flush=True)
        return 2

    real_transcribe = standalone.transcribe

    def capturing_transcribe(*a, **kw):
        tr = real_transcribe(*a, **kw)
        try:
            doc = {"engine": getattr(tr, "engine", ""), "language": getattr(tr, "language", ""),
                   "words": [{"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)} for w in tr.words]}
            (run / "transcript.json").write_text(json.dumps(doc, indent=0, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # the run matters more than the copy
            log.warning("could not save the transcript: %s", exc)
        return tr

    standalone.transcribe = capturing_transcribe

    real_already = standalone.subtitles.already_captioned

    def logged_already_captioned(*a, **kw):
        verdict = bool(real_already(*a, **kw))
        used = False if args.caption_detector_override else verdict
        (run / "already_captioned_verdict.json").write_text(
            json.dumps({"detector_verdict": verdict, "used": used, "override": args.caption_detector_override}),
            encoding="utf-8")
        log.info("already_captioned verdict=%s used=%s", verdict, used)
        print(f"[cut] burned-in caption detector said {verdict}; using {used}", flush=True)
        return used

    standalone.subtitles.already_captioned = logged_already_captioned

    t0 = time.time()

    def stage(name: str, progress: float) -> None:
        msg = f"[cut {time.time() - t0:7.1f}s] {name} {progress:.0%}"
        print(msg, flush=True)
        log.info(msg)

    try:
        result = standalone.clip_to_directory(
            str(Path(args.src).resolve()), run / "clips", count=args.count, language="en", layout="framed",
            captions=True, broll=True, memes=False, sfx=True, music=False, max_clip_seconds=args.max_clip_seconds,
            brand_name=None, watermark_text=None, on_stage=stage, cache_dir=run / "cache",
            shared_cache_dir=run / "shared_cache")
    except Exception as exc:
        info = {"type": type(exc).__name__, "code": getattr(exc, "code", None),
                "message": scrub_secrets(str(getattr(exc, "message", exc))),
                "trace": scrub_secrets(traceback.format_exc())}
        (run / "error.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
        print(f"::error title=Cut failed::{info['type']} {info['code']}: {info['message'][:300]}", flush=True)
        return 1
    (run / "result.json").write_text(json.dumps(result, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"[cut] done in {time.time() - t0:.0f}s: {len(result['clips'])} clips, {len(result['warnings'])} warnings",
          flush=True)
    for warning in result["warnings"]:
        print(f"[cut] warning: {scrub_secrets(warning)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
