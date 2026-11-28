"""Landing direction D, the opening (templates/redesign/d.html, static/js/d/, static/css/redesign-d.css), exported
only as a preview.

What must hold:
- it is a noindex preview that never reaches the live site, the sitemap or a default export;
- the brand comes from config: no literal in the page, its styles or scripts;
- every line of copy is real DOM text, one line per scene, and the film canvas is decorative;
- the loader is the one the owner asked for: a progressbar counting from 000 in fixed digit cells, a 1 px frame of
  four sides, and labels in two corners only;
- a calm version exists: reduced motion and no WebGL 2 get stills from the film, and a page whose script never runs
  is not stuck behind the loader;
- CSP-clean: no inline scripts, handlers or style attributes; nothing loaded from a CDN; only relative imports, and
  no vendored library at all (the opening needs none);
- no yellow in the palette, and the red is the recording red;
- the film's committed cut data agrees with the kept clips, and its media (once shipped) fits the budget and its
  manifest's hashes.
"""
from __future__ import annotations

import colorsys
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "saas" / "web"
JS_D = WEB / "static" / "js" / "d"
CSS_D = WEB / "static" / "css" / "redesign-d.css"
TEMPLATE = WEB / "templates" / "redesign" / "d.html"
MEDIA = WEB / "static" / "media" / "d"
FILM = ROOT / "scripts" / "landing_film" / "film.json"
EDL = ROOT / "scripts" / "showcase" / "edit_decision_list.json"
LINES = ("Long video in.", "We find the moments worth posting.", "Cut on the sentence.")


def _exporter():
    spec = importlib.util.spec_from_file_location("saas_export_static", ROOT / "scripts" / "saas_export_static.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("preview-d") / "public"
    _exporter().export(out, brand="Zebracut", public_url="https://z.pages.dev", support_email="hi@zebracut.test",
                       turnstile="1x00000000000000000000AA", previews=True)
    return out


@pytest.fixture(scope="module")
def html(site):
    return (site / "preview" / "d" / "index.html").read_text(encoding="utf-8")


def _text(html: str) -> str:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


# ---- the page --------------------------------------------------------------------------------------------------

def test_a_noindex_preview_that_stays_out_of_the_live_site(site, html, tmp_path):
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert 'class="page-d"' in html
    for marker in ("{{", "{%", "{#", "Undefined"):
        assert marker not in html, marker
    assert "/preview/" not in (site / "sitemap.xml").read_text(encoding="utf-8")
    plain = tmp_path / "plain"
    _exporter().export(plain, brand="Zebracut", public_url="https://z.pages.dev")
    assert not (plain / "preview").exists()
    assert ("redesign/d.html", "preview/d/index.html", "/preview/d/") in _exporter().PREVIEWS


def test_the_brand_comes_from_config(html):
    assert "Zebracut" in html and "doomscroller" not in html.lower()
    for f in [TEMPLATE, CSS_D, *JS_D.glob("*.js")]:
        assert "doomscroller" not in f.read_text(encoding="utf-8").lower(), f.name
    # the brand is a corner label, and nowhere in the copy
    assert '<p class="d-corner d-corner--tl d-brand">Zebracut</p>' in html


def test_one_line_of_real_text_per_scene(html):
    sections = re.findall(r'<section class="d-scene[^"]*"[^>]*>(.*?)</section>', html, re.S)
    assert len(sections) == 3
    for line, sec in zip(LINES, sections):
        headings = re.findall(r"<h([12])[^>]*>(.*?)</h\1>", sec, re.S)
        assert len(headings) == 1, sec
        assert _text(headings[0][1]) == line
        assert not re.search(r"<p[\s>]", sec), "no paragraph in a scene: one line per screen"
        assert len(line.split()) <= 8
    assert html.count("<h1") == 1 and "Long video in.</span></h1>" in html
    text = _text(html)
    for line in LINES:
        assert line in text
    # nothing else on the screen: no nav, no form, no transcript, no timecode
    main = html[html.index("<main"):html.index("</main>")]
    assert "<nav" not in main and "<form" not in main and "<input" not in main
    assert not re.search(r"\b\d{1,2}:\d{2}(\.\d)?\b", _text(main)), "no timecodes in the opening"


def test_every_section_and_control_is_tracked(html):
    for track in ("open", "moments", "cut"):
        assert f'data-track-section="{track}"' in html
    for track in ("d-footer-privacy", "d-footer-terms", "d-footer-refunds", "d-footer-cookies"):
        assert f'data-track="{track}"' in html


def test_the_loader(html):
    loader = re.search(r'<div class="d-loader"[^>]*>(.*?)</div>\s*<p class="d-corner d-corner--tl', html, re.S)
    assert loader, "the loader, then the brand corner"
    tag = re.search(r'<div class="d-loader"[^>]*>', html).group(0)
    for attr in ('role="progressbar"', 'aria-valuemin="0"', 'aria-valuemax="100"', 'aria-valuenow="0"',
                 'aria-label="Loading footage"'):
        assert attr in tag, attr
    body = loader.group(1)
    assert body.count("<span data-d-digit>0</span>") == 3, "000 in three fixed cells"
    assert all(f'data-side="{s}"' in body for s in "trbl"), "a frame of four sides"
    corners = re.findall(r'class="d-corner d-corner--(\w+)', html)
    assert sorted(corners) == ["br", "tl"], "labels in two corners only"
    assert "Loading footage</p>" in body
    css = CSS_D.read_text(encoding="utf-8")
    assert re.search(r"\.d-loader__count span\s*\{[^}]*width:\s*0\.\d+em", css), "fixed-width digit cells"
    for side, axis in (("t", "X"), ("r", "Y"), ("b", "X"), ("l", "Y")):
        assert re.search(rf'\[data-side="{side}"\][^}}]*transform: scale{axis}\(0\)', css), side
    loader_js = (JS_D / "loader.js").read_text(encoding="utf-8")
    assert "padStart(3, \"0\")" in loader_js and "this.el.hidden = true" in loader_js, "counts 000-100, ends in a cut"
    assert "opacity" not in loader_js and "transition" not in loader_js, "a cut, not a fade"


def test_the_loader_counts_real_bytes():
    main = (JS_D / "main.js").read_text(encoding="utf-8")
    frames = (JS_D / "frames.js").read_text(encoding="utf-8")
    assert "loader.set(n / critical)" in main and "store.packBytes(0)" in main
    assert "getReader()" in frames and "onBytes?.(got)" in frames


def test_there_is_a_calm_version(html):
    css = CSS_D.read_text(encoding="utf-8")
    main = (JS_D / "main.js").read_text(encoding="utf-8")
    assert html.count('<picture class="d-still">') == 3
    for n in (1, 2, 3):
        assert f"/static/media/d/desk-s{n}.jpg" in html and f"/static/media/d/phone-s{n}.jpg" in html
    for alt in re.findall(r'<img [^>]*alt="([^"]*)"', html):
        assert len(alt) > 30, "stills carry real descriptions"
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in main and 'getContext("webgl2")' in main
    assert re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.d-loader\s*\{\s*display: none", css)
    assert "<noscript><style>.d-loader{display:none}.d-still{display:block}</style></noscript>" in html
    assert "html:not([data-d]) .d-loader" in css and "@keyframes d-giveup" in css, "never stuck behind the loader"
    # the film layout only switches on once the script says so
    assert not re.search(r"^\.d-stage\s*\{[^}]*display:\s*block", css, re.M)
    assert 'html[data-d="film"] .d-stage' in css
    stage = re.search(r'<div class="d-stage"[^>]*>', html).group(0)
    assert 'aria-hidden="true"' in stage


def test_csp_clean_and_vendored_only(html):
    scripts = re.findall(r"<script\b([^>]*)>", html)
    assert scripts and all('src="' in s for s in scripts), "no inline scripts"
    srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)
    assert all(s.startswith("/static/") or s.startswith("https://challenges.cloudflare.com/") for s in srcs), srcs
    assert "/static/js/d/main.js?v=" in html
    main = html[html.index("<body"):]
    assert 'style="' not in main, "no style attributes: positions live in CSS"
    assert not re.search(r"\son[a-z]+=", main), "no inline handlers"
    assert "importmap" not in html
    for f in JS_D.glob("*.js"):
        text = f.read_text(encoding="utf-8")
        for spec in re.findall(r"""(?:\bfrom\s*|\bimport\s*\(\s*)["']([^"']+)["']""", text):
            assert spec.startswith("./"), (f.name, spec)
        assert "vendor/" not in text, "the opening uses no vendored library"
        assert not re.search(r"https?://", text), (f.name, "no absolute URLs")
        assert "eval(" not in text and "new Function" not in text


def test_imports_are_versioned_on_export(site):
    main = (site / "static" / "js" / "d" / "main.js").read_text(encoding="utf-8")
    for target in ("./frames.js", "./film.js", "./loader.js", "./story.js"):
        assert re.search(re.escape(target) + r"\?v=[0-9a-f]{10}", main), target


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_script_parses():
    files = sorted(JS_D.glob("*.js"))
    assert {f.name for f in files} == {"main.js", "frames.js", "film.js", "loader.js", "story.js"}
    for f in files:
        run = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert run.returncode == 0, (f.name, run.stderr[-400:])


def test_fonts_are_self_hosted_and_licensed():
    css = CSS_D.read_text(encoding="utf-8")
    for font, licence in (("gloock-latin-400-normal.woff2", "Gloock-OFL.txt"),
                          ("hanken-grotesk-latin-wght-normal.woff2", "HankenGrotesk-OFL.txt")):
        assert f'url("/static/fonts/{font}")' in css
        assert (WEB / "static" / "fonts" / font).is_file()
        assert "SIL Open Font License" in (WEB / "static" / "fonts" / "licenses" / licence).read_text(encoding="utf-8")
    for banned in ("Archivo", "Syne", "Space Grotesk", "Inter\"", "IBM Plex"):
        assert banned not in css, banned


def test_no_yellow_and_one_red():
    css = CSS_D.read_text(encoding="utf-8")
    colours = re.findall(r"#([0-9a-fA-F]{6})\b", css) + [
        "%02x%02x%02x" % tuple(int(x) for x in m) for m in re.findall(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", css)]
    for c in colours:
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
        h, light, sat = colorsys.rgb_to_hls(r, g, b)
        assert not (40 / 360 <= h <= 70 / 360 and sat > 0.45 and light > 0.3), f"yellow #{c}"
    assert "var(--accent" not in css, "none of the site's yellow tokens"
    assert "--d-red: #e8352c" in css
    footer = TEMPLATE.read_text(encoding="utf-8")
    assert "NASA ScienceCasts" in footer and "Event Horizon Telescope Collaboration (CC BY 4.0)" in footer


# ---- the film's inputs and media -------------------------------------------------------------------------------

def test_the_cut_data_matches_the_kept_clips():
    film = json.loads(FILM.read_text(encoding="utf-8"))
    edl = json.loads(EDL.read_text(encoding="utf-8"))["sources"][film["source_id"]]
    assert abs(film["duration"] - edl["expected_duration"]) < 0.05
    bounds = [b[0] for b in film["bounds"]]
    assert bounds == sorted(bounds) and len(bounds) == len(film["sentences"]) - 1
    cuts = [b[0] for b in film["bounds"] if b[1]]
    assert cuts == [c for clip in film["clips"] for c in (clip["start"], clip["end"])]
    assert [c["id"] for c in film["clips"]] == ["clip_01_20_50", "clip_02_64_85"], "the two kept clips only"
    for (s0, e0, _t0), (s1, _e1, _t1), at in zip(film["sentences"], film["sentences"][1:], bounds):
        # every bound sits in the gap between two sentences (Whisper's word times are coarse by a few tenths:
        # "Myth 1." | "All black holes" touch at 21.52 in the transcript, the silence is 21.61-21.89)
        assert e0 - 0.4 <= at <= s1 + 0.4, (at, e0, s1)
    for clip in film["clips"]:
        assert clip["start"] <= clip["face"][0] < clip["face"][1] <= clip["end"]


def _manifest():
    path = MEDIA / "film.json"
    if not path.is_file():
        pytest.skip("the film's media is not committed yet (the preview workflow takes it from a render run)")
    return json.loads(path.read_text(encoding="utf-8"))


def test_shipped_media_matches_its_manifest_and_budget():
    m = _manifest()
    files = [p for p in MEDIA.iterdir() if p.is_file()]
    assert len(files) <= 24, "a few packs, not hundreds of files"
    for name, cam in m["cams"].items():
        assert len(cam["frames"]) == m["frames"]
        for pack in cam["packs"] + [cam["light"]] + cam["stills"]:
            data = (MEDIA / pack["file"]).read_bytes()
            assert hashlib.sha256(data).hexdigest()[:10] == pack["v"], pack["file"]
        critical = cam["packs"][0]["bytes"]
        frames = sum(p["bytes"] for p in cam["packs"])
        assert critical <= 2.5e6, (name, critical)
        assert frames <= 6.0e6, (name, frames)
        assert cam["light"]["bytes"] <= 0.6e6, name
        assert sum(s["bytes"] for s in cam["stills"]) <= 0.6e6, name
        for pack_i, off, length in cam["frames"]:
            assert off + length <= cam["packs"][pack_i]["bytes"]


def test_stills_in_the_page_resolve_when_media_ships(html):
    _manifest()
    for n in (1, 2, 3):
        assert re.search(rf'/static/media/d/desk-s{n}\.jpg\?v=[0-9a-f]{{10}}', html), n
    assert re.search(r'data-d-film="/static/media/d/film\.json\?v=[0-9a-f]{10}"', html)
