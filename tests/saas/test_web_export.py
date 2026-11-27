"""The static export (scripts/saas_export_static.py): the marketing site as plain files for Cloudflare Pages.

What it must guarantee: no request-time dependencies (tracker, fingerprint, CSRF, database prices), analytics
only when configured and only behind consent, assets that resolve, and re-exports that never delete files
someone else put in the folder.
"""
from __future__ import annotations

import importlib.util
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _exporter():
    spec = importlib.util.spec_from_file_location("saas_export_static", ROOT / "scripts" / "saas_export_static.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Hooks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.missing: list[str] = []
        self.urls: list[str] = []
        self.text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("section", "article") and not a.get("data-track-section"):
            self.missing.append(f"<{tag}> {a}")
        if tag in ("a", "button", "summary", "select") and not a.get("data-track"):
            self.missing.append(f"<{tag}> {a}")
        for key in ("src", "href"):
            if a.get(key, "").startswith("/static/"):
                self.urls.append(a[key])
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)


@pytest.fixture()
def site(tmp_path):
    exporter = _exporter()
    out = tmp_path / "public"
    exporter.export(out, brand="Zebracut", public_url="https://zebracut.pages.dev/", support_email="hi@zebracut.test")
    return exporter, out


def test_every_page_and_extra_is_written(site):
    exporter, out = site
    for _template, file, _path in exporter.PAGES:
        assert (out / file).is_file(), file
    for extra in ("favicon.ico", "og.png", "site.webmanifest", "robots.txt", "sitemap.xml", "_headers"):
        assert (out / extra).is_file(), extra
    assert json.loads((out / "site.webmanifest").read_text(encoding="utf-8"))["name"] == "Zebracut"
    assert "https://zebracut.pages.dev/pricing/" in (out / "sitemap.xml").read_text(encoding="utf-8")
    assert not list(out.rglob("*.md")), "notes for developers are not published"
    assert not (out / "styleguide").exists() and not (out / "app").exists()


def test_app_assets_ship_but_app_pages_never_do(site):
    """The beta serves app pages through the site, so their CSS and JS are published; the pages themselves are not."""
    exporter, out = site
    for page in ("login", "signup", "forgot", "verify-email", "reset", "app"):
        assert not (out / page).exists(), page
    shipped = {p.relative_to(out / "static").as_posix() for p in (out / "static").rglob("*") if p.is_file()}
    source = ROOT / "saas" / "web" / "static"
    app_assets = {p.relative_to(source).as_posix() for p in source.rglob("*")
                  if p.is_file() and exporter.app_only(p.relative_to(source).as_posix())}
    assert {"css/app.css", "js/api.js", "js/auth.js", "js/app-shell.js", "js/app-job.js"} <= app_assets
    assert app_assets <= shipped, sorted(app_assets - shipped)
    assert "js/site.js" in shipped and "css/components.css" in shipped
    for html in out.rglob("*.html"):
        text = html.read_text(encoding="utf-8")
        assert 'href="/login"' not in text and "/static/js/auth.js" not in text, html.name


def test_every_relative_import_is_versioned(site):
    """/static/* is cached as immutable, so an unversioned import would pin a module's old bytes for a year.
    Covers `from "./x.js"`, bare `import "./x.js"` (app-brand.js) and `import("./x.js")`."""
    import hashlib

    _exporter_module, out = site
    spec = re.compile(r"""\b(?:from|import)\s*\(?\s*["']((?:\.\.?/)+[\w./-]+\.js)(\?v=([0-9a-f]+))?["']""")
    seen_bare = False
    for js in (out / "static" / "js").rglob("*.js"):  # subfolders too: editor/ imports "../api.js"
        text = "\n".join(line for line in js.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith(("//", "*", "/*")))  # code only
        seen_bare |= bool(re.search(r"""^import\s+["']\.""", text, re.MULTILINE))
        for target, version, digest in spec.findall(text):
            assert version, f"{js.relative_to(out)} imports {target} without ?v="
            published = (js.parent / target).resolve().read_bytes()
            assert hashlib.sha256(published).hexdigest()[:10] == digest, (js.name, target)
    assert seen_bare, "the bare-import case is exercised"


def test_parent_path_imports_are_versioned(tmp_path, monkeypatch):
    """A module in a subfolder importing "../x.js" gets the same ?v= the page uses for x.js."""
    import hashlib
    import shutil

    exporter = _exporter()
    static = tmp_path / "static"
    (static / "js" / "sub").mkdir(parents=True)
    (static / "js" / "base.js").write_text("export const a = 1;\n", encoding="utf-8")
    (static / "js" / "sub" / "deep.js").write_text('import { a } from "../base.js";\nimport "../base.js";\n',
                                                     encoding="utf-8")
    out = tmp_path / "out"
    exporter._copy_static(static, out / "static")
    deep = (out / "static" / "js" / "sub" / "deep.js").read_text(encoding="utf-8")
    digest = hashlib.sha256((out / "static" / "js" / "base.js").read_bytes()).hexdigest()[:10]
    assert deep.count(f'"../base.js?v={digest}"') == 2, deep
    shutil.rmtree(out)


def test_pages_have_no_request_time_dependencies(site):
    _exporter_module, out = site
    for page in out.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert "/ts/t.js" not in html, page
        assert 'name="fp"' not in html, page
        assert "{{" not in html and "{%" not in html, page
        assert "doomscroller" not in html.lower(), f"hardcoded brand in {page}"
        text = " ".join(" ".join(_parse(html).text).split())
        assert not re.search(r"[$€£₹]\s?\d", text), f"a price on {page}"
    landing = (out / "index.html").read_text(encoding="utf-8")
    assert "Prices are announced at launch." in landing
    assert '<link rel="canonical" href="https://zebracut.pages.dev/">' in landing
    assert '<link rel="canonical" href="https://zebracut.pages.dev/pricing/">' in \
        (out / "pricing" / "index.html").read_text(encoding="utf-8")
    assert "hi@zebracut.test" in landing


def _parse(html: str) -> Hooks:
    hooks = Hooks()
    hooks.feed(html)
    return hooks


def test_tracking_hooks_and_assets_resolve(site):
    _exporter_module, out = site
    for page in out.rglob("*.html"):
        hooks = _parse(page.read_text(encoding="utf-8"))
        assert not hooks.missing, (page, hooks.missing[:3])
        for url in hooks.urls:
            assert (out / url.split("?")[0].lstrip("/")).is_file(), (page, url)


def test_js_modules_import_the_same_hashed_url_the_page_loads(site):
    _exporter_module, out = site
    page_url = re.search(r'src="(/static/js/site\.js\?v=[0-9a-f]+)"', (out / "index.html").read_text(encoding="utf-8"))
    assert page_url
    version = page_url.group(1).split("?")[1]
    for name in ("landing.js", "waitlist.js", "thanks.js"):
        js = (out / "static" / "js" / name).read_text(encoding="utf-8")
        assert f'from "./site.js?{version}"' in js, name
    # Every ?v= is the hash of the published bytes, so a rewritten import line busts the immutable cache.
    import hashlib

    for url in re.findall(r'"(/static/[^"?]+)\?v=([0-9a-f]+)"', (out / "index.html").read_text(encoding="utf-8")):
        path, digest = url
        assert hashlib.sha256((out / path.lstrip("/")).read_bytes()).hexdigest()[:10] == digest, path


def test_no_analytics_or_turnstile_unless_configured(site):
    _exporter_module, out = site
    landing = (out / "index.html").read_text(encoding="utf-8")
    for absent in ("ds:ga4", "ds:clarity", "consent.js", "data-consent ", "cf-turnstile", "googletagmanager"):
        assert absent not in landing, absent



def test_headers_leave_csp_to_the_middleware_and_routes_skip_functions_for_files(site):
    _exporter_module, out = site
    headers = (out / "_headers").read_text(encoding="utf-8")
    # functions/_middleware.js sends the CSP with a nonce; a second policy here would be enforced too.
    assert "Content-Security-Policy" not in headers
    assert "X-Content-Type-Options: nosniff" in headers and "immutable" in headers
    routes = json.loads((out / "_routes.json").read_text(encoding="utf-8"))
    assert routes["version"] == 1 and routes["include"] == ["/*"]
    assert "/static/*" in routes["exclude"]
    root_files = {f"/{f.name}" for f in out.iterdir() if f.is_file()} - {"/_headers", "/_routes.json"}
    non_html = {name for name in root_files if not name.endswith(".html")}
    assert non_html <= set(routes["exclude"]), non_html - set(routes["exclude"])
    assert not any(rule.endswith(".html") or rule in ("/", "/*") for rule in routes["exclude"]), \
        "pages must reach the middleware"
    assert len(routes["exclude"]) <= 100 and all(len(rule) <= 100 for rule in routes["exclude"])


def test_analytics_load_behind_the_consent_banner(tmp_path):
    exporter = _exporter()
    out = tmp_path / "public"
    exporter.export(out, brand="Zebracut", public_url="https://zebracut.pages.dev", ga4="G-ABC123XYZ",
                    clarity="k1x2y3z4w5", turnstile="0x4AAAAAAAtestkey")
    landing = (out / "index.html").read_text(encoding="utf-8")
    assert '<meta name="ds:ga4" content="G-ABC123XYZ">' in landing
    assert '<meta name="ds:clarity" content="k1x2y3z4w5">' in landing
    assert re.search(r'<script type="module" src="/static/js/consent\.js\?v=[0-9a-f]+"></script>', landing)
    assert 'data-consent-choice="denied"' in landing and 'data-consent-choice="granted"' in landing
    assert "googletagmanager.com/gtag" not in landing, "the tag itself is only injected after Accept"
    assert landing.count('data-sitekey="0x4AAAAAAAtestkey"') == 2
    consent = (out / "static" / "js" / "consent.js").read_text(encoding="utf-8")
    assert 'analytics_storage: "denied"' in consent and 'window.gtag("consent", "default"' in consent


@pytest.mark.parametrize("field,value", [("ga4", "UA-12345-1"), ("clarity", "x"), ("turnstile", "<script>")])
def test_bad_ids_are_refused(tmp_path, field, value):
    exporter = _exporter()
    with pytest.raises(ValueError):
        exporter.export(tmp_path / "out", brand="Zebracut", public_url="https://z.pages.dev", **{field: value})


def test_reexport_removes_only_its_own_files_and_restores_globals(tmp_path):
    exporter = _exporter()
    from saas.templating import templates

    before = dict(templates.env.globals)
    out = tmp_path / "public"
    exporter.export(out, brand="Zebracut", public_url="https://z.pages.dev")
    (out / "_redirects").write_text("/old /new 301\n", encoding="utf-8")  # someone else's file
    stale = out / "old-page" / "index.html"
    stale.parent.mkdir()
    stale.write_text("old", encoding="utf-8")
    manifest = out / exporter.MANIFEST
    manifest.write_text(json.dumps(json.loads(manifest.read_text(encoding="utf-8")) + ["old-page/index.html"]),
                        encoding="utf-8")
    exporter.export(out, brand="Zebracut", public_url="https://z.pages.dev")
    assert (out / "_redirects").is_file()
    assert not stale.exists() and not stale.parent.exists()
    for key in ("brand_name", "public_url", "turnstile_site_key", "support_email", "launch_mode"):
        assert templates.env.globals.get(key) == before.get(key), key


def test_cli_reads_the_environment(tmp_path, monkeypatch, capsys):
    exporter = _exporter()
    monkeypatch.setenv("SAAS_SUPPORT_EMAIL", "help@zebracut.test")
    monkeypatch.setenv("SAAS_BRAND_NAME", "doomscroller")
    monkeypatch.delenv("SAAS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("GA4_MEASUREMENT_ID", "G-ENV1234")
    monkeypatch.delenv("CLARITY_PROJECT_ID", raising=False)
    assert exporter.main(["--out", str(tmp_path / "site")]) == 0
    landing = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "https://doomscroller.pages.dev/" in landing and 'content="G-ENV1234"' in landing
    assert "ds:clarity" not in landing
    monkeypatch.setenv("GA4_MEASUREMENT_ID", "nonsense")
    assert exporter.main(["--out", str(tmp_path / "site")]) == 2


@pytest.mark.parametrize("address", ["", "support@example.com", "a@EXAMPLE.org", "x@foo.example"])
def test_cli_refuses_a_placeholder_support_address(tmp_path, monkeypatch, capsys, address):
    exporter = _exporter()
    monkeypatch.setenv("SAAS_SUPPORT_EMAIL", address)
    for name in ("GA4_MEASUREMENT_ID", "CLARITY_PROJECT_ID", "SAAS_TURNSTILE_SITE_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert exporter.main(["--out", str(tmp_path / "site")]) == 2
    assert "SAAS_SUPPORT_EMAIL" in capsys.readouterr().err
    assert not (tmp_path / "site").exists(), "nothing is written when the build is refused"
    assert exporter.main(["--out", str(tmp_path / "site"), "--allow-placeholder"]) == 0
    assert "do not ship this build" in capsys.readouterr().err


def test_public_pages_carry_no_draft_markers(site):
    _exporter_module, out = site
    for html in out.rglob("*.html"):
        text = html.read_text(encoding="utf-8")
        for marker in ("LAWYER", "DRAFT", "[confirm", "[number", "[owner", 'class="todo"', "review-note", "{#", "#}"):
            assert marker not in text, (html.relative_to(out).as_posix(), marker)
    source = (ROOT / "saas" / "web" / "templates" / "legal" / "privacy.html").read_text(encoding="utf-8")
    assert "{# LAWYER REVIEW:" in source, "the review notes stay in the source as comments"
    todo = (ROOT / "docs" / "saas" / "LEGAL_TODO.md").read_text(encoding="utf-8")
    assert "Privacy, 1. Who we are" in todo and "Terms, 12. Law and disputes" in todo


def test_legal_pages_name_the_operator_and_date(tmp_path, monkeypatch):
    from saas.config import settings

    exporter = _exporter()
    monkeypatch.setattr(settings, "operator_name", "Asha Rao")
    monkeypatch.setattr(settings, "operator_country", "India")
    monkeypatch.setattr(settings, "legal_effective_date", "1 December 2026")
    out = tmp_path / "public"
    exporter.export(out, brand="Zebracut", public_url="https://z.pages.dev", support_email="hi@zebracut.test")
    privacy = (out / "privacy" / "index.html").read_text(encoding="utf-8")
    terms = (out / "terms" / "index.html").read_text(encoding="utf-8")
    assert "The controller of your personal data is Asha Rao, an individual based in India." in privacy
    assert "between you and Asha Rao, an individual based in India" in terms
    for page in ("privacy", "terms", "refunds", "cookies"):
        assert "Last updated: 1 December 2026" in (out / page / "index.html").read_text(encoding="utf-8"), page


def test_legal_date_defaults_to_the_export_day(monkeypatch):
    import datetime

    from saas.config import settings
    from saas.web import routes

    monkeypatch.setattr(settings, "legal_effective_date", "")
    assert routes.legal_date(datetime.date(2026, 11, 7)) == "7 November 2026"


def test_api_examples_never_point_at_the_static_host(site, monkeypatch, tmp_path):
    exporter, out = site
    landing = (out / "index.html").read_text(encoding="utf-8")
    assert "https://zebracut.pages.dev/api" not in landing and "https://zebracut.pages.dev/mcp" not in landing
    assert "curl https://&lt;app-host&gt;/api/v1/jobs" in landing and "https://&lt;app-host&gt;/mcp" in landing
    assert "at an address we publish then" in landing
    from saas.config import settings

    monkeypatch.setattr(settings, "api_base_url", "https://app.zebracut.test/api/v1/")
    exporter.export(tmp_path / "named", brand="Zebracut", public_url="https://z.pages.dev")
    named = (tmp_path / "named" / "index.html").read_text(encoding="utf-8")
    assert "curl https://app.zebracut.test/api/v1/jobs" in named and '"https://app.zebracut.test/mcp"' in named
    assert "&lt;app-host&gt;" not in named


def test_editing_and_broll_copy_stays_honest(site):
    _exporter_module, out = site
    landing = (out / "index.html").read_text(encoding="utf-8")
    pricing = (out / "pricing" / "index.html").read_text(encoding="utf-8")
    assert "there is no timeline editor" in landing and "no timeline to trim or re-cut" in landing
    assert "You edit in the browser" not in landing and "Adjust each clip" not in landing
    assert "Editing in the browser" not in pricing
    assert "Often a clip has none" in landing and "often none" in pricing
