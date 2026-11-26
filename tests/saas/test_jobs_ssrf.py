"""SSRF: the source URL validator, DNS answers, redirects off the allowlist, and how yt-dlp is invoked."""
from __future__ import annotations

import json
import subprocess

import pytest

from core_fixtures import core  # noqa: F401

PUBLIC = "142.250.190.78"


@pytest.fixture()
def dns(monkeypatch):
    """Resolver stub: host -> list of addresses (default: one public address)."""
    from saas.jobs import ssrf

    answers: dict[str, list[str]] = {}
    monkeypatch.setattr(ssrf, "resolve_host", lambda host, port: answers.get(host, [PUBLIC]))
    return answers


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://youtube.com/video",
    "javascript:alert(1)",
    "gopher://youtube.com/",
    "http://127.0.0.1/watch",
    "http://[::1]/watch",
    "http://169.254.169.254/latest/meta-data/",
    "http://2130706433/",  # decimal 127.0.0.1
    "https://youtube.com@evil.com/watch?v=x",
    "https://user:pass@youtube.com/watch?v=x",
    "https://evilyoutube.com/watch?v=x",
    "https://youtube.com.evil.com/watch?v=x",
    "https://evil.com/youtube.com/watch?v=x",
    "https://evil.com#youtube.com",
    "https://youtube.com:8080/watch?v=x",
    "https://youtube.com:22/watch?v=x",
    "https://you tube.com/watch",
    "https://youtube.com\\@evil.com/",
    "",
    "x" * 3000,
])
def test_refused_urls(core, dns, url):
    from saas.auth.errors import ApiError
    from saas.jobs import ssrf

    with pytest.raises(ApiError) as exc:
        ssrf.validate_url(url)
    assert exc.value.status == 400


@pytest.mark.parametrize("addrs", [
    ["10.0.0.5"], ["192.168.1.10"], ["172.16.0.1"], ["127.0.0.1"], ["169.254.169.254"], ["100.64.0.10"],
    ["0.0.0.0"], ["::1"], ["fd00:ec2::254"], ["fe80::1"], ["::ffff:127.0.0.1"], ["2002:7f00:0001::1"],
    ["2001:0000:4136:e378:8000:63bf:3fff:fdd2"], ["224.0.0.1"],
    [PUBLIC, "10.0.0.5"],  # rebinding pattern: one public answer, one private
])
def test_private_resolutions_refused(core, dns, addrs):
    from saas.auth.errors import ApiError
    from saas.jobs import ssrf

    dns["www.youtube.com"] = addrs
    with pytest.raises(ApiError):
        ssrf.validate_url("https://www.youtube.com/watch?v=abc")


def test_accepted_urls_are_rebuilt(core, dns):
    from saas.jobs import ssrf

    assert ssrf.validate_url("https://WWW.YouTube.com./watch?v=abc#t=10") == "https://www.youtube.com/watch?v=abc"
    assert ssrf.validate_url("https://youtu.be/abc") == "https://youtu.be/abc"
    assert ssrf.validate_url("http://m.youtube.com:80/watch?v=1") == "http://m.youtube.com/watch?v=1"
    assert ssrf.validate_url("https://vimeo.com/123") == "https://vimeo.com/123"


def test_dns_failure_and_timeout_refuse(core, monkeypatch):
    from saas.auth.errors import ApiError
    from saas.jobs import ssrf

    def fail(host, port):
        raise OSError("nxdomain")

    monkeypatch.setattr(ssrf, "resolve_host", fail)
    with pytest.raises(ApiError) as exc:
        ssrf.validate_url("https://youtube.com/watch?v=1")
    assert exc.value.code == "source_unreachable"


def _fake_run(stdout: dict | None, captured: list, returncode: int = 0, stderr: bytes = b""):
    def run(cmd, timeout):
        captured.append(cmd)
        out = json.dumps(stdout).encode() if stdout is not None else b""
        return subprocess.CompletedProcess(cmd, returncode, out, stderr)
    return run


def test_ytdlp_is_locked_down(core, dns, monkeypatch):
    from saas.jobs import probe

    probe.clear_cache()
    captured: list = []
    monkeypatch.setattr(probe, "_run", _fake_run({"title": "Talk", "duration": 754.2, "webpage_url":
                                                   "https://www.youtube.com/watch?v=abc", "extractor_key": "Youtube",
                                                   "thumbnail": "https://i.ytimg.com/x.jpg"}, captured))
    meta = probe.ytdlp_metadata("https://www.youtube.com/watch?v=abc")
    assert meta["duration_s"] == 754.2 and meta["title"] == "Talk"
    cmd = captured[0]
    assert "--ignore-config" in cmd and "--skip-download" in cmd and "--no-playlist" in cmd
    assert cmd[cmd.index("--use-extractors") + 1] == "default,-generic"
    assert cmd[-2] == "--" and cmd[-1] == "https://www.youtube.com/watch?v=abc"
    probe.ytdlp_metadata("https://www.youtube.com/watch?v=abc")
    assert len(captured) == 1  # cached


@pytest.mark.parametrize("info,code", [
    ({"title": "x", "duration": 60, "webpage_url": "https://evil.example/x"}, "source_not_allowed"),
    ({"_type": "playlist", "entries": []}, "source_playlist"),
    ({"title": "x", "duration": 60, "is_live": True}, "source_live"),
    ({"title": "x"}, "duration_unknown"),
])
def test_ytdlp_results_that_are_refused(core, dns, monkeypatch, info, code):
    from saas.auth.errors import ApiError
    from saas.jobs import probe

    probe.clear_cache()
    monkeypatch.setattr(probe, "_run", _fake_run(info, []))
    with pytest.raises(ApiError) as exc:
        probe.ytdlp_metadata("https://vimeo.com/123")
    assert exc.value.code == code


def test_ytdlp_timeout_is_reported(core, dns, monkeypatch):
    from saas.auth.errors import ApiError
    from saas.jobs import probe

    probe.clear_cache()

    def slow(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(probe, "_run", slow)
    with pytest.raises(ApiError) as exc:
        probe.ytdlp_metadata("https://vimeo.com/999")
    assert exc.value.code == "source_timeout"


def test_estimate_endpoint_reports_refusals_softly(client, core, dns, saas_db):
    from core_fixtures import csrf, login, make_user

    make_user(saas_db, "est@example.com", credits=5)
    h = login(client, "est@example.com")
    r = client.post("/api/jobs/estimate", json={"url": "http://169.254.169.254/latest"}, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["reason"] == "source_not_allowed"
    csrf(client)
