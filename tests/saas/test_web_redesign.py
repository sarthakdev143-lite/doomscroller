"""Landing directions A and B (saas/web/redesign.py, templates/redesign/), exported only as previews.

What must hold: every competitor figure comes from the pricing memo; our own price is never stated; the previews
stay out of the real site; the demo clips are all-or-nothing; the count slot starts hidden.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MEMO = (ROOT / "docs" / "saas" / "costs-and-pricing.md").read_text(encoding="utf-8")


def _exporter():
    spec = importlib.util.spec_from_file_location("saas_export_static", ROOT / "scripts" / "saas_export_static.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def previews(tmp_path_factory):
    exporter = _exporter()
    out = tmp_path_factory.mktemp("previews") / "public"
    exporter.export(out, brand="Zebracut", public_url="https://z.pages.dev", support_email="hi@zebracut.test",
                    previews=True)
    return {d: (out / "preview" / d / "index.html").read_text(encoding="utf-8") for d in ("a", "b")} | {"out": out}


def test_every_figure_in_the_comparison_is_in_the_memo():
    from saas.web import redesign

    memo_section = MEMO[MEMO.index("## 3. Competitors"):MEMO.index("## 4. Cost model")]
    for tool in redesign.COMPARISON:
        assert tool["tool"].split()[0] in memo_section, tool["tool"]
        assert f"[{tool['ref']}]" in MEMO, tool["ref"]
        for plan, figure in tool["rows"]:
            assert figure.lstrip("~") in memo_section, (tool["tool"], figure)
            for amount in re.findall(r"[$₹][\d,.]+", plan):
                assert amount.rstrip(".,") in memo_section, (tool["tool"], amount)
    for ref, (_name, url, _seen) in redesign.SOURCES.items():
        assert url in MEMO, ref


def test_our_price_is_never_stated(previews):
    for d in ("a", "b"):
        html = previews[d]
        assert "Pricing at launch" in html
        # The memo's proposed plans and the "% below" claims stay in the memo.
        for leak in ("$0.010", "$0.0083", "$8.33", "$22.50", "$74.17", "Creator $10", "$0.27", "cheapest", "% below"):
            assert leak not in html, (d, leak)


def test_previews_are_opt_in_and_never_go_to_the_live_folder(tmp_path):
    exporter = _exporter()
    plain = tmp_path / "plain"
    exporter.export(plain, brand="Zebracut", public_url="https://z.pages.dev")
    assert not (plain / "preview").exists()
    assert "/preview/" not in (plain / "sitemap.xml").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        exporter.export(exporter.DEFAULT_OUT, brand="Zebracut", public_url="https://z.pages.dev", previews=True)


def test_preview_pages_are_noindex_and_carry_every_section(previews):
    for d in ("a", "b"):
        html = previews[d]
        assert '<meta name="robots" content="noindex, nofollow">' in html
        for hook in ('id="before-after"', 'id="cost"', 'data-track-section="founder"', "data-waitlist-count",
                     "data-demo-sentence", "data-ba-words", "Sarthak Parulekar"):
            assert hook in html, (d, hook)
        assert "<video" not in html, "no demo files exist, so the drawn frames are used"
        assert re.search(r'<p class="rd-count"[^>]*\bhidden\b', html), "the count is hidden until it is fed"
        assert "/static/js/redesign.js?v=" in html and "/static/css/redesign.css?v=" in html
        for marker in ("{{", "{%", "LAWYER", "Undefined"):
            assert marker not in html, (d, marker)
    assert "page-rd-a" in previews["a"] and "page-rd-b" in previews["b"]


def test_demo_videos_are_all_or_nothing(tmp_path):
    from saas.web import redesign

    def asset(url):
        return url + "?v=x"

    demo = tmp_path / "demo"
    demo.mkdir()
    assert redesign.demo_videos(tmp_path, asset) is None
    for n in (1, 2, 3):
        (demo / f"clip-{n}.mp4").write_bytes(b"x")
    assert redesign.demo_videos(tmp_path, asset) is None, "posters are required too"
    for n in (1, 2):
        (demo / f"clip-{n}.jpg").write_bytes(b"x")
    assert redesign.demo_videos(tmp_path, asset) is None
    (demo / "clip-3.jpg").write_bytes(b"x")
    videos = redesign.demo_videos(tmp_path, asset)
    assert [v["src"] for v in videos] == [f"/static/demo/clip-{n}.mp4?v=x" for n in (1, 2, 3)]
    assert videos[0]["poster"] == "/static/demo/clip-1.jpg?v=x"


def test_real_clips_replace_the_drawn_frames(tmp_path, monkeypatch):
    from saas.web import redesign

    clips = [{"n": n, "src": f"/static/demo/clip-{n}.mp4?v=abc", "poster": f"/static/demo/clip-{n}.jpg?v=abc"}
             for n in (1, 2, 3)]
    monkeypatch.setattr(redesign, "demo_videos", lambda static_dir, asset: clips)
    out = tmp_path / "public"
    _exporter().export(out, brand="Zebracut", public_url="https://z.pages.dev", previews=True)
    html = (out / "preview" / "a" / "index.html").read_text(encoding="utf-8")
    section = html[html.index('id="before-after"'):html.index('id="cost"')]
    assert section.count("<video") == 3 and "data-ba-words" not in section
    assert 'src="/static/demo/clip-2.mp4?v=abc"' in section and 'poster="/static/demo/clip-2.jpg?v=abc"' in section
    assert "autoplay" not in section, "redesign.js starts them, so reduced motion and Pause apply"
    assert "not filmed" not in section


def test_settled_copy_holds(previews):
    for d in ("a", "b"):
        text = previews[d]
        assert "auto-post" not in text.lower() and "timeline editor" not in text
        assert f"10 Shorts or 10 minutes" in text
