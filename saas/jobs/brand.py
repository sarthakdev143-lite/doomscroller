"""Brand kit: a customer's logo and their own memes, stored per user and handed to the engine.

Nothing a customer uploads is kept as sent. Every file is size-capped while streaming, checked by
magic bytes, decoded, and re-encoded by us, so metadata, trailing payloads and polyglot tricks are
dropped:
* logo: PNG / JPEG / WebP decoded by Pillow (decompression-bomb guard on), downscaled to 1024 px,
  saved as PNG. SVG is rasterised server-side only when an SVG renderer (cairosvg) is installed, with
  external resources refused; otherwise SVG uploads get 415 `svg_unsupported`.
* memes: PNG and GIF decoded and re-encoded by Pillow (GIF animation kept, <= 10 s); MP4/MOV/WebM
  probed with ffprobe (file protocol + container whitelist), <= 10 s, then transcoded by ffmpeg to a
  small H.264 MP4 with all metadata stripped.

The worker passes `logo_path` and `custom_memes` (absolute paths) to `clip_to_directory`.
"""
from __future__ import annotations

import io
import logging
import secrets
import subprocess
import warnings
from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from PIL import Image, ImageSequence
from sqlalchemy.orm import Session

from ..auth import compat
from ..auth.deps import require_user, require_verified_user
from ..auth.errors import ApiError, ApiRoute
from ..common import utcnow
from ..config import settings
from ..db import get_db
from ..models.core import User
from ..models.jobs import BrandAsset
from . import probe, storage
from .uploads import clean_name, sniff_container, stream_file

log = logging.getLogger("saas.jobs.brand")

LOGO_MAX_BYTES = 5 * 1024 * 1024
LOGO_MAX_PIXELS = 4096
LOGO_OUT_PX = 1024
MEME_MAX_BYTES = 15 * 1024 * 1024
MEME_MAX_SECONDS = 10.0
MEME_MAX_PIXELS = 4096
MEME_IMAGE_OUT_PX = 720
MEME_GIF_OUT_PX = 480
MEME_MAX_FRAMES = 300
MAX_MEMES = 50
_PIL_PIXEL_LIMIT = 40_000_000

router = APIRouter(route_class=ApiRoute)


def meme_key(path: Path) -> str:
    """The id the clip engine gives this file as a custom meme (app.clipper.memes.custom_candidates:
    "custom-" + the first 20 hex chars of the SHA-1 of its bytes). Editor specs reference memes by it."""
    import hashlib

    digest = hashlib.sha1()  # noqa: S324 - an identity, not a security hash; must match the engine
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return f"custom-{digest.hexdigest()[:20]}"


def brand_dir(user_id: str) -> Path:
    d = storage.user_dir(user_id) / "brand"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sniff_image(head: bytes) -> Optional[str]:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"<svg") or (stripped.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    return None


def _open_image(data: bytes) -> Image.Image:
    """Decode fully with Pillow's decompression-bomb guard turned into a hard error."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        old = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = _PIL_PIXEL_LIMIT
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
            return im
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise ApiError(415, "image_too_large", "That image is too large.")
        except ApiError:
            raise
        except Exception:
            raise ApiError(415, "invalid_image", "That file isn't an image we can read.")
        finally:
            Image.MAX_IMAGE_PIXELS = old


def rasterise_svg(data: bytes) -> bytes:
    """PNG bytes from SVG, refusing external resources. Raises 415 when no renderer is installed."""
    try:
        import cairosvg  # optional dependency
    except ImportError:
        raise ApiError(415, "svg_unsupported", "SVG logos aren't supported on this server yet. Upload a PNG.")

    def refuse(*_a, **_k):
        raise ValueError("external resources are not allowed")

    try:
        return cairosvg.svg2png(bytestring=data, output_width=LOGO_OUT_PX, unsafe=False, url_fetcher=refuse)
    except Exception:
        raise ApiError(415, "invalid_image", "That SVG couldn't be rendered.")


def process_logo(data: bytes) -> tuple[bytes, int, int]:
    kind = sniff_image(data[:512])
    if kind == "svg":
        data = rasterise_svg(data)
        kind = "png"
    if kind not in ("png", "jpeg", "webp"):
        raise ApiError(415, "unsupported_type", "Upload the logo as PNG, JPEG, WebP or SVG.")
    im = _open_image(data)
    if im.width > LOGO_MAX_PIXELS or im.height > LOGO_MAX_PIXELS or im.width < 16 or im.height < 16:
        raise ApiError(415, "invalid_image", f"Logos must be between 16 and {LOGO_MAX_PIXELS} pixels per side.")
    im = im.convert("RGBA")
    im.thumbnail((LOGO_OUT_PX, LOGO_OUT_PX))
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)  # a fresh PNG: no text chunks, EXIF or trailing bytes
    return out.getvalue(), im.width, im.height


def process_image_meme(data: bytes, kind: str) -> tuple[bytes, str, int, int, float]:
    im = _open_image(data)
    if im.width > MEME_MAX_PIXELS or im.height > MEME_MAX_PIXELS:
        raise ApiError(415, "invalid_image", "That image is too large.")
    out = io.BytesIO()
    if kind == "gif":
        frames, durations = [], []
        for i, frame in enumerate(ImageSequence.Iterator(im)):
            if i >= MEME_MAX_FRAMES:
                raise ApiError(415, "meme_too_long", f"Animated memes can be up to {int(MEME_MAX_SECONDS)} seconds.")
            f = frame.convert("RGBA")
            f.thumbnail((MEME_GIF_OUT_PX, MEME_GIF_OUT_PX))
            frames.append(f)
            durations.append(max(20, int(frame.info.get("duration", 100) or 100)))
        total = sum(durations) / 1000.0
        if total > MEME_MAX_SECONDS:
            raise ApiError(415, "meme_too_long", f"Animated memes can be up to {int(MEME_MAX_SECONDS)} seconds.")
        frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0,
                       disposal=2, optimize=False)
        return out.getvalue(), "image/gif", frames[0].width, frames[0].height, round(total, 2)
    img = im.convert("RGBA")
    img.thumbnail((MEME_IMAGE_OUT_PX, MEME_IMAGE_OUT_PX))
    img.save(out, format="PNG", optimize=True)
    return out.getvalue(), "image/png", img.width, img.height, 0.0


def transcode_video_meme(src: Path, dst: Path) -> dict:
    info = probe.ffprobe(src)
    duration = float(info.get("duration_s") or 0)
    if not info.get("has_video") or duration <= 0:
        raise ApiError(415, "invalid_media", "That file has no playable video.")
    if duration > MEME_MAX_SECONDS + 0.5:
        raise ApiError(415, "meme_too_long", f"Video memes can be up to {int(MEME_MAX_SECONDS)} seconds.")
    if not (0 < info.get("width", 0) <= MEME_MAX_PIXELS and 0 < info.get("height", 0) <= MEME_MAX_PIXELS):
        raise ApiError(415, "invalid_media", "That video's dimensions aren't supported.")
    cmd = [settings.ffmpeg_bin, "-hide_banner", "-v", "error", "-nostdin",
           "-protocol_whitelist", "file", "-format_whitelist", probe.FFPROBE_FORMATS, "-i", str(src),
           "-t", str(MEME_MAX_SECONDS), "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
           "-vf", f"scale='trunc(min({MEME_IMAGE_OUT_PX},iw)/2)*2':-2", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-y", str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False, **probe.background_kwargs())
    except FileNotFoundError:
        raise ApiError(503, "media_tools_unavailable", "Video memes are temporarily unavailable.")
    except subprocess.TimeoutExpired:
        raise ApiError(415, "invalid_media", "That video took too long to process.")
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise ApiError(415, "invalid_media", "That video couldn't be processed.")
    out = probe.ffprobe(dst)
    return {"width": out.get("width", 0), "height": out.get("height", 0), "duration_s": out.get("duration_s", 0.0)}


# ---- persistence ----------------------------------------------------------------------------------

def _logo(db: Session, user_id: str) -> Optional[BrandAsset]:
    return (db.query(BrandAsset).filter(BrandAsset.user_id == user_id, BrandAsset.kind == "logo",
                                        BrandAsset.deleted_at.is_(None))
            .order_by(BrandAsset.created_at.desc()).first())


def _memes(db: Session, user_id: str, limit: int = MAX_MEMES) -> list[BrandAsset]:
    return (db.query(BrandAsset).filter(BrandAsset.user_id == user_id, BrandAsset.kind == "meme",
                                        BrandAsset.deleted_at.is_(None))
            .order_by(BrandAsset.created_at.desc()).limit(limit).all())


def _remove(db: Session, asset: BrandAsset) -> None:
    storage.remove_file(asset.path)
    asset.deleted_at = utcnow()


def public(a: BrandAsset) -> dict:
    url = "/api/brand/logo/file" if a.kind == "logo" else f"/api/brand/memes/{a.id}/file"
    return {"id": a.id, "kind": a.kind, "name": a.original_name, "media_type": a.media_type,
            "size_bytes": a.size_bytes, "width": a.width, "height": a.height, "duration_s": a.duration_s,
            "url": url, "created_at": a.created_at.isoformat()}


def engine_assets(db: Session, user_id: str, options: dict) -> tuple[Optional[str], list[str]]:
    """(logo path, custom meme paths) for a job, honouring the job's `logo` / `custom_memes` options."""
    logo_path = None
    if options.get("logo", True):
        logo = _logo(db, user_id)
        p = storage.resolve(logo.path) if logo else None
        logo_path = str(p) if p is not None and p.is_file() else None
    memes: list[str] = []
    if options.get("custom_memes", True):
        for m in _memes(db, user_id):
            p = storage.resolve(m.path)
            if p is not None and p.is_file():
                memes.append(str(p))
    return logo_path, memes


def purge(db: Session, user_id: str) -> None:
    for a in db.query(BrandAsset).filter(BrandAsset.user_id == user_id, BrandAsset.deleted_at.is_(None)).all():
        _remove(db, a)


# ---- routes ---------------------------------------------------------------------------------------

@router.get("/api/brand")
def get_brand(user: User = Depends(require_user), db: Session = Depends(get_db)):
    logo = _logo(db, user.id)
    return {"logo": public(logo) if logo else None, "memes": [public(m) for m in _memes(db, user.id)],
            "limits": {"logo_max_mb": LOGO_MAX_BYTES // (1024 * 1024), "meme_max_mb": MEME_MAX_BYTES // (1024 * 1024),
                       "meme_max_seconds": MEME_MAX_SECONDS, "max_memes": MAX_MEMES,
                       "logo_types": ["png", "jpeg", "webp", "svg"], "meme_types": ["gif", "png", "mp4", "mov", "webm"]}}


@router.post("/api/brand/logo", status_code=201)
async def upload_logo(request: Request, user: User = Depends(require_verified_user), db: Session = Depends(get_db)):
    compat.rate_limit(request, "brand_upload", 30, 3600, key=f"user:{user.id}")
    folder = brand_dir(user.id)
    tmp, filename, size = await stream_file(request, folder, LOGO_MAX_BYTES,
                                            f"Logos can be up to {LOGO_MAX_BYTES // (1024 * 1024)} MB.")
    try:
        data = tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)
    png, w, h = await anyio.to_thread.run_sync(process_logo, data)
    final = folder / f"logo_{secrets.token_hex(12)}.png"
    final.write_bytes(png)
    old = _logo(db, user.id)
    if old is not None:
        _remove(db, old)
    asset = BrandAsset(user_id=user.id, kind="logo", path=storage.relative(final), media_type="image/png",
                       original_name=clean_name(filename, "logo"), size_bytes=len(png), width=w, height=h)
    db.add(asset)
    db.commit()
    return public(asset)


@router.delete("/api/brand/logo")
def delete_logo(user: User = Depends(require_user), db: Session = Depends(get_db)):
    logo = _logo(db, user.id)
    if logo is None:
        raise ApiError(404, "not_found", "No logo uploaded.")
    _remove(db, logo)
    db.commit()
    return {"ok": True}


@router.get("/api/brand/logo/file")
def logo_file(user: User = Depends(require_user), db: Session = Depends(get_db)):
    logo = _logo(db, user.id)
    p = storage.resolve(logo.path) if logo else None
    if p is None or not p.is_file():
        raise ApiError(404, "not_found", "No logo uploaded.")
    # One URL for whichever logo is current, so the browser revalidates (ETag) rather than show a replaced one.
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, no-cache"})


@router.post("/api/brand/memes", status_code=201)
async def upload_meme(request: Request, user: User = Depends(require_verified_user), db: Session = Depends(get_db)):
    compat.rate_limit(request, "brand_upload", 30, 3600, key=f"user:{user.id}")
    if len(_memes(db, user.id)) >= MAX_MEMES:
        raise ApiError(409, "too_many_memes", f"You can keep up to {MAX_MEMES} memes. Delete some first.")
    folder = brand_dir(user.id)
    tmp, filename, size = await stream_file(request, folder, MEME_MAX_BYTES,
                                            f"Memes can be up to {MEME_MAX_BYTES // (1024 * 1024)} MB.")
    name = clean_name(filename, "meme")
    try:
        with open(tmp, "rb") as f:
            head = f.read(64)
        image_kind = sniff_image(head)
        if image_kind in ("png", "gif"):
            data = tmp.read_bytes()
            body, media_type, w, h, dur = await anyio.to_thread.run_sync(process_image_meme, data, image_kind)
            final = folder / f"meme_{secrets.token_hex(12)}.{'gif' if media_type == 'image/gif' else 'png'}"
            final.write_bytes(body)
            stored = len(body)
        elif sniff_container(head[:16]) in ("mp4", "matroska"):
            final = folder / f"meme_{secrets.token_hex(12)}.mp4"
            meta = await anyio.to_thread.run_sync(transcode_video_meme, tmp, final)
            media_type, w, h, dur = "video/mp4", int(meta["width"]), int(meta["height"]), float(meta["duration_s"])
            stored = final.stat().st_size
        else:
            raise ApiError(415, "unsupported_type", "Upload memes as GIF, PNG, MP4 or WebM.")
    finally:
        tmp.unlink(missing_ok=True)
    asset = BrandAsset(user_id=user.id, kind="meme", path=storage.relative(final), media_type=media_type,
                       original_name=name, size_bytes=stored, width=w, height=h, duration_s=dur,
                       meme_key=meme_key(final))
    db.add(asset)
    db.commit()
    return public(asset)


@router.get("/api/brand/memes/{asset_id}/file")
def meme_file(asset_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    a = db.query(BrandAsset).filter(BrandAsset.id == asset_id, BrandAsset.user_id == user.id,
                                    BrandAsset.kind == "meme", BrandAsset.deleted_at.is_(None)).one_or_none()
    p = storage.resolve(a.path) if a else None
    if p is None or not p.is_file():
        raise ApiError(404, "not_found", "Meme not found.")
    return FileResponse(p, media_type=a.media_type, headers={"Cache-Control": "private, max-age=300"})


@router.delete("/api/brand/memes/{asset_id}")
def delete_meme(asset_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    a = db.query(BrandAsset).filter(BrandAsset.id == asset_id, BrandAsset.user_id == user.id,
                                    BrandAsset.kind == "meme", BrandAsset.deleted_at.is_(None)).one_or_none()
    if a is None:
        raise ApiError(404, "not_found", "Meme not found.")
    _remove(db, a)
    db.commit()
    return {"ok": True}
