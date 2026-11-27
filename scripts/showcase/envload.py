"""Environment for the showcase scripts. Import this before anything from ``app.*``.

The showcase cut (``.github/workflows/showcase.yml``) runs the project's own clip pipeline to make the
footage for this project's website. Its keys come only from the process environment: repository
secrets on GitHub Actions. ``DOOMSCROLLER_ENV_FILE`` is forced empty here because the owner's ``.env``
turns real YouTube uploads on and must never configure this run.

Locally (optional), ``SHOWCASE_ENV_FILE=/path/to/.env.saas`` exports that file's lines the way
``scripts/saas_serve.py`` does: variables already set win, and the owner's ``.env`` is refused.
Values are never printed; :func:`load` returns only the names it exported.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["DOOMSCROLLER_ENV_FILE"] = ""  # must precede any import of app.*
os.environ.setdefault("SAAS_ENV_FILE", "")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The settings that carry credentials, by environment-variable name (values come from secrets).
SECRET_ENV_NAMES = ("CLIP_AI_API_KEY", "CLIP_STT_API_KEY", "DEEPGRAM_API_KEY", "PIXABAY_API_KEY")


def load() -> list[str]:
    """Export ``SHOWCASE_ENV_FILE`` (if set) into ``os.environ``; existing variables win. Returns names loaded."""
    name = os.environ.get("SHOWCASE_ENV_FILE", "").strip()
    if not name:
        return []
    path = Path(name).expanduser().resolve()
    if path == (ROOT / ".env").resolve() or path.name == ".env":
        raise SystemExit("refusing to load the owner's .env for the showcase cut; point SHOWCASE_ENV_FILE at .env.saas")
    if not path.is_file():
        raise SystemExit(f"SHOWCASE_ENV_FILE does not exist: {path}")
    from dotenv import dotenv_values

    loaded = []
    for key, value in dotenv_values(path).items():
        if value is None or key in os.environ or key == "DOOMSCROLLER_ENV_FILE" or key.startswith("SAAS_"):
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
