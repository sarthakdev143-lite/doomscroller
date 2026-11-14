"""Initialize the database (create all tables and apply idempotent schema upgrades)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.db import init_db

_URL_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@")


def _redact_db_url(url: str) -> str:
    return _URL_CRED_RE.sub(r"\1[REDACTED]@", str(url or ""))


def main() -> None:
    print(f"DB URL: {_redact_db_url(settings.db_url)}")
    init_db()
    print("Database initialized.")


if __name__ == "__main__":
    main()
