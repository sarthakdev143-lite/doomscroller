"""The signed-in product and sign-in pages (saas.web.app_pages).

Pages are shells that fetch their data from the JSON API, so these tests check what the server owns:
who may see a page, what it renders, the hooks and fields the browser code and other agents rely on,
and that none of it leaks into the static export.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

AUTH_PAGES = ["/login", "/signup", "/verify-email", "/forgot", "/reset/sometoken"]
APP_PAGES = {
    "/app": "dashboard", "/app/new": "new", "/app/jobs/job_abc": "dashboard", "/app/clips": "clips",
    "/app/brand": "brand", "/app/billing": "billing", "/app/settings": "settings", "/app/developers": "developers",
}


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.missing_track: list[str] = []
        self.inline_scripts = 0
        self.style_attrs = 0
        self.current_nav: list[str] = []
        self.names: list[str] = []
        self.scripts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if "style" in a:
            self.style_attrs += 1
        if tag in ("a", "button", "summary", "select") and not a.get("data-track"):
            self.missing_track.append(f"<{tag}> {a}")
        if tag in ("section", "article") and not a.get("data-track-section"):
            self.missing_track.append(f"<{tag}> {a}")
        if tag == "script":
            if a.get("src"):
                self.scripts.append(a["src"])
            else:
                self.inline_scripts += 1
        if tag == "a" and a.get("aria-current") == "page":
            self.current_nav.append(a.get("href", ""))
        if tag in ("input", "textarea", "select") and a.get("name"):
            self.names.append(a["name"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def parse(html: str) -> Page:
    p = Page()
    p.feed(html)
    return p


@pytest.fixture()
def signed_in(saas_db):
    """A TestClient whose requests are made as a real user row, through the auth dependency the pages use."""
    from fastapi.testclient import TestClient

    from saas.auth.deps import optional_user, require_user
    from saas.common import utcnow
    from saas.main import create_app
    from saas.models.core import User

    user = User(email="asha@example.com", email_canonical="asha@example.com", name="Asha Rao",
                referral_code="refasha01", email_verified_at=utcnow())
    saas_db.add(user)
    saas_db.commit()
    app = create_app()
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[optional_user] = lambda: user
    with TestClient(app) as c:
        c.user = user
        yield c


# ---- sign-in pages ----------------------------------------------------------------------------------

@pytest.mark.parametrize("path", AUTH_PAGES)
def test_auth_pages_render_clean(client, path):
    r = client.get(path, headers={"Accept": "text/html"})
    assert r.status_code == 200, r.text[:300]
    page = parse(r.text)
    assert page.title.strip()
    assert not page.missing_track, page.missing_track[:3]
    assert page.inline_scripts == 0 and page.style_attrs == 0
    assert any("/static/js/auth.js" in s for s in page.scripts)
    assert '<meta name="robots" content="noindex">' in r.text
    for leak in ("{{", "{%", "Undefined"):
        assert leak not in r.text


@pytest.mark.parametrize("path,form", [("/login", "login"), ("/signup", "signup"), ("/forgot", "forgot")])
def test_identity_forms_carry_the_risk_fields(client, path, form):
    page = parse(client.get(path).text)
    for name in ("fp", "ts", "website"):
        assert name in page.names, (path, name)


def test_login_handles_mfa_token_next_and_notices(client):
    html = client.get("/login?mfa=tok123&next=/app/billing&verified=1").text
    assert 'data-mfa-token="tok123"' in html and 'data-next="/app/billing"' in html
    assert "Email confirmed" in html
    hostile = client.get("/login?next=//evil.example/steal").text
    assert 'data-next="/app"' in hostile, "an off-site next is replaced by the dashboard"
    assert "data-mfa-form" in html and 'name="code"' in html


def test_google_button_only_when_configured(client, monkeypatch):
    assert "/auth/google/start" not in client.get("/login").text
    from saas.auth import google

    monkeypatch.setattr(google, "enabled", lambda: True)
    assert "/auth/google/start" in client.get("/login").text


def test_signup_shows_the_invite_field_only_in_waitlist_mode(client, monkeypatch):
    from saas.templating import templates

    monkeypatch.setitem(templates.env.globals, "launch_mode", "waitlist")
    html = client.get("/signup?invite=ABC123").text
    assert 'name="invite"' in html and 'value="ABC123"' in html
    monkeypatch.setitem(templates.env.globals, "launch_mode", "open")
    assert 'name="invite"' not in client.get("/signup").text


def test_signup_never_claims_a_session(client):
    html = client.get("/signup").text
    assert "Check your inbox" in html  # the post-submit state; the API never signs a new account in


def test_verify_email_invalid_status(client):
    assert "didn’t work" in client.get("/verify-email?status=invalid").text


# ---- the app ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", list(APP_PAGES))
def test_app_pages_need_a_session(client, path):
    r = client.get(path, follow_redirects=False, headers={"Accept": "text/html"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


@pytest.mark.parametrize("path,nav", list(APP_PAGES.items()))
def test_app_pages_render_for_a_signed_in_user(signed_in, path, nav):
    r = signed_in.get(path)
    assert r.status_code == 200, r.text[:300]
    page = parse(r.text)
    assert page.title.strip()
    assert not page.missing_track, page.missing_track[:3]
    assert page.inline_scripts == 0 and page.style_attrs == 0
    assert "asha@example.com" in r.text
    expected = {"dashboard": "/app", "new": "/app/new"}.get(nav, f"/app/{nav}")
    assert expected in page.current_nav, (path, page.current_nav)
    assert '<meta name="robots" content="noindex, nofollow">' in r.text
    assert len(page.scripts) and all("/ts/t.js" in s or "/static/js/" in s for s in page.scripts)
    assert 'data-free-minutes="10"' in r.text and 'data-free-shorts="10"' in r.text


def test_signed_in_visitors_skip_login_and_signup(signed_in):
    assert signed_in.get("/login?next=/app/clips", follow_redirects=False).headers["location"] == "/app/clips"
    assert signed_in.get("/signup", follow_redirects=False).headers["location"] == "/app"


def test_unverified_users_see_the_confirm_banner(signed_in, saas_db):
    signed_in.user.email_verified_at = None
    saas_db.commit()
    assert "data-verify-banner" in signed_in.get("/app").text
    signed_in.user.email_verified_at = __import__("saas.common", fromlist=["utcnow"]).utcnow()
    saas_db.commit()
    assert "data-verify-banner" not in signed_in.get("/app").text


def test_job_page_escapes_the_id(signed_in):
    html = signed_in.get('/app/jobs/"><script>alert(1)</script>').text
    assert "<script>alert(1)</script>" not in html


def test_billing_status_notices(signed_in):
    assert "Payment received" in signed_in.get("/app/billing?status=paid").text
    assert "didn’t go through" in signed_in.get("/app/billing?status=failed").text
    assert "Payment received" not in signed_in.get("/app/billing?status=bogus").text


def test_developers_page_points_at_this_deployment(signed_in):
    from saas.config import settings

    html = signed_in.get("/app/developers").text
    assert f"{settings.public_url.rstrip('/')}/mcp" in html
    assert "dsk_live_YOUR_KEY" in html and "claude mcp add --transport http" in html


def test_new_job_page_offers_every_contract_option(signed_in):
    names = set(parse(signed_in.get("/app/new").text).names)
    for option in ("clips", "max_clip_seconds", "language", "layout", "captions", "broll", "memes", "sfx", "music",
                   "logo", "custom_memes"):
        assert option in names, option


def test_every_app_script_and_stylesheet_exists(signed_in, client):
    seen = set()
    for path in list(APP_PAGES) + AUTH_PAGES:
        html = (signed_in if path.startswith("/app") else client).get(path).text
        seen.update(re.findall(r'(?:src|href)="(/static/[^"?]+)', html))
    for url in sorted(seen):
        assert signed_in.get(url).status_code == 200, url
