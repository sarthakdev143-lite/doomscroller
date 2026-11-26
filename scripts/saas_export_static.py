"""Render the marketing site to static files for Cloudflare Pages (edge/public/).

    .venv-saas/Scripts/python.exe scripts/saas_export_static.py [--out edge/public]

Python never runs on Pages: these files are the whole site. The waitlist form posts JSON to
/api/waitlist and the thanks page reads /api/waitlist/me, both Cloudflare Pages Functions in edge/functions.

Environment, all optional. An unset analytics or Turnstile ID means that snippet is not in the pages at all.
  SAAS_BRAND_NAME          default "doomscroller" (shown exactly as written)
  SAAS_PUBLIC_URL          default https://doomscroller.pages.dev (canonical URLs, share image, sitemap)
  SAAS_SUPPORT_EMAIL       the contact address on every page
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

GA4_ID = re.compile(r"^G-[A-Z0-9]{4,20}$")
CLARITY_ID = re.compile(r"^[a-z0-9]{6,20}$", re.IGNORECASE)
TURNSTILE_KEY = re.compile(r"^[0-9A-Za-z_-]{10,100}$")
JS_IMPORT = re.compile(r"""(\bfrom\s+["'])(\./[\w.-]+\.js)(\?v=[0-9a-f]+)?(["'])""")


def _saas():
    """Import the shared templates without touching a database or the owner's env files."""
    os.environ.setdefault("SAAS_ENV_FILE", "")
    os.environ.setdefault("SAAS_DATABASE_URL", "sqlite://")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from saas.templating import templates
    from saas.web import routes

    return templates, routes


def content_security_policy(ga4: str, clarity: str, turnstile: str) -> str:
    script, connect, img, frame = ["'self'"], ["'self'"], ["'self'", "data:"], ["'none'"]
    if ga4:
        script.append("https://www.googletagmanager.com")
        hosts = ["https://*.google-analytics.com", "https://*.analytics.google.com", "https://*.googletagmanager.com"]
        connect += hosts
        img += hosts
    if clarity:
        script.append("https://*.clarity.ms")
        connect += ["https://*.clarity.ms", "https://c.bing.com"]
        img += ["https://*.clarity.ms", "https://c.bing.com"]
    if turnstile:
        script.append("https://challenges.cloudflare.com")
        frame = ["https://challenges.cloudflare.com"]
    return "; ".join([
        "default-src 'self'", f"script-src {' '.join(script)}", f"connect-src {' '.join(connect)}",
        f"img-src {' '.join(img)}", "style-src 'self' 'unsafe-inline'", "font-src 'self'",
        f"frame-src {' '.join(frame)}", "worker-src 'self' blob:", "manifest-src 'self'", "object-src 'none'",
        "base-uri 'self'", "form-action 'self'", "frame-ancestors 'none'",
    ])


def headers_file(csp: str) -> str:
    # Static assets only: Pages Functions (/api/*) set their own headers.
    return "\n".join([
        "/*",
        "  X-Content-Type-Options: nosniff",
        "  X-Frame-Options: DENY",
        "  Referrer-Policy: strict-origin-when-cross-origin",
        "  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()",
        "  Strict-Transport-Security: max-age=31536000",
        f"  Content-Security-Policy: {csp}",
        "",
        "/static/*",
        # Pages link CSS/JS as ?v=<content hash>; fonts and icons are versioned by file name.
        "  Cache-Control: public, max-age=31536000, immutable",
        "",
    ])


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
           turnstile: str = "") -> list[Path]:
    """Write the site to `out` and return every file written."""
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
        for template, file, path in PAGES:
            ctx = routes.common_context(path, public_url)
            ctx.update(static_export=True, analytics={"ga4": ga4, "clarity": clarity})
            if template == "landing.html":
                ctx.update(routes.landing_context(pricing=None))  # no prices before launch
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
        "_headers": headers_file(content_security_policy(ga4, clarity, turnstile)).encode(),
    }
    for name, data in extras.items():
        (out / name).write_bytes(data)
        written.append(out / name)
    (out / MANIFEST).write_text(json.dumps(sorted(p.relative_to(out).as_posix() for p in written), indent=0),
                                encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default {DEFAULT_OUT})")
    args = parser.parse_args(argv)
    support = os.environ.get("SAAS_SUPPORT_EMAIL", "").strip()
    try:
        files = export(
            args.out,
            brand=os.environ.get("SAAS_BRAND_NAME", "doomscroller"),
            public_url=os.environ.get("SAAS_PUBLIC_URL", DEFAULT_PUBLIC_URL),
            support_email=support,
            ga4=os.environ.get("GA4_MEASUREMENT_ID", ""),
            clarity=os.environ.get("CLARITY_PROJECT_ID", ""),
            turnstile=os.environ.get("SAAS_TURNSTILE_SITE_KEY", ""),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pages = [f for f in files if f.suffix == ".html"]
    print(f"wrote {len(files)} files ({len(pages)} pages) to {args.out}")
    if not support or support.endswith("@example.com"):
        print("warning: SAAS_SUPPORT_EMAIL is not set; pages show a placeholder contact address", file=sys.stderr)
    for name in ("GA4_MEASUREMENT_ID", "CLARITY_PROJECT_ID", "SAAS_TURNSTILE_SITE_KEY"):
        if not os.environ.get(name, "").strip():
            print(f"note: {name} not set; that snippet is left out", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
