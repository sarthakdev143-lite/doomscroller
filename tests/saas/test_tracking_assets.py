"""The tracker script and vendored rrweb files: valid JS, served with script MIME types (nosniff)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "saas" / "tracking" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_t_js_parses():
    r = subprocess.run(["node", "--check", str(STATIC / "t.js")], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("url", ["/ts/t.js", "/ts/vendor/rrweb/rrweb-record-2.2.2.min.js",
                                 "/ts/vendor/rrweb/rrweb-player-2.2.2.min.js"])
def test_scripts_served_as_javascript(client, url):
    r = client.get(url)
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_css_served_as_css(client):
    for url in ("/ts/t.css", "/ts/vendor/rrweb/rrweb-player-2.2.2.min.css"):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")


def test_vendor_files_carry_their_license():
    lic = (STATIC / "vendor" / "rrweb" / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in lic


def test_vendored_globals_match_what_t_js_calls():
    rec = (STATIC / "vendor" / "rrweb" / "rrweb-record-2.2.2.min.js").read_text(encoding="utf-8")
    player = (STATIC / "vendor" / "rrweb" / "rrweb-player-2.2.2.min.js").read_text(encoding="utf-8")
    tjs = (STATIC / "t.js").read_text(encoding="utf-8")
    assert '"rrwebRecord"' in rec and "exports.record=" in rec and "rrwebRecord.record" in tjs
    assert '"rrwebPlayer"' in player and "exports.Player=" in player


def test_t_js_never_reads_input_values():
    tjs = (STATIC / "t.js").read_text(encoding="utf-8")
    # The only `.value` writes are the fp/ts fills; nothing reads a field's value into an event.
    assert "maskAllInputs: true" in tjs
    for forbidden in ("el.value", "target.value", "b.value", "innerText", ".value +", "value:"):
        assert forbidden not in tjs, forbidden
