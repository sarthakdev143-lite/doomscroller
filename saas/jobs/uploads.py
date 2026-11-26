"""Streamed video uploads: `POST /api/uploads`, multipart with one file field named `file`.

FastAPI's UploadFile would spool the whole body before our code ran, so the body is parsed here,
chunk by chunk, with python-multipart's streaming parser, written straight to a temp file and
aborted the moment it passes the plan's cap (`stream_file`, shared with the brand-kit uploads). Then,
before anything else sees the file: extension allowlist -> magic bytes -> ffprobe (file protocol and
container whitelist only) -> a video stream, a duration within the plan's limit, sane dimensions, and
a plausible bitrate (a 1 MB file that claims ten hours is refused). Accepted files get a random name
under SAAS_DATA_DIR/<user_id>/uploads/.
"""
from __future__ import annotations

import logging
import math
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Optional

import anyio
from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from ..auth.errors import ApiError
from ..billing import catalog
from ..common import utcnow
from ..config import settings
from ..models.core import User
from ..models.jobs import Upload
from . import probe, storage

log = logging.getLogger("saas.jobs.uploads")

ALLOWED_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
MAX_DIMENSION = 8192
MIN_BYTES_PER_SECOND = 2_000  # ~16 kbit/s: below this a long "video" is a crafted header, not footage
MAX_PENDING_UPLOADS = 10
_FLUSH_AT = 1024 * 1024
_OTHER_PARTS_CAP = 64 * 1024


class _TooLarge(Exception):
    pass


def sniff_container(head: bytes) -> Optional[str]:
    """'mp4' | 'matroska' | 'avi' from the first bytes, else None."""
    if len(head) >= 12 and head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    return None


_EXT_FAMILY = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mp4", ".mkv": "matroska", ".webm": "matroska", ".avi": "avi"}


def clean_name(raw: str, default: str = "video") -> str:
    base = os.path.basename(raw.replace("\\", "/"))
    base = re.sub(r"[^\w.\- ()]+", "_", base).strip(" .")
    return base[:200] or default


async def stream_file(request: Request, folder: Path, cap: int, too_large_message: str) -> tuple[Path, str, int]:
    """Stream the multipart field `file` to a temp file in `folder`, aborting past `cap` bytes.

    Returns (temp path, client filename, size). The temp file is deleted on any failure; on success
    the caller owns it (and must move or delete it).
    """
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise ApiError(415, "multipart_required", "Upload as multipart/form-data with a 'file' field.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + _OTHER_PARTS_CAP:
        raise ApiError(413, "upload_too_large", too_large_message)

    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{secrets.token_hex(16)}.part"
    state: dict = {"headers": {}, "field": b"", "value": b"", "is_file": False, "written": 0, "filename": None,
                   "seen_file": False, "buf": bytearray(), "other_bytes": 0}

    def on_part_begin() -> None:
        state["headers"] = {}
        state["is_file"] = False

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["value"] += data[start:end]

    def on_header_end() -> None:
        state["headers"][state["field"].decode("latin-1").lower()] = state["value"]
        state["field"], state["value"] = b"", b""

    def on_headers_finished() -> None:
        disp, opts = parse_options_header(state["headers"].get("content-disposition", b""))
        if disp == b"form-data" and opts.get(b"name") == b"file" and b"filename" in opts and not state["seen_file"]:
            state["is_file"] = True
            state["seen_file"] = True
            state["filename"] = opts[b"filename"].decode("utf-8", "replace")

    def on_part_data(data: bytes, start: int, end: int) -> None:
        n = end - start
        if state["is_file"]:
            state["written"] += n
            if state["written"] > cap:
                raise _TooLarge()
            state["buf"] += data[start:end]
        else:
            state["other_bytes"] += n
            if state["other_bytes"] > _OTHER_PARTS_CAP:
                raise _TooLarge()

    ok = False
    fh = open(tmp, "wb")
    try:
        parser = MultipartParser(boundary, callbacks={
            "on_part_begin": on_part_begin, "on_header_field": on_header_field, "on_header_value": on_header_value,
            "on_header_end": on_header_end, "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
        })

        async def flush() -> None:
            if state["buf"]:
                chunk = bytes(state["buf"])
                state["buf"].clear()
                await anyio.to_thread.run_sync(fh.write, chunk)

        try:
            async for chunk in request.stream():
                parser.write(chunk)
                if len(state["buf"]) >= _FLUSH_AT:
                    await flush()
            parser.finalize()
            await flush()
        except _TooLarge:
            raise ApiError(413, "upload_too_large", too_large_message)
        except ClientDisconnect:
            raise ApiError(400, "upload_interrupted", "The upload was interrupted. Try again.")
        except ApiError:
            raise
        except Exception:
            raise ApiError(400, "invalid_multipart", "The upload was malformed. Try again.")
        finally:
            fh.close()
        if not state["seen_file"] or state["written"] == 0:
            raise ApiError(400, "file_required", "Attach the file in a field named 'file'.")
        ok = True
        return tmp, state["filename"] or "", state["written"]
    finally:
        if not fh.closed:
            fh.close()
        if not ok:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


async def receive(request: Request, db: Session, user: User) -> Upload:
    feats = catalog.features_for(db, user)
    cap_mb = min(int(feats.get("max_upload_mb", 500)), settings.max_upload_mb)
    pending = db.query(Upload).filter(Upload.user_id == user.id, Upload.job_id.is_(None),
                                      Upload.deleted_at.is_(None)).count()
    if pending >= MAX_PENDING_UPLOADS:
        raise ApiError(429, "too_many_uploads", "You have too many unused uploads. Start a job or delete some first.")

    tmp, filename, size = await stream_file(request, storage.uploads_dir(user.id), cap_mb * 1024 * 1024,
                                            f"Files can be up to {cap_mb} MB on your plan.")
    ok = False
    try:
        original = clean_name(filename or "video")
        ext = Path(original).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ApiError(415, "unsupported_type", "Upload an MP4, MOV, MKV, WebM or AVI video.")
        with open(tmp, "rb") as f:
            head = f.read(16)
        family = sniff_container(head)
        if family is None or family != _EXT_FAMILY[ext]:
            raise ApiError(415, "unsupported_type", "That file's contents don't match a supported video format.")
        info = await anyio.to_thread.run_sync(probe.ffprobe, tmp)
        upload = _accept(db, user, tmp, original, ext, size, info, feats)
        ok = True
        return upload
    finally:
        if not ok:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _accept(db: Session, user: User, tmp: Path, original: str, ext: str, size: int, info: dict, feats: dict) -> Upload:
    duration = float(info.get("duration_s") or 0)
    if not info.get("has_video") or duration <= 0:
        raise ApiError(415, "invalid_media", "That file has no playable video.")
    if not (0 < info.get("width", 0) <= MAX_DIMENSION and 0 < info.get("height", 0) <= MAX_DIMENSION):
        raise ApiError(415, "invalid_media", "That video's dimensions aren't supported.")
    max_s = int(feats.get("max_source_minutes", 30)) * 60
    if duration > max_s:
        raise ApiError(400, "source_too_long", f"Videos can be up to {max_s // 60} minutes on your plan.",
                       duration_s=duration)
    if duration > 120 and size / duration < MIN_BYTES_PER_SECOND:
        raise ApiError(415, "implausible_media", "That file's length doesn't match its size. Re-export it and try again.")
    final = tmp.with_name(f"{secrets.token_hex(16)}{ext}")
    tmp.replace(final)
    now = utcnow()
    upload = Upload(user_id=user.id, original_name=original, path=storage.relative(final), size_bytes=size,
                    duration_s=round(duration, 2), width=int(info["width"]), height=int(info["height"]),
                    container=str(info.get("format_name", ""))[:40], credits=max(1, math.ceil(duration / 60)),
                    created_at=now, expires_at=now + timedelta(hours=settings.upload_ttl_hours))
    db.add(upload)
    db.commit()
    return upload


def public(u: Upload) -> dict:
    return {"upload_id": u.id, "name": u.original_name, "size_bytes": u.size_bytes, "duration_s": u.duration_s,
            "credits": u.credits, "width": u.width, "height": u.height,
            "expires_at": u.expires_at.isoformat() if u.expires_at else None}
