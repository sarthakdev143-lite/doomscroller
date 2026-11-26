"""Where customer files live, and signed links to them.

Layout under SAAS_DATA_DIR (paths stored in the DB are relative to it, forward slashes):
    <user_id>/uploads/<random>.<ext>
    <user_id>/jobs/<job_id>/out/...      finished clips and thumbnails
    <user_id>/jobs/<job_id>/work/...     engine scratch, removed after the job

`resolve()` refuses any stored path that would land outside the data root, so a corrupted or crafted
row can never make the server stream an arbitrary file.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Optional

from ..auth.crypto import sign, verify_sig
from ..config import settings

log = logging.getLogger("saas.jobs.storage")

FILE_KINDS = ("video", "download", "thumbnail")


def root() -> Path:
    p = Path(settings.data_dir).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_dir(user_id: str) -> Path:
    if not user_id or "/" in user_id or "\\" in user_id or ".." in user_id:
        raise ValueError("bad user id")
    return root() / user_id


def uploads_dir(user_id: str) -> Path:
    d = user_dir(user_id) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_dir(user_id: str, job_id: str) -> Path:
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        raise ValueError("bad job id")
    return user_dir(user_id) / "jobs" / job_id


def relative(path: Path) -> str:
    return path.resolve().relative_to(root()).as_posix()


def resolve(rel: Optional[str]) -> Optional[Path]:
    if not rel:
        return None
    base = root()
    p = (base / rel).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        log.error("refused path outside the data root: %r", rel)
        return None
    return p


def within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def remove_tree(path: Path) -> None:
    if path.exists() and within(path, root()):
        shutil.rmtree(path, ignore_errors=True)


def remove_file(rel: Optional[str]) -> None:
    p = resolve(rel)
    if p is not None and p.is_file():
        try:
            p.unlink()
        except OSError:
            log.warning("could not delete %s", rel)


def signed_file_url(clip_id: str, kind: str, ttl: Optional[int] = None) -> tuple[str, int]:
    exp = int(time.time()) + int(ttl or settings.signed_url_ttl_s)
    sig = sign("clip-file", f"{clip_id}:{kind}:{exp}")
    return f"{settings.public_url.rstrip('/')}/files/clips/{clip_id}/{kind}?exp={exp}&sig={sig}", exp


def verify_file_signature(clip_id: str, kind: str, exp: int, sig: str) -> bool:
    if kind not in FILE_KINDS or exp < time.time():
        return False
    return verify_sig("clip-file", f"{clip_id}:{kind}:{exp}", sig)
