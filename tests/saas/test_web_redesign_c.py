"""Landing direction C (templates/redesign/c.html, static/js/c/, static/css/redesign-c.css), exported only as a preview.

What must hold:
- the page is complete without JavaScript: every chapter's words, both waitlist forms, prices and the founder note
  are in the HTML, and the WebGL canvas is decorative (aria-hidden);
- the hooks the tracker and consent rely on are present (data-track-section on every beat, data-track on controls);
- a still version exists for prefers-reduced-motion and for browsers without WebGL 2;
- the page loads no code from a CDN: three.js, GSAP and Lenis are vendored with their licences and notices, and every
  module import is relative (so the exporter versions it);
- every price and per-minute figure comes from docs/saas/pricing.md, and the chart's positions follow its log scale.
"""
from __future__ import annotations

import importlib.util
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "saas" / "web"
JS_C = WEB / "static" / "js" / "c"
VENDOR = WEB / "static" / "vendor"
PRICING = (ROOT / "docs" / "saas" / "pricing.md").read_text(encoding="utf-8")

BEATS = ("hero", "listen", "cut", "frame", "caption", "shorts", "package", "pricing", "founder", "join")


def _exporter():
    spec = importlib.util.spec_from_file_location("saas_export_static", ROOT / "scripts" / "saas_export_static.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("preview-c") / "public"
    _exporter().export(out, brand="Zebracut", public_url="https://z.pages.dev", support_email="hi@zebracut.test",
                       turnstile="1x00000000000000000000AA", previews=True)
    return out


@pytest.fixture(scope="module")
def html(site):
    return (site / "preview" / "c" / "index.html").read_text(encoding="utf-8")


# ---- the page ------------------------------------------------------------------------------------------------

def test_renders_as_a_noindex_preview_and_stays_out_of_the_live_site(site, html, tmp_path):
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert "page-rc" in html and "Zebracut" in html
    for marker in ("{{", "{%", "Undefined", "LAWYER"):
        assert marker not in html, marker
    assert "/preview/" not in (site / "sitemap.xml").read_text(encoding="utf-8")
    plain = tmp_path / "plain"
    _exporter().export(plain, brand="Zebracut", public_url="https://z.pages.dev")
    assert not (plain / "preview").exists()


def test_every_beat_is_a_tracked_section_and_the_controls_are_tracked(html):
    for beat in BEATS:
        assert f'data-track-section="{beat}"' in html, beat
    for track in ("waitlist-submit-hero", "waitlist-submit-end", "rc-sound", "pricing-yearly", "pricing-sources",
                  "founder-email"):
        assert f'data-track="{track}"' in html, track


def test_both_waitlist_forms_post_like_every_other_form(html):
    for form_id in ("hero", "end"):
        form = html[html.index(f'id="wl-{form_id}"'):]
        form = form[:form.index("</form>")]
        assert 'action="/api/waitlist"' in form and "data-waitlist-form" in form
        for field in ('name="email"', 'name="ts"', 'name="website"', 'name="ref"', 'class="cf-turnstile'):
            assert field in form, (form_id, field)
    assert "/static/js/waitlist.js?v=" in html
    assert re.search(r'<p class="rd-count"[^>]*\bhidden\b', html), "the count stays hidden until it is fed"


def test_the_words_are_all_in_the_html(html):
    """Search engines and screen readers get the whole story without the canvas."""
    for text in ("Shorts that end", "It reads every word before it cuts one.",
                 "A cut can only fall between two sentences.", "Then each one is edited, not just trimmed.",
                 "Captions keep time with the voice.", "A 72-minute episode in. Three Shorts out.",
                 "Each one comes back ready to post.", "Nobody tells you the first hundred videos are practice.",
                 "IN 14:02.3", "OUT 14:17.9", "Tue 18:00", "#shorts #podcast #creator", "Sarthak Parulekar",
                 "The first 100 videos are practice", "Views are not the score", "Finish it, then fix the next one"):
        assert text in re.sub(r"<[^>]+>", "", html).replace("\n", " ").replace("  ", " ") or text in html, text
    assert "auto-post" not in html.lower() and "timeline editor" not in html


def test_the_canvas_is_decorative(html):
    stage = re.search(r'<div class="rc-stage"[^>]*>', html).group(0)
    assert 'aria-hidden="true"' in stage
    assert 'data-rc-tiles=' in stage
    assert re.search(r'<div class="rc-hud[^"]*"[^>]*aria-hidden="true"', html)
    sound = re.search(r"<button class=\"rc-sound[^>]*>", html).group(0)
    assert 'aria-pressed="false"' in sound and "hidden" in sound, "sound starts off and appears only with motion"


def test_there_is_a_still_version(html):
    css = (WEB / "static" / "css" / "redesign-c.css").read_text(encoding="utf-8")
    main = (JS_C / "main.js").read_text(encoding="utf-8")
    assert 'class="rc-poster"' in html and html.count("clip__caption") >= 3, "the poster: three drawn Shorts"
    assert "<noscript><style>.rc-poster{display:block}</style></noscript>" in html
    assert "@media (prefers-reduced-motion: reduce)" in css and ".rc-static .rc-poster" in css
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in main and 'getContext("webgl2")' in main
    assert "toStatic" in main and 'reduce.addEventListener' in main
    # reveal styles wait for the story to run, so a failed script never hides a word
    assert re.search(r"\.rc-live \.rc-ch \[data-rc-in\]\s*\{\s*opacity: 0", css)
    assert not re.search(r"^\.rc-ch \[data-rc-in\]", css, re.MULTILINE)


def test_demo_clips_become_video_textures_when_present(tmp_path, monkeypatch):
    from saas.web import redesign

    clips = [{"n": n, "src": f"/static/demo/clip-{n}.mp4?v=abc", "poster": f"/static/demo/clip-{n}.jpg?v=abc"}
             for n in (1, 2, 3)]
    monkeypatch.setattr(redesign, "demo_videos", lambda static_dir, asset: clips)
    out = tmp_path / "public"
    _exporter().export(out, brand="Zebracut", public_url="https://z.pages.dev", previews=True)
    page = (out / "preview" / "c" / "index.html").read_text(encoding="utf-8")
    assert 'data-rc-clips="/static/demo/clip-1.mp4?v=abc|/static/demo/clip-2.mp4?v=abc|/static/demo/clip-3.mp4?v=abc"' in page
    assert "autoplay" not in page


# ---- scripts and vendored libraries -------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_script_parses():
    files = sorted(JS_C.glob("*.js")) + sorted(VENDOR.rglob("*.js"))
    assert {f.name for f in JS_C.glob("*.js")} >= {"main.js", "story.js", "scene.js", "textures.js", "sound.js",
                                                    "pricing.js"}
    for f in files:
        run = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert run.returncode == 0, (f.name, run.stderr[-400:])


def test_nothing_loads_from_a_cdn(html):
    for f in JS_C.glob("*.js"):
        text = f.read_text(encoding="utf-8")
        for spec in re.findall(r"""(?:\bfrom\s*|\bimport\s*\(\s*)["']([^"']+)["']""", text):
            assert spec.startswith("."), (f.name, spec)  # relative, so the exporter versions it
    scripts = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)
    assert all(s.startswith("/static/") or s.startswith("https://challenges.cloudflare.com/") for s in scripts), scripts
    assert "importmap" not in html


def test_vendored_libraries_carry_their_licences():
    three = (VENDOR / "three" / "LICENSE").read_text(encoding="utf-8")
    lenis = (VENDOR / "lenis" / "LICENSE").read_text(encoding="utf-8")
    gsap = (VENDOR / "gsap" / "LICENSE.txt").read_text(encoding="utf-8")
    assert "MIT" in three and "three.js authors" in three.lower()
    assert "MIT" in lenis and "darkroom.engineering" in lenis
    assert "https://gsap.com/standard-license" in gsap and "3.15.1" in gsap
    for f in ("three.core.min.js", "three.module.min.js"):
        assert "@license" in (VENDOR / "three" / f).read_text(encoding="utf-8")[:400], f
    for f in ("gsap-core.js", "CSSPlugin.js", "Observer.js", "ScrollTrigger.js"):
        head = (VENDOR / "gsap" / f).read_text(encoding="utf-8")[:2000]
        assert "Subject to the terms at https://gsap.com/standard-license" in head, f
    assert (VENDOR / "lenis" / "lenis.min.js").read_text(encoding="utf-8").startswith("/*! Lenis 1.3.29 | MIT License")


def test_vendored_modules_export_with_their_licences_and_versioned_imports(site):
    import hashlib

    for rel in ("three/LICENSE", "lenis/LICENSE", "gsap/LICENSE.txt"):
        assert (site / "static" / "vendor" / rel).is_file(), rel
    module = (site / "static" / "vendor" / "three" / "three.module.min.js").read_text(encoding="utf-8")
    digest = hashlib.sha256((site / "static" / "vendor" / "three" / "three.core.min.js").read_bytes()).hexdigest()[:10]
    assert f'from"./three.core.min.js?v={digest}"' in module
    story = (site / "static" / "js" / "c" / "story.js").read_text(encoding="utf-8")
    for target in ("../../vendor/gsap/index.js", "../../vendor/gsap/ScrollTrigger.js",
                   "../../vendor/lenis/lenis.min.js", "./scene.js"):
        assert re.search(re.escape(target) + r"\?v=[0-9a-f]{10}", story), target


# ---- prices ----------------------------------------------------------------------------------------------------

def _set_block(name: str) -> str:
    source = (WEB / "templates" / "redesign" / "c.html").read_text(encoding="utf-8")
    start = source.index("{% set " + name + " = [")
    return source[start:source.index("] %}", start)]


def test_every_price_comes_from_the_pricing_doc(html):
    ours = PRICING[PRICING.index("## Our prices"):PRICING.index("## Competitors")]
    for amount in ("₹99", "₹349", "₹1,499", "₹299", "₹799", "₹2,499", "₹2,990", "₹7,990", "₹24,990"):
        assert amount in ours and amount in html, amount
    for minutes in ("250", "1,000", "5,000", "3,000", "10,000"):
        assert minutes in ours, minutes
    # per-minute figures: the template's exact values, as the doc prints them, shown rounded to paise
    for value in re.findall(r'"per_min(?:_year)?": ([0-9.]+)', _set_block("packs") + _set_block("plans")):
        exact = f"₹{float(value):.3f}"
        assert exact in ours or f"**{exact}**" in ours, exact
        assert f"₹{float(value):.2f}" in html, value
    assert "API and MCP are on every paid plan" in PRICING and "API and MCP on every paid plan" in html


def test_every_competitor_figure_is_in_the_pricing_doc():
    table = PRICING[PRICING.index("## Competitors"):PRICING.index("**How far below")]
    rows = re.findall(r'\{"tool": "([^"]+)", "plan": "([^"]+)", "inr": "([0-9.]+)", "r": (-?\d), "x": ([0-9.]+), '
                      r'"ref": "(C\d+)"', _set_block("rivals"))
    assert len(rows) == 28, "the doc's clipping tools; its two editors are left out"
    for tool, _plan, inr, _row, _x, ref in rows:
        line = next((l for l in table.splitlines() if l.startswith(f"| {tool} ") and f"₹{inr}" in l), None)
        assert line, (tool, inr)
        assert f"[{ref}]" in line, (tool, ref)
    assert "Descript" not in _set_block("rivals") and "Gling" not in _set_block("rivals")


def test_chart_positions_follow_the_log_scale(html):
    def x_of(value: float) -> float:
        return round((math.log(value) - math.log(0.2)) / (math.log(12) - math.log(0.2)) * 100, 2)

    for value, x in re.findall(r'"inr": "([0-9.]+)", "r": -?\d, "x": ([0-9.]+)', _set_block("rivals")):
        assert abs(x_of(float(value)) - float(x)) < 0.05, (value, x)
    for value, x in re.findall(r'"per_min": ([0-9.]+), "x": ([0-9.]+)', _set_block("packs")):
        assert abs(x_of(float(value)) - float(x)) < 0.05, (value, x)
    for value, x in re.findall(r'"per_min": ([0-9.]+), "per_min_year": [0-9.]+, "x": ([0-9.]+)', _set_block("plans")):
        assert abs(x_of(float(value)) - float(x)) < 0.05, (value, x)
    for value, x in re.findall(r'"per_min_year": ([0-9.]+), "x": [0-9.]+, "x_year": ([0-9.]+)', _set_block("plans")):
        assert abs(x_of(float(value)) - float(x)) < 0.05, (value, x)
    assert html.count('class="rc-dot rc-dot--us"') == 9 and html.count('class="rc-dot rc-dot--them"') == 28
    assert 'style="' not in html[html.index('class="rc-strip"'):html.index('class="rc-plans"')], \
        "chart positions are SVG attributes, so a strict style CSP cannot move a dot"


def test_no_unsourced_superlative(html):
    """pricing.md: check Vizard's upper tiers by hand before printing "cheapest on the internet"."""
    assert "cheapest on the internet" not in html.lower()
    assert "0.89" in html and "that is a sale" in html
