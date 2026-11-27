"""Scrub a folder before it is uploaded as a workflow artifact, then prove no key is left in it.

    python scripts/showcase/scrub_artifacts.py <folder>

1. Every text file (.log .json .txt .srt .vtt .ass .md .csv) is rewritten through
   ``app.logging_config.scrub_secrets``, which redacts the configured keys and token-shaped strings.
2. Every file, text or binary, is then searched byte-for-byte for each key in the environment
   (``envload.SECRET_ENV_NAMES``, and each part of a comma-separated backup list). A file that still holds one is
   deleted and the script exits 1, naming only the file and the variable, never the value.
Run it with the same secrets in the environment as the cut, or step 2 has nothing to compare against.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envload  # noqa: E402  (sets DOOMSCROLLER_ENV_FILE before app.* is imported)

TEXT_SUFFIXES = {".log", ".json", ".txt", ".srt", ".vtt", ".ass", ".md", ".csv"}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: scrub_artifacts.py <folder>")
    root = Path(args[0])
    if not root.is_dir():
        print(f"[scrub] nothing to scrub: {root} does not exist")
        return 0
    from app.logging_config import scrub_secrets

    files = [p for p in root.rglob("*") if p.is_file()]
    scrubbed = 0
    for path in files:
        if path.suffix.lower() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            clean = scrub_secrets(text)
            if clean != text:
                path.write_text(clean, encoding="utf-8")
                scrubbed += 1
    secrets = [(name, value) for name in envload.SECRET_ENV_NAMES
               for value in {v.strip() for v in [os.environ.get(name, "")] + os.environ.get(name, "").split(",")}
               if len(value) >= 8]
    if not secrets:
        print("::warning::no secrets in the environment, so the leak check compared nothing")
    leaks = []
    for path in files:
        if not path.exists():
            continue
        data = path.read_bytes()
        hits = sorted({name for name, value in secrets if value.encode("utf-8") in data})
        if hits:
            leaks.append((path, hits))
            path.unlink()
    print(f"[scrub] {len(files)} files checked, {scrubbed} text files redacted, {len(leaks)} removed for holding a key")
    for path, names in leaks:
        print(f"::error title=Key found in artifact::{path.relative_to(root)} held {', '.join(names)}; file removed")
    return 1 if leaks else 0


if __name__ == "__main__":
    raise SystemExit(main())
