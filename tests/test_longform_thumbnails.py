"""Unit tests for app.longform.thumbnails + lazy numpy/cv2 server-import invariant."""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image
import pytest

from app.longform import thumbnails


def _make_jpg(path: Path, color: tuple[int, int, int]) -> str:
    img = Image.new("RGB", (640, 360), color)
    img.save(path, format="JPEG", quality=90)
    return str(path)


def test_idle_server_modules_do_not_eagerly_import_numpy_or_cv2():
    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys; "
        "import app.main, app.review, app.scheduler; "
        "assert 'numpy' not in sys.modules, 'numpy eagerly imported'; "
        "assert 'cv2' not in sys.modules, 'cv2 eagerly imported'; "
        "assert not hasattr(app.review, 'ClipJob'); "
        "assert not hasattr(app.review, 'LongJob'); "
        "print('OK')"
    )
    env = {**os.environ, "DOOMSCROLLER_ENV_FILE": "", "PYTHONPATH": str(root)}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "OK" in proc.stdout


def test_generate_thumbnails_produces_3_distinct_1280x720_jpegs_under_2mb(tmp_path, monkeypatch):
    f1 = _make_jpg(tmp_path / "c1.jpg", (210, 40, 40))
    f2 = _make_jpg(tmp_path / "c2.jpg", (40, 120, 230))
    monkeypatch.setattr(
        thumbnails,
        "extract_candidate_frames",
        lambda vp, ts, od, **k: [
            {"index": 0, "timestamp": 2.0, "path": f1, "sharpness": 120.0, "faces": 1, "score": 620.0},
            {"index": 1, "timestamp": 10.0, "path": f2, "sharpness": 180.0, "faces": 0, "score": 180.0},
        ],
    )
    monkeypatch.setattr(thumbnails.ai, "available", lambda: True)
    monkeypatch.setattr(thumbnails.ai, "ask_json", lambda *a, **k: {"hero": 1, "alt": 0})

    out_dir = tmp_path / "thumbs"
    variants = thumbnails.generate_thumbnails(
        "dummy.mp4",
        out_dir=out_dir,
        title="Alpha vs Beta Craziest Stunts",
        format_kind="vs",
        chapters=[(0.0, "Intro"), (5.0, "Round 1"), (40.0, "Round 2")],
        creator_labels=("@Alpha", "@Beta"),
    )
    assert [v["id"] for v in variants] == ["v1", "v2", "v3"]
    sizes = []
    byte_blobs = []
    for v in variants:
        p = Path(v["path"])
        assert p.is_file()
        assert 0 < p.stat().st_size < 2_000_000
        byte_blobs.append(p.read_bytes())
        with Image.open(p) as im:
            assert im.format == "JPEG"
            assert im.size == (1280, 720)
            sizes.append(im.size)
    assert len(set(byte_blobs)) == 3  # 3 visually distinct variants


def test_save_custom_thumbnail_validates_and_resizes_to_1280x720(tmp_path):
    buf = io.BytesIO()
    Image.new("RGBA", (600, 900), (50, 200, 100, 180)).save(buf, format="PNG")
    dest = tmp_path / "custom.jpg"
    out = thumbnails.save_custom_thumbnail(dest, buf.getvalue())
    assert Path(out).is_file()
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size == (1280, 720)

    with pytest.raises(ValueError):
        thumbnails.save_custom_thumbnail(dest, b"")
    with pytest.raises(ValueError):
        thumbnails.save_custom_thumbnail(dest, b"not an image")


def test_generate_thumbnail_prompt_and_ai_backends(tmp_path, monkeypatch):
    import base64
    from app.config import settings

    hero = _make_jpg(tmp_path / "hero.jpg", (220, 80, 40))
    monkeypatch.setattr(thumbnails.ai, "available", lambda: True)
    monkeypatch.setattr(
        thumbnails.ai,
        "ask_json",
        lambda *a, **k: {"prompt": "Shocked creator close-up with cinematic rim light and bold 3-word yellow text WHY?!"},
    )
    prompt = thumbnails.generate_thumbnail_prompt(hero, title="Crazy Challenge", format_kind="best_of", creator_labels=("@Rug",))
    assert "Shocked creator" in prompt

    png_buf = io.BytesIO()
    Image.new("RGB", (1536, 1024), (20, 140, 240)).save(png_buf, format="PNG")
    b64_img = base64.b64encode(png_buf.getvalue()).decode("ascii")

    class FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
            self.text = str(body)

        def json(self):
            return self._body

    # 1. ChatGPT gpt-image-1 path
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-secret")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    seen = {}

    def fake_openai_post(url, **kw):
        seen["url"] = url
        seen["auth"] = kw["headers"]["Authorization"]
        return FakeResp(200, {"data": [{"b64_json": b64_img}]})

    monkeypatch.setattr(thumbnails.httpx, "post", fake_openai_post)
    res_oai = thumbnails.generate_ai_thumbnail(hero, prompt, tmp_path / "v4_oai.jpg")
    assert res_oai is not None and res_oai[0] == "ChatGPT AI"
    assert seen["auth"] == "Bearer sk-test-secret"
    with Image.open(res_oai[1]) as im:
        assert im.size == (1280, 720) and im.format == "JPEG"

    # 2. Gemini gemini-2.5-flash-image fallback path (x-goog-api-key header, never URL param)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-test-key")
    seen_gem = {}

    def fake_gem_post(url, **kw):
        seen_gem["url"] = url
        seen_gem["headers"] = kw["headers"]
        return FakeResp(200, {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": b64_img}}]}}]})

    monkeypatch.setattr(thumbnails.httpx, "post", fake_gem_post)
    res_gem = thumbnails.generate_ai_thumbnail(hero, prompt, tmp_path / "v4_gem.jpg")
    assert res_gem is not None and res_gem[0] == "Gemini AI"
    assert "AIza-test-key" not in seen_gem["url"]
    assert seen_gem["headers"]["x-goog-api-key"] == "AIza-test-key"
    with Image.open(res_gem[1]) as im:
        assert im.size == (1280, 720) and im.format == "JPEG"
