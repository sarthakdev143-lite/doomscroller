"""Brand kit: logo and custom memes are size/type/magic checked, decoded and re-encoded (payloads
stripped), private to their owner, and handed to the engine."""
from __future__ import annotations

import io
import shutil
import struct
import subprocess
import zlib

import pytest
from PIL import Image

from core_fixtures import core, login, make_user  # noqa: F401


def png_bytes(w=64, h=64, color=(200, 30, 30, 255), extra: bytes = b"") -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, format="PNG")
    return buf.getvalue() + extra


def gif_bytes(frames=3, ms=100, size=(40, 40)) -> bytes:
    imgs = [Image.new("RGB", size, (i * 60, 0, 0)) for i in range(frames)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=ms, loop=0)
    return buf.getvalue()


def bomb_png(w=30000, h=30000) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + chunk(b"IEND", b"")


def up(client, h, path, name, data, ctype="application/octet-stream"):
    return client.post(path, files={"file": (name, data, ctype)}, headers=h)


def test_logo_is_reencoded_and_payload_stripped(client, core, saas_db):
    from saas.jobs import storage
    from saas.models.jobs import BrandAsset

    make_user(saas_db, "logo@example.com")
    h = login(client, "logo@example.com")
    payload = b"<?php system($_GET['x']); ?>"
    r = up(client, h, "/api/brand/logo", "logo.png", png_bytes(extra=payload), "image/png")
    assert r.status_code == 201, r.text
    asset = saas_db.query(BrandAsset).one()
    stored = storage.resolve(asset.path).read_bytes()
    assert stored.startswith(b"\x89PNG") and payload not in stored
    got = client.get("/api/brand/logo/file")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    # Replacing the logo removes the old file.
    old_path = storage.resolve(asset.path)
    assert up(client, h, "/api/brand/logo", "new.jpg", _jpeg(), "image/jpeg").status_code == 201
    assert not old_path.exists()
    assert client.get("/api/brand").json()["logo"]["name"] == "new.jpg"


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (80, 40), (0, 0, 255)).save(buf, format="JPEG")
    return buf.getvalue()


def test_logo_rejections(client, core, saas_db):
    make_user(saas_db, "badlogo@example.com")
    h = login(client, "badlogo@example.com")
    assert up(client, h, "/api/brand/logo", "logo.png", b"not an image at all").json()["error"] == "unsupported_type"
    r = up(client, h, "/api/brand/logo", "logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>')
    assert r.status_code == 415 and r.json()["error"] in ("svg_unsupported", "invalid_image")
    r = up(client, h, "/api/brand/logo", "bomb.png", bomb_png())
    assert r.status_code == 415 and r.json()["error"] in ("image_too_large", "invalid_image")
    r = up(client, h, "/api/brand/logo", "huge.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (6 * 1024 * 1024))
    assert r.status_code == 413
    assert up(client, h, "/api/brand/logo", "tiny.png", png_bytes(4, 4)).status_code == 415


def test_memes_gif_png_and_limits(client, core, saas_db):
    make_user(saas_db, "memes@example.com")
    h = login(client, "memes@example.com")
    r = up(client, h, "/api/brand/memes", "lol.gif", gif_bytes())
    assert r.status_code == 201 and r.json()["media_type"] == "image/gif" and r.json()["duration_s"] == pytest.approx(0.3)
    assert up(client, h, "/api/brand/memes", "still.png", png_bytes(900, 300)).json()["width"] == 720
    long_gif = up(client, h, "/api/brand/memes", "long.gif", gif_bytes(frames=4, ms=4000))
    assert long_gif.status_code == 415 and long_gif.json()["error"] == "meme_too_long"
    assert up(client, h, "/api/brand/memes", "x.txt", b"hello").json()["error"] == "unsupported_type"
    assert len(client.get("/api/brand").json()["memes"]) == 2


@pytest.mark.slow
@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_video_memes_are_transcoded_and_capped(client, core, saas_db, tmp_path):
    from saas.jobs import storage
    from saas.models.jobs import BrandAsset

    def make(seconds):
        out = tmp_path / f"m{seconds}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        f"testsrc=size=320x240:rate=15:duration={seconds}", "-metadata", "comment=SECRET-PAYLOAD",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True, stdin=subprocess.DEVNULL,
                       timeout=120)
        return out.read_bytes()

    make_user(saas_db, "vmeme@example.com")
    h = login(client, "vmeme@example.com")
    r = up(client, h, "/api/brand/memes", "clip.mp4", make(2), "video/mp4")
    assert r.status_code == 201, r.text
    stored = storage.resolve(saas_db.query(BrandAsset).one().path).read_bytes()
    assert b"SECRET-PAYLOAD" not in stored
    r = up(client, h, "/api/brand/memes", "long.mp4", make(12), "video/mp4")
    assert r.status_code == 415 and r.json()["error"] == "meme_too_long"


def test_brand_assets_are_private_and_reach_the_engine(client, core, saas_db):
    from saas.jobs import brand

    a = make_user(saas_db, "brand-a@example.com")
    h = login(client, "brand-a@example.com")
    up(client, h, "/api/brand/logo", "logo.png", png_bytes())
    meme_id = up(client, h, "/api/brand/memes", "lol.gif", gif_bytes()).json()["id"]
    logo, memes = brand.engine_assets(saas_db, a.id, {})
    assert logo and logo.endswith(".png") and len(memes) == 1
    assert brand.engine_assets(saas_db, a.id, {"logo": False, "custom_memes": False}) == (None, [])
    make_user(saas_db, "brand-b@example.com")
    h2 = login(client, "brand-b@example.com")
    assert client.get(f"/api/brand/memes/{meme_id}/file").status_code == 404
    assert client.delete(f"/api/brand/memes/{meme_id}", headers=h2).status_code == 404
    assert client.get("/api/brand/logo/file").status_code == 404
    assert client.get("/api/brand").json() == {**client.get("/api/brand").json(), "logo": None, "memes": []}


def test_unverified_users_cannot_upload_brand_assets(client, core, saas_db):
    make_user(saas_db, "unv-brand@example.com", verified=False)
    h = login(client, "unv-brand@example.com")
    assert up(client, h, "/api/brand/logo", "logo.png", png_bytes()).json()["error"] == "email_unverified"
