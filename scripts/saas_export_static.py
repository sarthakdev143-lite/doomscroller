"""Render the marketing site to static files for Cloudflare Pages (edge/public/).

    .venv-saas/Scripts/python.exe scripts/saas_export_static.py [--out edge/public] [--allow-placeholder]

Python never runs on Pages: these files are the whole site. The waitlist form posts JSON to
/api/waitlist and the thanks page reads /api/waitlist/me, both Cloudflare Pages Functions in edge/functions.

Environment. An unset analytics or Turnstile ID means that snippet is not in the pages at all.
  SAAS_SUPPORT_EMAIL       REQUIRED: the contact address on every page. Unset or @example.com fails the export,
                           unless --allow-placeholder is passed (a preview build, never a launch)
  SAAS_BRAND_NAME          default "doomscroller" (shown exactly as written)
  SAAS_PUBLIC_URL          default https://doomscroller.pages.dev (canonical URLs, share image, sitemap)
  SAAS_API_BASE_URL        the app's API base for the code examples; unset shows https://<app-host>/api/v1
  SAAS_OPERATOR_NAME       who runs the service, on the legal pages (default "the operator of <brand name>")
  SAAS_OPERATOR_COUNTRY    default "India"
  SAAS_LEGAL_EFFECTIVE_DATE  e.g. "27 November 2026"; unset uses the export date
  GA4_MEASUREMENT_ID       G-XXXXXXXXXX; Google Analytics 4, loaded only after the visitor accepts
  CLARITY_PROJECT_ID       Microsoft Clarity project id, loaded only after the visitor accepts
  SAAS_TURNSTILE_SITE_KEY  Cloudflare Turnstile widget on the waitlist forms
The owner's .env and .env.saas are not read: the build depends on these variables only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "edge" / "public"
DEFAULT_PUBLIC_URL = "https://doomscroller.pages.dev"
MANIFEST = ".static-export.json"  # what the last export wrote, so a re-export never deletes anyone else's files

# (template, file, URL path). Directories with index.html give /pricing/ and friends on Pages without rewrites.
PAGES = [
    ("landing.html", "index.html", "/"),
    ("pricing.html", "pricing/index.html", "/pricing/"),
    ("waitlist_thanks.html", "waitlist/thanks/index.html", "/waitlist/thanks/"),
    ("legal/privacy.html", "privacy/index.html", "/privacy/"),
    ("legal/terms.html", "terms/index.html", "/terms/"),
    ("legal/refunds.html", "refunds/index.html", "/refunds/"),
    ("legal/cookies.html", "cookies/index.html", "/cookies/"),
    ("errors/404.html", "404.html", "/404"),  # Pages serves it for every unknown path
]
SITEMAP = ["/", "/pricing/", "/privacy/", "/terms/", "/refunds/", "/cookies/"]
# Landing directions for the owner to choose between (saas/web/redesign.py). Only with --previews, never into the
# default output folder, never in the sitemap; each page carries noindex.
PREVIEWS = [
    ("redesign/a.html", "preview/a/index.html", "/preview/a/"),
    ("redesign/b.html", "preview/b/index.html", "/preview/b/"),
    ("redesign/c.html", "preview/c/index.html", "/preview/c/"),
]

GA4_ID = re.compile(r"^G-[A-Z0-9]{4,20}$")
CLARITY_ID = re.compile(r"^[a-z0-9]{6,20}$", re.IGNORECASE)
TURNSTILE_KEY = re.compile(r"^[0-9A-Za-z_-]{10,100}$")
# A relative module specifier in any import form: `from "./x.js"`, a bare `import "./x.js"` statement and
# `import("./x.js")`. The bare form must start its line: site.js mentions `import "./site.js"` in a comment, and
# rewriting that would make the module's hash depend on itself, so the rewrite never settles.
# Parent paths count too: static/js/editor/*.js import "../api.js", which must carry the same ?v= as the page's
# own api.js or the browser loads a second, unversioned copy that the immutable cache then pins.
JS_IMPORT = re.compile(
    r"""((?:\bfrom\s*|^[ \t]*import\s*|\bimport\s*\(\s*)["'])((?:\.\.?/)+(?:[\w.-]+/)*[\w.-]+\.js)(\?v=[0-9a-f]+)?(["'])""",
    re.MULTILINE)
# Assets only the signed-in app and the sign-in pages load (paths relative to saas/web/static). They ARE published
# with the site, because the beta serves app pages through it; no app or sign-in PAGE is ever exported.
APP_ONLY = re.compile(r"^(css/app\.css|js/api\.js|js/auth\.js|js/app-[\w-]+\.js)$")


def app_only(rel: str) -> bool:
    return bool(APP_ONLY.match(rel))


def is_placeholder_email(address: str) -> bool:
    """Unset, or a reserved example domain (RFC 2606), which nobody can write to."""
    domain = address.strip().lower().rpartition("@")[2]
    return not domain or domain in ("example.com", "example.org", "example.net") or domain.endswith(".example")


def _saas():
    """Import the shared templates without touching a database or the owner's env files."""
    os.environ.setdefault("SAAS_ENV_FILE", "")
    os.environ.setdefault("SAAS_DATABASE_URL", "sqlite://")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from saas.templating import templates
    from saas.web import routes

    return templates, routes


def headers_file() -> str:
    # No Content-Security-Policy here: functions/_middleware.js sets it per response with a nonce, and a second
    # policy from this file would be enforced as well, blocking Turnstile, GA and Clarity.
    return "\n".join([
        "/*",
        "  X-Content-Type-Options: nosniff",
        "  X-Frame-Options: DENY",
        "  Referrer-Policy: strict-origin-when-cross-origin",
        "  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()",
        "  Strict-Transport-Security: max-age=31536000",
        "",
        "/static/*",
        # Pages link CSS/JS as ?v=<content hash>; fonts and icons are versioned by file name.
        "  Cache-Control: public, max-age=31536000, immutable",
        "",
    ])


def routes_file(out: Path) -> str:
    """_routes.json: every non-HTML file is served without invoking a Function, so it costs nothing against the
    free plan's 100k Function requests a day. HTML pages still go through functions/_middleware.js (CSP nonce)."""
    root_files = {f.name for f in out.iterdir() if f.is_file()} | {MANIFEST}
    exclude = ["/static/*"] + sorted(f"/{name}" for name in root_files
                                     if not name.endswith(".html") and name not in ("_headers", "_routes.json"))
    return json.dumps({"version": 1, "include": ["/*"], "exclude": exclude}, indent=2) + "\n"


def _clear_previous(out: Path) -> None:
    manifest = out / MANIFEST
    if not manifest.is_file():
        return
    for rel in json.loads(manifest.read_text(encoding="utf-8")):
        path = (out / rel).resolve()
        if out.resolve() in path.parents and path.is_file():
            path.unlink()
    for folder in sorted((p for p in out.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    manifest.unlink()


def exported_asset(out: Path):
    """asset() for the export: '/static/x.css' -> '/static/x.css?v=<hash of the exported bytes>'.

    Hashing what is published, not the source, matters for JS: an exported module's import lines change
    when the module it imports changes, and /static/* is cached as immutable."""
    def asset(url: str) -> str:
        f = out / url.lstrip("/")
        if url.startswith("/static/") and f.is_file():
            return f"{url}?v={hashlib.sha256(f.read_bytes()).hexdigest()[:10]}"
        return url

    return asset


def _copy_static(src: Path, dst: Path) -> list[Path]:
    """saas/web/static -> <out>/static. A module's imports carry the same ?v= the pages use for that module,
    so the page's <script> and another module's import are one URL, evaluated once."""
    written = []
    for f in sorted(src.rglob("*")):
        if not f.is_file() or f.suffix == ".md" or "__pycache__" in f.parts:
            continue
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
        written.append(target)
    asset = exported_asset(dst.parent)
    modules = [t for t in written if t.suffix == ".js"]
    for _ in range(len(modules) + 1):  # rewrite until stable: a dependency's hash settles before its importers'
        changed = False
        for js in modules:
            text = js.read_text(encoding="utf-8")

            def versioned(m: re.Match, here: Path = js) -> str:
                url = "/" + (here.parent / m.group(2)).resolve().relative_to(dst.parent.resolve()).as_posix()
                return m.group(1) + m.group(2) + asset(url)[len(url):] + m.group(4)

            new = JS_IMPORT.sub(versioned, text)
            if new != text:
                js.write_text(new, encoding="utf-8", newline="\n")
                changed = True
        if not changed:
            break
    return written


def export(out: Path, *, brand: str, public_url: str, support_email: str = "", ga4: str = "", clarity: str = "",
           turnstile: str = "", previews: bool = False) -> list[Path]:
    """Write the site to `out` and return every file written. previews=True adds the landing directions."""
    if previews and Path(out).resolve() == DEFAULT_OUT.resolve():
        raise ValueError(f"previews are never written to {DEFAULT_OUT}; pass --out with a scratch folder")
    brand, public_url = brand.strip(), public_url.strip().rstrip("/")
    ga4, clarity, turnstile = ga4.strip(), clarity.strip(), turnstile.strip()
    if not brand:
        raise ValueError("SAAS_BRAND_NAME is empty")
    if not re.match(r"^https?://[^\s/]+", public_url):
        raise ValueError(f"SAAS_PUBLIC_URL must be an absolute URL, got {public_url!r}")
    for value, pattern, name in ((ga4, GA4_ID, "GA4_MEASUREMENT_ID"), (clarity, CLARITY_ID, "CLARITY_PROJECT_ID"),
                                 (turnstile, TURNSTILE_KEY, "SAAS_TURNSTILE_SITE_KEY")):
        if value and not pattern.match(value):
            raise ValueError(f"{name} does not look right: {value!r}")

    templates, routes = _saas()
    env = templates.env
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    _clear_previous(out)

    written = _copy_static(routes.STATIC_DIR, out / "static")
    overrides = {"brand_name": brand, "public_url": public_url, "turnstile_site_key": turnstile,
                 "launch_mode": "waitlist", "support_email": support_email or env.globals.get("support_email", ""),
                 "asset": exported_asset(out)}
    saved = {key: env.globals.get(key) for key in overrides}
    env.globals.update(overrides)
    try:
        from saas.web import redesign

        for template, file, path in PAGES + (PREVIEWS if previews else []):
            ctx = routes.common_context(path, public_url)
            if template.startswith("redesign/"):
                ctx.update(routes.landing_context(pricing=None, static=True))
                ctx.update(redesign.context(routes.STATIC_DIR, exported_asset(out)))
            ctx.update(static_export=True, analytics={"ga4": ga4, "clarity": clarity})
            if template == "landing.html":
                ctx.update(routes.landing_context(pricing=None, static=True))  # no prices before launch
            elif template == "pricing.html":
                ctx.update(routes.pricing_context(pricing=None))
            elif template == "waitlist_thanks.html":
                ctx["code"] = ""  # thanks.js reads ?code= in the browser
            elif template.startswith("legal/"):
                ctx.update(routes.legal_context(template[len("legal/"):-len(".html")]))
            target = out / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(env.get_template(template).render(ctx), encoding="utf-8", newline="\n")
            written.append(target)
    finally:
        env.globals.update(saved)

    extras = {
        "favicon.ico": (routes.STATIC_DIR / "icons" / "favicon.ico").read_bytes(),
        "og.png": routes.og_png(brand),
        "site.webmanifest": json.dumps(routes.manifest_body(brand), indent=2).encode(),
        "robots.txt": routes.robots_txt(public_url).encode(),
        "sitemap.xml": routes.sitemap_xml(public_url, SITEMAP).encode(),
        "_headers": headers_file().encode(),
    }
    for name, data in extras.items():
        (out / name).write_bytes(data)
        written.append(out / name)
    (out / "_routes.json").write_text(routes_file(out), encoding="utf-8")
    written.append(out / "_routes.json")
    (out / MANIFEST).write_text(json.dumps(sorted(p.relative_to(out).as_posix() for p in written), indent=0),
                                encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default {DEFAULT_OUT})")
    parser.add_argument("--allow-placeholder", action="store_true",
                        help="build even though SAAS_SUPPORT_EMAIL is unset or an example address (preview only)")
    parser.add_argument("--previews", action="store_true",
                        help="also write the landing directions to preview/{a,b,c}/ (scratch --out only)")
    args = parser.parse_args(argv)
    support = os.environ.get("SAAS_SUPPORT_EMAIL", "").strip()
    if is_placeholder_email(support) and not args.allow_placeholder:
        print("error: SAAS_SUPPORT_EMAIL is unset or an example address, so every page would show a contact address "
              "nobody reads. Set it, or pass --allow-placeholder for a preview build.", file=sys.stderr)
        return 2
    try:
        files = export(
            args.out,
            brand=os.environ.get("SAAS_BRAND_NAME", "doomscroller"),
            public_url=os.environ.get("SAAS_PUBLIC_URL", DEFAULT_PUBLIC_URL),
            support_email=support,
            ga4=os.environ.get("GA4_MEASUREMENT_ID", ""),
            clarity=os.environ.get("CLARITY_PROJECT_ID", ""),
            turnstile=os.environ.get("SAAS_TURNSTILE_SITE_KEY", ""),
            previews=args.previews,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pages = [f for f in files if f.suffix == ".html"]
    print(f"wrote {len(files)} files ({len(pages)} pages) to {args.out}")
    if is_placeholder_email(support):
        print("warning: --allow-placeholder: pages show a placeholder contact address; do not ship this build",
              file=sys.stderr)
    if not os.environ.get("SAAS_LEGAL_EFFECTIVE_DATE", "").strip():
        print("note: SAAS_LEGAL_EFFECTIVE_DATE not set; the legal pages are dated today", file=sys.stderr)
    for name in ("GA4_MEASUREMENT_ID", "CLARITY_PROJECT_ID", "SAAS_TURNSTILE_SITE_KEY"):
        if not os.environ.get(name, "").strip():
            print(f"note: {name} not set; that snippet is left out", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
