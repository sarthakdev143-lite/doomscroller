"""Customer pages (saas.web): landing, waitlist thanks, legal pages, icons and the share image.

Pages are server-rendered from saas/web/templates through the shared Jinja environment
(saas.templating), and talk to the JSON endpoints other agents own (docs/saas/CONTRACT.md) with
fetch from saas/web/static/js. These routes only read (the billing catalog seeds itself on first read).
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session

from ..common import client_country, utcnow
from ..config import settings
from ..db import get_db
from ..templating import templates

log = logging.getLogger("saas.web")

router = APIRouter(include_in_schema=False)

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
SEED_CATALOG = WEB_DIR.parent / "billing" / "seed_catalog.json"

# Job option limits from the jobs contract (POST /api/jobs options): clips 1-20, 15-90 s per clip.
MAX_CLIPS = 20
CLIP_SECONDS = (15, 90)
# The owner's free tier (2026-11-27): whichever of the two runs out first.
FREE_SHORTS = 10
FREE_MINUTES = 10

_SOURCE_NAMES = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "m.youtube.com": "YouTube", "vimeo.com": "Vimeo",
    "twitch.tv": "Twitch", "loom.com": "Loom", "x.com": "X", "twitter.com": "X", "tiktok.com": "TikTok",
    "instagram.com": "Instagram", "facebook.com": "Facebook", "fb.watch": "Facebook",
    "drive.google.com": "Google Drive", "dropbox.com": "Dropbox", "rumble.com": "Rumble", "kick.com": "Kick",
}


# ---- pricing ------------------------------------------------------------------------------------
# Two shapes reach the page. The live one is saas.billing.catalog.public_catalog():
#   plans: [{slug, name, description, monthly_credits, rollover_months, features: {api, mcp, ...},
#            prices: {"month": view | None, "year": view | None}}]
#   packs: [{slug, name, description, credits, price: view}]     view = {amount_minor, currency, display}
# The fallback is saas/billing/seed_catalog.json: the same plans and packs with no prices on them, and a
# top-level "prices" list of {item_kind, item_slug, interval, currency, tier, amount_minor}.

_SEED_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}


def _minor(view: Any) -> int | None:
    if not isinstance(view, dict):
        return None
    amount = view.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
        return None
    return int(amount)


def _display(minor: int, currency: str, view: dict) -> str:
    shown = view.get("display")
    if isinstance(shown, str) and shown.strip():
        return shown.strip()
    major, cents = divmod(minor, 100)  # the seed file has no formatted price; mirror billing's format
    symbol = _SEED_SYMBOLS.get(currency, f"{currency} ")
    return f"{symbol}{major:,}" + (f".{cents:02d}" if cents else "")


def _symbol(display: str) -> str:
    """'$' from '$9', '₹' from '₹499', 'SEK ' from 'SEK 99': whatever the formatter puts before the digits."""
    return re.match(r"^[^\d]*", display).group(0)


def _per_minute(minor: int, credits: int, symbol: str, currency: str) -> str:
    """One source minute, to a tenth of a cent: enough to compare packs and plans."""
    # shortcut: assumes 2-decimal currencies (USD, INR, EUR...); add a zero-decimal table if JPY is sold
    value = minor / 100 / credits
    text = f"{value:.3f}" if value < 1 else f"{value:.2f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency}"


def _seed_as_live(raw: dict) -> dict:
    """Give the seed file the live catalog's shape: default-tier prices in the default currency."""
    currency = settings.default_currency.upper()
    rows = [r for r in raw.get("prices") or [] if isinstance(r, dict)
            and str(r.get("currency", "")).upper() == currency and r.get("tier", "default") == "default"]

    def view(kind: str, slug: Any, interval: str) -> dict | None:
        for r in rows:
            if r.get("item_kind") == kind and r.get("item_slug") == slug and r.get("interval") == interval:
                return {"amount_minor": r.get("amount_minor"), "currency": currency}
        return None

    # Only what the owner has approved: an item is published only when it says so, the same default as
    # saas/billing/catalog.py, so provisional prices in the seed never reach a page.
    def published(item: Any) -> bool:
        return isinstance(item, dict) and item.get("published") is True

    plans = [{**p, "prices": {"month": view("plan", p.get("slug"), "month")}}
             for p in raw.get("plans") or [] if published(p)]
    packs = [{**k, "price": view("pack", k.get("slug"), "once")} for k in raw.get("packs") or [] if published(k)]
    return {"currency": currency, "plans": plans, "packs": packs}


def normalise_catalog(raw: Any) -> dict | None:
    """The catalog as the templates use it, or None when nothing in it carries a usable price."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("prices"), list):
        raw = _seed_as_live(raw)
    currency = str(raw.get("currency") or settings.default_currency).upper()
    out: dict[str, Any] = {"currency": currency, "plans": [], "packs": []}
    for kind in ("plans", "packs"):
        for item in raw.get(kind) or []:
            if not isinstance(item, dict) or item.get("public") is False or item.get("active") is False:
                continue
            if kind == "plans":
                view = (item.get("prices") or {}).get("month") if isinstance(item.get("prices"), dict) else None
                credits = item.get("monthly_credits")
            else:
                view = item.get("price")
                credits = item.get("credits")
            minor = _minor(view)
            name = str(item.get("name") or "").strip()
            if minor is None or not name or isinstance(credits, bool) or not isinstance(credits, int) or credits <= 0:
                continue
            features = item.get("features") if isinstance(item.get("features"), dict) else {}
            label = _display(minor, currency, view)
            out[kind].append({
                "slug": str(item.get("slug") or name),
                "name": name,
                "description": str(item.get("description") or "").strip(),
                "credits": credits,
                "price_label": label,
                "symbol": _symbol(label),
                "per_minute": _per_minute(minor, credits, _symbol(label), currency) if minor else None,
                "per_minute_value": minor / 100 / credits,
                "api": bool(features.get("api") or features.get("mcp")),
                "rollover_months": item.get("rollover_months") if isinstance(item.get("rollover_months"), int) else 0,
            })
    if not out["plans"] and not out["packs"]:
        return None
    paid = [e for e in out["plans"] + out["packs"] if e["per_minute_value"] > 0]
    out["cheapest"] = min(paid, key=lambda e: e["per_minute_value"]) if paid else None
    out["api_plans"] = [p["name"] for p in out["plans"] if p["api"]]
    return out


def load_pricing(db: Session, country: str | None) -> dict | None:
    """Live catalog if billing has landed, else the seed file, else None (the page hides prices)."""
    raw: Any = None
    try:
        catalog = importlib.import_module("saas.billing.catalog")
        raw = catalog.public_catalog(db, country)
    except Exception as exc:  # not built yet, table missing, bad row: the landing page must still render
        log.debug("live catalog unavailable: %s", type(exc).__name__)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - a dead connection has nothing to roll back
            pass
        raw = None
    pricing = normalise_catalog(raw)
    if pricing is None and SEED_CATALOG.is_file():
        try:
            pricing = normalise_catalog(json.loads(SEED_CATALOG.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pricing = None
    return pricing


# ---- page context -------------------------------------------------------------------------------

def _source_names() -> list[str]:
    names: list[str] = []
    for domain in settings.allowed_source_domain_list:
        name = _SOURCE_NAMES.get(domain, domain)
        if name not in names:
            names.append(name)
    return names


@lru_cache(maxsize=1)
def _waveform(bars: int = 220) -> list[dict]:
    """A deterministic loudness strip for the demo timeline (SVG rects, so no inline styles)."""
    out = []
    for i in range(bars):
        x = i / bars
        level = 0.34 + 0.22 * math.sin(x * 37.0) * math.sin(x * 5.3 + 1.1) + 0.18 * math.sin(x * 211.0 + 0.7)
        level += 0.12 * math.sin(x * 97.0 + 2.3)
        level = max(0.08, min(0.96, abs(level)))
        h = round(level * 56, 1)
        out.append({"x": i * 5, "y": round(30 - h / 2, 1), "h": h})
    return out


# The demo source: a 72:40 episode. Clips sit on sentence boundaries; seconds into the source.
DEMO_SOURCE_SECONDS = 72 * 60 + 40
DEMO_CLIPS = (
    {"n": 1, "start": 14 * 60 + 2.3, "end": 14 * 60 + 17.9},
    {"n": 2, "start": 31 * 60 + 18.0, "end": 31 * 60 + 52.4},
    {"n": 3, "start": 58 * 60 + 40.1, "end": 59 * 60 + 36.9},
)


def _clock(seconds: float, tenths: bool = True) -> str:
    minutes, sec = divmod(seconds, 60)
    return f"{int(minutes)}:{sec:04.1f}" if tenths else f"{int(minutes)}:{int(sec):02d}"


def _demo() -> dict:
    width = 220 * 5
    clips = []
    for clip in DEMO_CLIPS:
        w = max(6.0, (clip["end"] - clip["start"]) / DEMO_SOURCE_SECONDS * width)  # visible even when short
        x = clip["start"] / DEMO_SOURCE_SECONDS * width
        clips.append({
            **clip,
            "x": round(x, 1),
            "w": round(w, 1),
            "x_pct": round(x / width * 100, 3),
            "w_pct": round(w / width * 100, 3),
            "start_label": _clock(clip["start"]),
            "end_label": _clock(clip["end"]),
            "length": f"{clip['end'] - clip['start']:.1f}s",
        })
    return {
        "waveform": _waveform(),
        "width": width,
        "clips": clips,
        "duration_label": "1:12:40",
        "credits": math.ceil(DEMO_SOURCE_SECONDS / 60),
    }


def _credit_examples() -> list[dict]:
    """Worked examples of the rounding rule (1 credit per source minute, rounded up per job)."""
    examples = (("An interview", 18 * 60 + 5), ("Episode 41, above", DEMO_SOURCE_SECONDS),
                ("A lecture", 1 * 3600 + 58 * 60), ("A stream replay", 3 * 3600 + 4 * 60 + 12))
    rows = []
    for label, seconds in examples:
        h, rest = divmod(seconds, 3600)
        m, s = divmod(rest, 60)
        length = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        rows.append({"label": label, "length": length, "credits": math.ceil(seconds / 60)})
    return rows


def common_context(path: str, public_url: str) -> dict:
    """What every page template reads, for a request or for the static export."""
    now = utcnow()
    return {
        "canonical": f"{public_url.rstrip('/')}{path}",
        "year": now.year,
        "now_ms": int(now.timestamp() * 1000),  # the form's render time; waitlist.js refreshes it on load
        "free_shorts": FREE_SHORTS,
        "free_minutes": FREE_MINUTES,
        "retention_days": settings.clip_retention_days,
        "is_dev": not settings.is_prod,
        "static_export": False,
        "analytics": {},
    }


API_PLACEHOLDER = "https://<app-host>/api/v1"  # the static site, before the app has an address


def api_links(static: bool) -> dict:
    """The API base and MCP URL the examples show. On the static site there is no API at public_url (Pages
    serves only the marketing pages), so unless SAAS_API_BASE_URL names the app, show a host nobody can mistake
    for a real one."""
    base = settings.api_base_url.strip().rstrip("/")
    if not base:
        base = API_PLACEHOLDER if static else f"{settings.public_url.rstrip('/')}/api/v1"
    origin = base[: -len("/api/v1")] if base.endswith("/api/v1") else base
    return {"api_base": base, "mcp_url": f"{origin}/mcp", "api_is_placeholder": base == API_PLACEHOLDER}


def legal_date(today=None) -> str:
    """'27 November 2026': the configured effective date, else the day of rendering (or of the export)."""
    if settings.legal_effective_date.strip():
        return settings.legal_effective_date.strip()
    d = today or utcnow().date()
    return f"{d.day} {d:%B %Y}"


def landing_context(pricing: dict | None, ref: str = "", *, static: bool = False) -> dict:
    max_upload_gb = settings.max_upload_mb / 1024
    return {
        **api_links(static),
        "pricing": pricing,
        "demo": _demo(),
        "credit_examples": _credit_examples(),
        "sources": _source_names(),
        "max_upload": f"{max_upload_gb:g} GB" if max_upload_gb >= 1 else f"{settings.max_upload_mb} MB",
        "max_clips": MAX_CLIPS,
        "clip_seconds": CLIP_SECONDS,
        "ref": ref[:64],
    }


def pricing_context(pricing: dict | None) -> dict:
    return {"pricing": pricing, "credit_examples": _credit_examples(), "max_clips": MAX_CLIPS, "ref": ""}


LEGAL = {
    "privacy": ("Privacy policy", "What we collect, why, who processes it, how long we keep it, and your rights."),
    "terms": ("Terms of service", "The agreement between you and us when you use the service."),
    "refunds": ("Refund policy", "When payments can be refunded, and how."),
    "cookies": ("Cookie policy", "Every cookie and similar technology we use, and how to change your choice."),
}


def legal_context(slug: str) -> dict:
    title, description = LEGAL[slug]
    return {"page_title": title, "page_description": description, "legal_slug": slug, "legal_pages": LEGAL,
            "session_days": settings.session_days, "operator_name": settings.operator_name.strip(),
            "operator_country": settings.operator_country.strip(), "legal_date": legal_date()}


def _base_context(request: Request, **extra: Any) -> dict:
    ctx = common_context(request.url.path, settings.public_url)
    ctx["request"] = request
    ctx.update(extra)
    return ctx


def _render(request: Request, template: str, status_code: int = 200, **extra: Any):
    return templates.TemplateResponse(request, template, _base_context(request, **extra), status_code=status_code)


# ---- pages --------------------------------------------------------------------------------------

@router.get("/")
def landing(request: Request, db: Session = Depends(get_db)):
    pricing = load_pricing(db, client_country(request))
    return _render(request, "landing.html", **landing_context(pricing, request.query_params.get("ref") or ""))


@router.get("/pricing")
def pricing_page(request: Request, db: Session = Depends(get_db)):
    return _render(request, "pricing.html", **pricing_context(load_pricing(db, client_country(request))))


@router.get("/waitlist/thanks")
def waitlist_thanks(request: Request):
    return _render(request, "waitlist_thanks.html", code=(request.query_params.get("code") or "")[:64])


def _legal_page(slug: str):
    def page(request: Request):
        return _render(request, f"legal/{slug}.html", **legal_context(slug))

    page.__name__ = f"legal_{slug}"
    return page


for _slug in LEGAL:
    router.add_api_route(f"/{_slug}", _legal_page(_slug), methods=["GET"])


@router.get("/styleguide")
def styleguide(request: Request):
    """Every component on one page, for building and reviewing screens. Not served in production."""
    if settings.is_prod:
        return _render(request, "errors/404.html", status_code=404)
    return _render(request, "styleguide.html")


# ---- icons, share image, crawlers ---------------------------------------------------------------

@router.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC_DIR / "icons" / "favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=604800"})


def manifest_body(brand: str) -> dict:
    return {
        "name": brand,
        "short_name": brand,
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f4f4f0",
        "theme_color": "#f4f4f0",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }


@router.get("/site.webmanifest")
def manifest():
    return Response(json.dumps(manifest_body(settings.brand_name)), media_type="application/manifest+json")


def robots_txt(public_url: str) -> str:
    base = public_url.rstrip("/")
    return "\n".join([
        "User-agent: *",
        "Disallow: /app",
        "Disallow: /api/",
        "Disallow: /admin",
        "Disallow: /auth/",
        f"Sitemap: {base}/sitemap.xml",
        "",
    ])


@router.get("/robots.txt")
def robots():
    return PlainTextResponse(robots_txt(settings.public_url))


def sitemap_xml(public_url: str, paths: list[str]) -> str:
    base = public_url.rstrip("/")
    urls = "".join(f"<url><loc>{base}{path}</loc></url>" for path in paths)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


@router.get("/sitemap.xml")
def sitemap():
    xml = sitemap_xml(settings.public_url, ["/", "/pricing", *(f"/{s}" for s in LEGAL)])
    return Response(xml, media_type="application/xml")


@lru_cache(maxsize=4)
def og_png(brand: str) -> bytes:
    """The 1200x630 share card, drawn from the configured brand name so a rename needs no new asset."""
    from PIL import Image, ImageDraw, ImageFont  # lazy: only social crawlers ask for this

    font_path = str(STATIC_DIR / "fonts" / "archivo-latin-wdth-normal.woff2")
    mono_path = str(STATIC_DIR / "fonts" / "ibm-plex-mono-latin-500-normal.woff2")

    def face(size: int, weight: int, width: int) -> ImageFont.FreeTypeFont:
        f = ImageFont.truetype(font_path, size)
        f.set_variation_by_axes([weight, width])
        return f

    img = Image.new("RGB", (1200, 630), "#f4f4f0")
    draw = ImageDraw.Draw(img)
    draw.text((72, 64), brand, font=face(44, 850, 68), fill="#111111")
    headline = face(112, 800, 72)
    draw.text((72, 170), "Shorts that end", font=headline, fill="#111111")
    second = "where the sentence ends."
    box = draw.textbbox((72, 290), second, font=headline)
    draw.rectangle((box[0] - 10, box[1] - 8, box[2] + 10, box[3] + 12), fill="#ffe500")
    draw.text((72, 290), second, font=headline, fill="#111111")
    mono = ImageFont.truetype(mono_path, 26)
    draw.text((72, 520), "Long video in. Finished vertical clips out. Priced per source minute.", font=mono,
              fill="#45453f")
    # A 9:16 frame with a caption bar: the same mark as the favicon.
    draw.rounded_rectangle((1040, 60, 1128, 216), radius=10, fill="#111111")
    draw.rectangle((1052, 168, 1116, 182), fill="#ffe500")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@router.get("/og.png")
def og_image():
    return Response(og_png(settings.brand_name), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# The signed-in product and sign-in pages register on this router (Python app only, never exported).
from . import app_pages  # noqa: E402,F401
