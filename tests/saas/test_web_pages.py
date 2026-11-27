"""Customer pages (saas.web): every page renders, carries its tracking hooks, and keeps its promises.

These run on the shared `client` fixture (fresh in-memory DB). Pricing is tested against all three
sources the landing page can read: the live catalog, the seed file, and nothing at all.
"""
from __future__ import annotations

import json
import re
import sys
import types
from html.parser import HTMLParser
from pathlib import Path

import pytest

PAGES = ["/", "/pricing", "/waitlist/thanks", "/privacy", "/terms", "/refunds", "/cookies", "/styleguide"]
WEB = Path(__file__).resolve().parents[2] / "saas" / "web"


class Page(HTMLParser):
    """Collects what the assertions need from one HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.sections: list[dict] = []
        self.clickables: list[tuple[str, dict]] = []
        self.inline_scripts = 0
        self.style_attrs = 0
        self.assets: list[str] = []
        self.text: list[str] = []
        self.ids: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "style" in a:
            self.style_attrs += 1
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "title":
            self._in_title = True
        if tag in ("section", "article"):
            self.sections.append(a)
        if tag in ("a", "button", "summary", "select"):
            self.clickables.append((tag, a))
        if tag == "script":
            self._skip += 1
            if not a.get("src") and a.get("type") != "application/ld+json":
                self.inline_scripts += 1
            elif a.get("src"):
                self.assets.append(a["src"])
        if tag == "style":
            self._skip += 1
        if tag == "link" and a.get("rel") in ("stylesheet", "preload", "icon", "apple-touch-icon", "manifest"):
            self.assets.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style"):
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.text.append(data)


def parse(html: str) -> Page:
    page = Page()
    page.feed(html)
    return page


def visible_text(html: str) -> str:
    return " ".join(" ".join(parse(html).text).split())


@pytest.fixture()
def no_catalog(monkeypatch, tmp_path):
    """Neither the live catalog nor a seed file: the page must hide prices."""
    monkeypatch.setitem(sys.modules, "saas.billing.catalog", None)  # import now raises
    from saas.web import routes

    monkeypatch.setattr(routes, "SEED_CATALOG", tmp_path / "missing.json")


# ---- every page --------------------------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_page_renders_with_title_and_no_template_leaks(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    assert r.headers["content-type"].startswith("text/html")
    page = parse(r.text)
    assert page.title.strip(), "every page needs a <title>"
    assert "Doomscroller" in page.title or "{{" not in page.title
    for leak in ("{{", "{%", "Undefined", "Traceback", "jinja2"):
        assert leak not in r.text, f"{leak!r} leaked into {path}"
    assert len(page.ids) == len(set(page.ids)), "duplicate element ids"


@pytest.mark.parametrize("path", PAGES)
def test_tracking_hooks_on_every_section_and_control(client, path):
    page = parse(client.get(path).text)
    assert page.sections, "pages are built from tracked sections"
    for attrs in page.sections:
        assert attrs.get("data-track-section"), f"section without data-track-section on {path}: {attrs}"
    for tag, attrs in page.clickables:
        assert attrs.get("data-track"), f"<{tag}> without data-track on {path}: {attrs}"


@pytest.mark.parametrize("path", PAGES)
def test_pages_are_csp_safe_and_load_the_tracker(client, path):
    html = client.get(path).text
    page = parse(html)
    assert page.inline_scripts == 0, "inline scripts break the CSP"
    assert page.style_attrs == 0, "inline style attributes break a strict CSP"
    assert re.search(r'<script src="/ts/t\.js(\?v=[0-9a-f]+)?" defer></script>', html)


def test_every_referenced_static_asset_exists(client):
    seen: set[str] = set()
    for path in PAGES:
        for url in parse(client.get(path).text).assets:
            if url.startswith(("/static/", "/favicon.ico", "/site.webmanifest")):
                seen.add(url)
    assert seen
    for url in sorted(seen):
        assert client.get(url).status_code == 200, url


def test_icons_manifest_and_share_image(client):
    assert client.get("/favicon.ico").status_code == 200
    manifest = client.get("/site.webmanifest").json()
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    og = client.get("/og.png")
    assert og.status_code == 200 and og.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "Disallow: /app" in client.get("/robots.txt").text
    assert "<loc>" in client.get("/sitemap.xml").text


# ---- landing -----------------------------------------------------------------------------------

def test_landing_has_both_waitlist_forms_with_the_contract_fields(client):
    html = client.get("/").text
    for form_id in ("wl-hero", "wl-footer"):
        form = html[html.index(f'id="{form_id}"'):]
        form = form[:form.index("</form>")]
        for name in ("email", "fp", "ts", "website", "ref"):
            assert f'name="{name}"' in form, f"{form_id} lacks {name}"
        assert 'tabindex="-1"' in form, "the honeypot stays out of the tab order"
    assert 'name="role"' in html and 'name="channel_url"' in html


def test_landing_carries_a_referral_code_into_the_form(client):
    html = client.get("/?ref=abc123").text
    assert 'name="ref" value="abc123"' in html
    hostile = client.get('/?ref="><script>alert(1)</script>').text
    assert "<script>alert(1)</script>" not in hostile


def test_turnstile_widget_only_when_configured(client, monkeypatch):
    assert "cf-turnstile" not in client.get("/").text
    from saas.templating import templates

    monkeypatch.setitem(templates.env.globals, "turnstile_site_key", "0x4AAAAtest")
    html = client.get("/").text
    assert html.count('data-sitekey="0x4AAAAtest"') == 2
    assert "challenges.cloudflare.com/turnstile/v0/api.js" in html


def test_copy_has_no_banned_marketing_language(client):
    banned = ["unleash", "supercharge", "revolutioni", "effortless", "seamless", "elevate", "in seconds",
              "game-chang", "trusted by", "10,000+", "cutting-edge", "next-level"]
    for path in PAGES:
        text = visible_text(client.get(path).text)
        lower = text.lower()
        for phrase in banned:
            assert phrase not in lower, f"{phrase!r} on {path}"
        assert "!" not in text, f"exclamation mark on {path}"


def test_brand_name_is_never_hardcoded(client, monkeypatch):
    from saas.config import settings
    from saas.templating import templates

    monkeypatch.setitem(templates.env.globals, "brand_name", "Zebracut")
    monkeypatch.setattr(settings, "brand_name", "Zebracut")
    for path in PAGES:
        html = client.get(path).text
        assert "doomscroller" not in html.lower(), f"hardcoded brand on {path}"
        assert "Zebracut" in html
    assert client.get("/site.webmanifest").json()["name"] == "Zebracut"
    for f in list((WEB / "static").rglob("*.css")) + list((WEB / "static").rglob("*.js")) + \
            list((WEB / "static").rglob("*.svg")):
        assert "doomscroller" not in f.read_text(encoding="utf-8").lower(), f
    for f in (WEB / "templates").rglob("*.html"):
        assert "doomscroller" not in f.read_text(encoding="utf-8").lower(), f


def test_share_image_is_drawn_from_the_configured_name():
    from saas.web import routes

    assert routes.og_png("Zebracut") != routes.og_png("Another Name")


# ---- pricing: live catalog, seed file, or nothing ---------------------------------------------

LIVE = {
    "currency": "USD",
    "plans": [
        {"slug": "starter", "name": "Starter", "description": "For a few Shorts a week.", "monthly_credits": 150,
         "rollover_months": 1, "features": {"api": False, "mcp": False},
         "prices": {"month": {"amount_minor": 1500, "currency": "USD", "display": "$15"}, "year": None}},
        {"slug": "studio", "name": "Studio", "description": "", "monthly_credits": 1500, "rollover_months": 1,
         "features": {"api": True, "mcp": True},
         "prices": {"month": {"amount_minor": 9900, "currency": "USD", "display": "$99"}, "year": None}},
        {"slug": "hidden", "name": "Hidden", "monthly_credits": 10, "public": False,
         "prices": {"month": {"amount_minor": 100, "currency": "USD", "display": "$1"}}},
    ],
    "packs": [
        {"slug": "pack-60", "name": "60 minutes", "description": "Never expires.", "credits": 60,
         "price": {"amount_minor": 900, "currency": "USD", "display": "$9"}},
        {"slug": "broken", "name": "Broken", "credits": True, "price": {"amount_minor": 1}},
    ],
}


def _pricing_html(client) -> str:
    html = client.get("/").text
    start = html.index('id="pricing"')
    return html[start:html.index("</section>", start)]


def test_pricing_reads_the_live_catalog(client, monkeypatch):
    fake = types.ModuleType("saas.billing.catalog")
    fake.public_catalog = lambda db, country: LIVE
    monkeypatch.setitem(sys.modules, "saas.billing.catalog", fake)
    section = _pricing_html(client)
    assert "$9" in section and "$15" in section and "$99" in section
    assert "$0.150" in section and "$0.066" in section  # per source minute
    assert "Hidden" not in section and "Broken" not in section
    assert "carry over for 1 month" in section
    assert "announced at launch" not in section
    assert "API and MCP access come with the Studio plan" in client.get("/").text


def test_pricing_falls_back_to_the_seed_file(client, monkeypatch, tmp_path):
    from saas.web import routes

    def broken(db, country):
        raise RuntimeError("billing tables missing")

    fake = types.ModuleType("saas.billing.catalog")
    fake.public_catalog = broken
    monkeypatch.setitem(sys.modules, "saas.billing.catalog", fake)
    seed = {
        "plans": [{"slug": "pro", "name": "Pro", "monthly_credits": 500, "public": True, "published": True}],
        "packs": [{"slug": "pack-300", "name": "300 minutes", "credits": 300, "published": True}],
        "prices": [
            {"item_kind": "plan", "item_slug": "pro", "interval": "month", "currency": "USD", "tier": "default",
             "amount_minor": 3900},
            {"item_kind": "plan", "item_slug": "pro", "interval": "month", "currency": "INR", "tier": "in",
             "amount_minor": 199900},
            {"item_kind": "pack", "item_slug": "pack-300", "interval": "once", "currency": "USD",
             "tier": "default", "amount_minor": 3500},
        ],
    }
    path = tmp_path / "seed_catalog.json"
    path.write_text(json.dumps(seed), encoding="utf-8")
    monkeypatch.setattr(routes, "SEED_CATALOG", path)
    section = _pricing_html(client)
    assert "$39" in section and "$35" in section
    assert "1,999" not in section and "₹" not in section  # other tiers never leak into the default view


def test_seed_fallback_skips_unpublished_prices(client, monkeypatch, tmp_path):
    """Provisional prices in the seed file never reach a page: unpublished (or unmarked) items are skipped."""
    from saas.web import routes

    fake = types.ModuleType("saas.billing.catalog")
    fake.public_catalog = lambda db, country: {"plans": [], "packs": []}  # the live catalog is empty
    monkeypatch.setitem(sys.modules, "saas.billing.catalog", fake)
    seed = {
        "plans": [{"slug": "pro", "name": "Pro", "monthly_credits": 500, "public": True, "published": False},
                  {"slug": "max", "name": "Max", "monthly_credits": 900, "public": True}],
        "packs": [{"slug": "pack-300", "name": "300 minutes", "credits": 300, "published": False}],
        "prices": [
            {"item_kind": "plan", "item_slug": "pro", "interval": "month", "currency": "USD", "tier": "default",
             "amount_minor": 3900},
            {"item_kind": "plan", "item_slug": "max", "interval": "month", "currency": "USD", "tier": "default",
             "amount_minor": 7700},
            {"item_kind": "pack", "item_slug": "pack-300", "interval": "once", "currency": "USD",
             "tier": "default", "amount_minor": 3500},
        ],
    }
    path = tmp_path / "seed_catalog.json"
    path.write_text(json.dumps(seed), encoding="utf-8")
    monkeypatch.setattr(routes, "SEED_CATALOG", path)
    section = _pricing_html(client)
    assert "Prices are announced at launch." in section
    assert "$39" not in section and "$77" not in section and "$35" not in section
    assert routes.normalise_catalog(seed) is None


def test_the_real_seed_file_publishes_no_paid_price(client, monkeypatch):
    """Today every paid plan and pack in saas/billing/seed_catalog.json is unpublished, so the fallback shows none."""
    from saas.web import routes

    raw = json.loads(routes.SEED_CATALOG.read_text(encoding="utf-8"))
    pricing = routes.normalise_catalog(raw)
    shown = [e["slug"] for e in ((pricing or {}).get("plans", []) + (pricing or {}).get("packs", []))]
    published = {i.get("slug") for i in raw.get("plans", []) + raw.get("packs", []) if i.get("published") is True}
    assert set(shown) <= published, "an unpublished item leaked into the fallback"


def test_pricing_hidden_without_any_catalog(client, no_catalog):
    section = _pricing_html(client)
    assert "Prices are announced at launch." in section
    text = visible_text(section)
    assert not re.search(r"[$€£₹]\s?\d", text), "no invented prices"
    # The rounding examples are arithmetic on lengths, so they still show.
    assert "1:12:40" in section and ">73<" in section


def test_normalise_catalog_rejects_junk():
    from saas.web.routes import normalise_catalog

    assert normalise_catalog(None) is None
    assert normalise_catalog({"plans": [], "packs": []}) is None
    assert normalise_catalog({"packs": [{"name": "x", "credits": 10, "price": {"amount_minor": "9"}}]}) is None
    assert normalise_catalog({"packs": [{"name": "x", "credits": 0, "price": {"amount_minor": 900}}]}) is None
    ok = normalise_catalog({"packs": [{"name": "x", "credits": 10, "price": {"amount_minor": 900}}]})
    assert ok["packs"][0]["price_label"] == "$9" and ok["cheapest"]["name"] == "x"


def test_credit_examples_round_up_per_job():
    from saas.web.routes import _credit_examples

    rows = {r["length"]: r["credits"] for r in _credit_examples()}
    assert rows["18:05"] == 19 and rows["1:12:40"] == 73 and rows["1:58:00"] == 118


def test_live_landing_points_the_api_examples_at_this_app(client):
    from saas.config import settings

    html = client.get("/").text
    base = settings.public_url.rstrip("/")
    assert f"curl {base}/api/v1/jobs" in html and f'"{base}/mcp"' in html
    assert "&lt;app-host&gt;" not in html and "at an address we publish then" not in html


def test_live_legal_pages_show_no_draft_markers(client):
    for page in ("/privacy", "/terms", "/refunds", "/cookies"):
        html = client.get(page).text
        for marker in ("LAWYER", "DRAFT", 'class="todo"', "review-note", "{#"):
            assert marker not in html, (page, marker)
        assert "Last updated: " in html


# ---- thanks, styleguide, errors ----------------------------------------------------------------

def test_thanks_page_escapes_the_code(client):
    html = client.get('/waitlist/thanks?code="><img src=x onerror=alert(1)>').text
    assert "<img src=x" not in html
    assert 'data-thanks-state="empty"' in html and 'data-thanks-state="joined"' in html


def test_styleguide_is_not_served_in_production(client, monkeypatch):
    from saas.config import settings

    monkeypatch.setattr(settings, "env", "prod")
    assert client.get("/styleguide").status_code == 404


@pytest.fixture()
def error_client(saas_db):
    from fastapi.testclient import TestClient

    from saas.main import create_app
    from saas.web.errors import install

    app = create_app()
    install(app)

    @app.get("/test-boom")
    def boom():
        raise RuntimeError("boom")

    @app.get("/api/_test_boom")
    def api_boom():
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_browsers_get_html_error_pages(error_client):
    r = error_client.get("/no-such-page", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "isn’t here" in r.text and "<title>" in r.text
    r = error_client.get("/test-boom", headers={"Accept": "text/html"})
    assert r.status_code == 500 and "Something broke on our side" in r.text
    assert "RuntimeError" not in r.text and "Traceback" not in r.text  # never leak the exception


def test_machine_paths_keep_json_errors(error_client):
    r = error_client.get("/api/no-such-thing", headers={"Accept": "text/html"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    r = error_client.get("/api/_test_boom")
    assert r.status_code == 500 and r.json()["error"] == "server_error"


# ---- design tokens -----------------------------------------------------------------------------

def _luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_token_colours_meet_wcag_aa():
    css = (WEB / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
    root = css[css.index(":root {"):]
    tok = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", root))
    text_pairs = [("ink", "paper"), ("ink-2", "paper"), ("ink-3", "paper"), ("ink-3", "surface"),
                  ("danger", "paper"), ("ok", "paper"), ("accent-ink", "accent"), ("paper", "ink"),
                  ("night-text", "night"), ("night-muted", "night"), ("night-muted", "night-2")]
    for fg, bg in text_pairs:
        assert _contrast(tok[fg], tok[bg]) >= 4.5, (fg, bg, round(_contrast(tok[fg], tok[bg]), 2))
    # Non-text UI (field borders, focus rings) needs 3:1.
    assert _contrast(tok["rule-strong"], tok["paper"]) >= 3.0
    assert _contrast(tok["focus"], tok["paper"]) >= 3.0
    assert _contrast(tok["focus-on-dark"], tok["night"]) >= 3.0
