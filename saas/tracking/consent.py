"""Who has to be asked before analytics run, and what they answered. Enforced server-side.

Modes (`SAAS_CONSENT_MODE`):
- eu_strict (default): visitors from the EU/EEA, the UK and Switzerland (and, with
  `consent_unknown_country=ask`, anyone whose country is unknown) are asked first; nothing is
  stored for them until they accept. Everyone else gets a notice and is tracked unless they opt out.
- always_ask: everyone is asked first.
- notice_only: everyone gets the notice; opt-out only.

A browser sending Global Privacy Control (`Sec-GPC: 1`) is treated as having declined unless it
explicitly accepted on our banner afterwards.

States: granted (accepted), implied (notice region, did not opt out), denied, pending (must be asked
and has not answered). Analytics are allowed for granted and implied only; replay likewise.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from ..config import settings

COOKIE = "ds_consent"
COOKIE_MAX_AGE = 180 * 24 * 3600  # re-ask after six months
BANNER_VERSION = "2026-11"

# EU-27 + EEA (IS, LI, NO) + UK + Switzerland.
STRICT_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
    "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "GB", "CH",
    "GG", "JE", "IM", "GI",
})


def country_of(request: Request) -> str | None:
    from . import geo

    return geo.country(request)


def required(request: Request, country: str | None = None) -> bool:
    mode = (settings.consent_mode or "eu_strict").lower()
    if mode == "always_ask":
        return True
    if mode == "notice_only":
        return False
    cc = country if country is not None else country_of(request)
    if not cc:
        return (settings.consent_unknown_country or "ask").lower() != "notice"
    return cc.upper() in STRICT_COUNTRIES


def cookie_choice(request: Request) -> str | None:
    val = (request.cookies.get(COOKIE) or "").split(".", 1)[0]
    return val if val in ("granted", "denied") else None


def gpc(request: Request) -> bool:
    return request.headers.get("sec-gpc", "").strip() == "1"


def state(request: Request, country: str | None = None) -> str:
    choice = cookie_choice(request)
    if choice:
        return choice
    if gpc(request):
        return "denied"
    return "pending" if required(request, country) else "implied"


def analytics_allowed(request: Request, country: str | None = None) -> bool:
    return state(request, country) in ("granted", "implied")


def set_cookie(response: Response, choice: str) -> None:
    response.set_cookie(COOKIE, f"{choice}.{BANNER_VERSION}", max_age=COOKIE_MAX_AGE, path="/",
                        samesite="lax", secure=settings.is_prod, httponly=False)
