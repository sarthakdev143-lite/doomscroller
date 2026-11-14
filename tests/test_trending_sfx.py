"""Trending SFX downloads (Freesound) into the local pack dir.

Only Creative Commons 0 sounds are kept, so clips need no attribution and the
runtime `sfx` pack picks them up with no code change. Everything is optional:
no key, no results, a rate limit or a failed download all just mean fewer new
pack files, because the synth still plays.
"""
from __future__ import annotations

import pytest

from app.clipper import trending_sfx
from app.config import settings


def _search_response():
    return {
        "count": 3,
        "next": None,
        "previous": None,
        "results": [
            {"id": 11, "name": "Air horn blast", "username": "bob",
             "license": "Creative Commons 0",
             "previews": {"preview-hq-mp3": "https://freesound.org/a.mp3"},
             "url": "https://freesound.org/people/bob/sounds/11/"},
            {"id": 22, "name": "Sad trombone", "username": "ann",
             "license": "Attribution",
             "previews": {"preview-hq-ogg": "https://freesound.org/b.ogg"},
             "url": "https://freesound.org/people/ann/sounds/22/"},
            {"id": 33, "name": "Pop click", "username": "cid",
             "license": "Creative Commons 0",
             "previews": {},
             "url": "https://freesound.org/people/cid/sounds/33/"},
        ],
    }


def test_provider_none_without_key(monkeypatch):
    monkeypatch.setattr(settings, "freesound_api_key", "")
    monkeypatch.setattr(settings, "clip_trending_sfx", True)
    assert trending_sfx.provider() is None
    assert trending_sfx.available() is False


def test_provider_with_key(monkeypatch):
    monkeypatch.setattr(settings, "freesound_api_key", "k")
    monkeypatch.setattr(settings, "clip_trending_sfx", True)
    assert trending_sfx.provider() == trending_sfx.FREESOUND
    assert trending_sfx.available() is True
    monkeypatch.setattr(settings, "clip_trending_sfx", False)
    assert trending_sfx.available() is False


def test_trending_sends_token_header_and_download_sort(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "freesound_api_key", "k")
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return _search_response()

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["headers"] = headers
        return _Resp()

    monkeypatch.setattr(trending_sfx.httpx, "get", fake_get)
    found = trending_sfx.trending(limit=3)
    assert seen["url"] == "https://freesound.org/apiv2/search/"
    assert seen["headers"] == {"Authorization": "Token k"}
    assert all(v != "k" for v in seen["params"].values())  # key rides the header, never the URL
    assert seen["params"]["sort"] == "downloads_desc"
    assert [c.sfx_id for c in found] == ["freesound-11"]  # Attribution + previewless dropped
    assert found[0].link == "https://freesound.org/a.mp3"
    assert found[0].license == "Creative Commons 0"


def test_cc0_preferred_and_previewless_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "freesound_api_key", "k")
    monkeypatch.setattr(trending_sfx, "_request", lambda url, params: _search_response())
    found = trending_sfx.search("horn")
    assert [c.sfx_id for c in found] == ["freesound-11"]  # Attribution + previewless dropped


def test_rate_limited_and_bad_key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "freesound_api_key", "k")

    class _Resp:
        def __init__(self, status):
            self.status_code = status

        def json(self):
            return {}

    monkeypatch.setattr(trending_sfx.httpx, "get", lambda *a, **k: _Resp(429))
    with pytest.raises(trending_sfx.RateLimited):
        trending_sfx.trending()
    monkeypatch.setattr(trending_sfx.httpx, "get", lambda *a, **k: _Resp(401))
    with pytest.raises(trending_sfx.BadKey):
        trending_sfx.search("x")
    monkeypatch.setattr(trending_sfx.httpx, "get", lambda *a, **k: _Resp(400))
    with pytest.raises(trending_sfx.SfxError):
        trending_sfx.search("x")


def test_scrub_secrets_redacts_key(monkeypatch):
    monkeypatch.setattr(settings, "freesound_api_key", "fs-secret-9")
    assert "fs-secret-9" not in trending_sfx.scrub_secrets("Token fs-secret-9 failed")
