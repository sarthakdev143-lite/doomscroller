"""SQLite online backups of the SaaS database (Postgres: use pg_dump; this refuses).

`run_backup()` uses SQLite's online backup API (consistent even while the server writes, WAL included),
writes to a temp name, checks the copy with `PRAGMA quick_check`, then renames it to
`saas-YYYYMMDD-HHMMSS-mmm.db` and keeps the newest SAAS_BACKUP_KEEP. `scripts/saas_manage.py backup` runs one
by hand; `scripts/saas_serve.py` runs one whenever the newest is older than SAAS_BACKUP_EVERY_HOURS.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger("saas.backup")
PREFIX = "saas-"


class BackupError(RuntimeError):
    pass


def sqlite_path(url: Optional[str] = None) -> Optional[Path]:
    """The database file behind a sqlite URL; None for in-memory or non-SQLite databases."""
    from sqlalchemy.engine import make_url

    u = make_url(url or settings.database_url)
    if u.get_backend_name() != "sqlite" or not u.database or u.database == ":memory:":
        return None
    return Path(u.database).resolve()


def backup_dir(db_file: Path) -> Path:
    d = Path(settings.backup_dir).expanduser() if settings.backup_dir else db_file.parent / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _existing(d: Path) -> list[Path]:
    """Oldest first, by modification time (names alone mis-order a same-second -2 suffix)."""
    files = [p for p in d.glob(f"{PREFIX}*.db") if p.is_file()]
    return sorted(files, key=lambda p: (p.stat().st_mtime_ns, p.name))


def due(d: Optional[Path] = None, *, now: Optional[float] = None) -> bool:
    db_file = sqlite_path()
    if db_file is None or not db_file.exists():
        return False
    files = _existing(d or backup_dir(db_file))
    if not files:
        return True
    newest = max(p.stat().st_mtime for p in files)
    return (now or time.time()) - newest >= settings.backup_every_hours * 3600


def run_backup(*, db_url: Optional[str] = None, dest: Optional[Path] = None, keep: Optional[int] = None) -> Path:
    db_file = sqlite_path(db_url)
    if db_file is None:
        raise BackupError("backups are built in for SQLite only; for Postgres use pg_dump")
    if not db_file.exists():
        raise BackupError(f"database file not found: {db_file}")
    d = dest or backup_dir(db_file)
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}"
    final = d / f"{PREFIX}{stamp}.db"
    n = 1
    while final.exists():
        n += 1
        final = d / f"{PREFIX}{stamp}-{n}.db"
    tmp = final.with_suffix(".db.part")
    src = sqlite3.connect(str(db_file), timeout=60)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise BackupError(f"backup failed its integrity check: {ok}")
    os.replace(tmp, final)
    prune(d, settings.backup_keep if keep is None else keep)
    log.info("backup written: %s", final.name)
    return final


def prune(d: Path, keep: int) -> list[Path]:
    files = _existing(d)
    old = files[: max(0, len(files) - max(1, int(keep)))]
    for p in old:
        p.unlink(missing_ok=True)
    return old
