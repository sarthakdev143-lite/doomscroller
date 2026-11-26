"""One Jinja2 environment for every server-rendered page (web + admin). Integrator-owned.

Autoescape is on for every template. Never mark user- or visitor-supplied text |safe.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .config import settings

SAAS_DIR = Path(__file__).resolve().parent
TEMPLATE_DIRS = [SAAS_DIR / "web" / "templates", SAAS_DIR / "admin" / "templates"]
STATIC_DIRS = {
    "/static": SAAS_DIR / "web" / "static",
    "/admin-static": SAAS_DIR / "admin" / "static",
    "/ts": SAAS_DIR / "tracking" / "static",
}

templates = Jinja2Templates(directory=[str(p) for p in TEMPLATE_DIRS])


@lru_cache(maxsize=512)
def _digest(path: str, mtime: float) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:10]


def asset(url: str) -> str:
    """'/static/css/app.css' -> '/static/css/app.css?v=<hash>' so deploys bust browser caches."""
    for prefix, root in STATIC_DIRS.items():
        if url.startswith(prefix + "/"):
            f = root / url[len(prefix) + 1:]
            if f.is_file():
                return f"{url}?v={_digest(str(f), f.stat().st_mtime)}"
    return url


templates.env.globals.update(
    brand_name=settings.brand_name,
    public_url=settings.public_url,
    support_email=settings.support_email,
    launch_mode=settings.launch_mode,
    turnstile_site_key=settings.turnstile_site_key,
    asset=asset,
)
