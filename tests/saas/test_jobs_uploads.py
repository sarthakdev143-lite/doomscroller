"""Uploads: streamed size cap, extension + magic bytes, ffprobe validation, duration caps, ownership."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from core_fixtures import core, csrf, login, make_user  # noqa: F401

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 700_000  # plausible for 300 s
MKV = b"\x1a\x45\xdf\xa3" + b"\x00" * 700_000
TINY_MP4 = MP4[:8192]


@pytest.fixture()
def probe_ok(monkeypatch):
    """ffprobe stub; tests change `info` to simulate what the file 'contains'."""
    from saas.jobs import probe

    info = {"duration_s": 300.0, "format_name": "mov,mp4,m4a,3gp,3g2,mj2", "width": 1920, "height": 1080,
            "has_video": True, "has_audio": True}
    monkeypatch.setattr(probe, "ffprobe", lambda path: dict(info))
    return info


def _upload(client, h, name="talk.mp4", data=MP4, ctype="video/mp4"):
    return client.post("/api/uploads", files={"file": (name, data, ctype)}, headers=h)


def test_good_upload_and_job_uses_stored_duration(client, core, saas_db, probe_ok):
    from saas.billing import ledger
    from saas.models.jobs import Upload

    u = make_user(saas_db, "up@example.com", credits=20, plan="pro")
    h = login(client, "up@example.com")
    r = _upload(client, h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["duration_s"] == 300.0 and body["credits"] == 5
    up = saas_db.get(Upload, body["upload_id"])
    assert up.path.startswith(f"{u.id}/uploads/") and "talk" not in up.path  # random on-disk name
    r = client.post("/api/jobs", json={"upload_id": body["upload_id"], "options": {"clips": 2}}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["credits_reserved"] == 5
    assert ledger.balance(saas_db, u.id) == 15
    again = client.post("/api/jobs", json={"upload_id": body["upload_id"]}, headers=h)
    assert again.status_code == 409 and again.json()["error"] == "upload_used"


def test_wrong_magic_bytes_or_extension_refused(client, core, saas_db, probe_ok):
    make_user(saas_db, "magic@example.com")
    h = login(client, "magic@example.com")
    r = _upload(client, h, data=b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\nhttp://169.254.169.254/x\n" + b"\x00" * 100)
    assert r.status_code == 415 and r.json()["error"] == "unsupported_type"
    r = _upload(client, h, name="talk.mkv", data=MP4)  # MP4 bytes with an MKV name
    assert r.status_code == 415
    r = _upload(client, h, name="talk.exe", data=MP4)
    assert r.status_code == 415
    r = _upload(client, h, name="talk.webm", data=MKV)
    assert r.status_code == 201


def test_oversize_upload_is_cut_off(client, core, saas_db, probe_ok, monkeypatch):
    from saas.billing import catalog

    make_user(saas_db, "big@example.com")
    h = login(client, "big@example.com")
    feats = catalog.features_for
    monkeypatch.setattr(catalog, "features_for", lambda db, user: {**feats(db, user), "max_upload_mb": 1})
    r = _upload(client, h, data=MP4 + b"\x00" * (2 * 1024 * 1024))
    assert r.status_code == 413 and r.json()["error"] == "upload_too_large"
    from saas.jobs import storage

    leftovers = list((storage.root()).rglob("*.part"))
    assert leftovers == []


def test_duration_caps_and_implausible_media(client, core, saas_db, probe_ok):
    make_user(saas_db, "dur@example.com")
    h = login(client, "dur@example.com")
    probe_ok["duration_s"] = 11 * 60.0  # the free plan allows 10 minutes
    r = _upload(client, h)
    assert r.status_code == 400 and r.json()["error"] == "source_too_long"
    probe_ok["duration_s"] = 9 * 60.0  # an 8 KB file "lasting" 9 minutes: a crafted header
    r = _upload(client, h, data=TINY_MP4)
    assert r.status_code == 415 and r.json()["error"] == "implausible_media"
    probe_ok.update(duration_s=100.0, has_video=False)
    assert _upload(client, h).status_code == 415
    probe_ok.update(has_video=True, width=100000)
    assert _upload(client, h).status_code == 415


def test_upload_needs_multipart_and_file_field(client, core, saas_db, probe_ok):
    make_user(saas_db, "mp@example.com")
    h = login(client, "mp@example.com")
    r = client.post("/api/uploads", content=MP4, headers={**h, "Content-Type": "video/mp4"})
    assert r.status_code == 415 and r.json()["error"] == "multipart_required"
    r = client.post("/api/uploads", files={"other": ("a.mp4", TINY_MP4, "video/mp4")}, headers=h)
    assert r.status_code == 400 and r.json()["error"] == "file_required"


def test_uploads_are_private_to_their_owner(client, core, saas_db, probe_ok):
    make_user(saas_db, "owner@example.com", credits=50)
    h = login(client, "owner@example.com")
    upload_id = _upload(client, h).json()["upload_id"]
    make_user(saas_db, "thief@example.com", credits=50)
    h2 = login(client, "thief@example.com")
    r = client.post("/api/jobs", json={"upload_id": upload_id}, headers=h2)
    assert r.status_code == 404
    assert client.delete(f"/api/uploads/{upload_id}", headers=h2).status_code == 404


def test_unverified_users_cannot_upload(client, core, saas_db, probe_ok):
    make_user(saas_db, "unv-up@example.com", verified=False)
    h = login(client, "unv-up@example.com")
    assert _upload(client, h).json()["error"] == "email_unverified"


@pytest.mark.slow
@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_real_ffprobe_accepts_video_and_refuses_playlists(tmp_path, core):
    from saas.auth.errors import ApiError
    from saas.jobs import probe

    out = tmp_path / "tiny.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True, stdin=subprocess.DEVNULL,
                   timeout=60)
    info = probe.ffprobe(out)
    assert info["has_video"] and 0.9 <= info["duration_s"] <= 1.2 and info["width"] == 160
    playlist = tmp_path / "evil.mp4"
    playlist.write_text("#EXTM3U\n#EXTINF:10,\nhttp://169.254.169.254/latest/meta-data\n")
    with pytest.raises(ApiError):
        probe.ffprobe(playlist)
    concat = tmp_path / "concat.mp4"
    concat.write_text("ffconcat version 1.0\nfile '/etc/passwd'\n")
    with pytest.raises(ApiError):
        probe.ffprobe(concat)
    csrf  # noqa: B018
