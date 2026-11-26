"""IP -> country / region / city / ASN.

Sources, best first: MaxMind GeoLite2/GeoIP2 databases (`SAAS_GEOIP_CITY_DB`, `SAAS_GEOIP_ASN_DB`),
then Cloudflare's visitor-location headers when `SAAS_TRUST_PROXY=cloudflare` (country always;
city/region/timezone when the "Add visitor location headers" managed transform is on). Private and
unparseable addresses resolve to nothing. Never raises.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict, dataclass

from fastapi import Request

from ..common import client_country
from ..config import settings
from ..security import netutil

log = logging.getLogger("saas.tracking.geo")


@dataclass
class Geo:
    country: str | None = None
    region: str | None = None
    city: str | None = None
    timezone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    asn: int | None = None
    asn_org: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


_readers: dict[str, object] = {}
_readers_lock = threading.Lock()


def _reader(path: str):
    if not path or not os.path.isfile(path):
        return None
    with _readers_lock:
        rd = _readers.get(path)
        if rd is None:
            try:
                import geoip2.database

                rd = geoip2.database.Reader(path)
            except Exception as exc:
                log.warning("cannot open GeoIP database %s: %s", path, type(exc).__name__)
                rd = False
            _readers[path] = rd
    return rd or None


def _from_maxmind(ip: str, geo: Geo) -> None:
    city_rd = _reader(settings.geoip_city_db)
    if city_rd is not None:
        try:
            r = city_rd.city(ip)
            geo.country = (r.country.iso_code or None)
            geo.city = r.city.name or None
            sub = r.subdivisions.most_specific
            geo.region = sub.name or None
            geo.timezone = r.location.time_zone or None
            geo.latitude = r.location.latitude
            geo.longitude = r.location.longitude
        except Exception:
            pass
    asn_rd = _reader(settings.geoip_asn_db)
    if asn_rd is not None:
        try:
            r = asn_rd.asn(ip)
            geo.asn = r.autonomous_system_number
            geo.asn_org = r.autonomous_system_organization
        except Exception:
            pass


def _from_cloudflare(request: Request, geo: Geo) -> None:
    if settings.trust_proxy.lower() != "cloudflare":
        return
    h = request.headers
    geo.country = geo.country or client_country(request)
    geo.city = geo.city or (h.get("cf-ipcity") or None)
    geo.region = geo.region or (h.get("cf-region") or None)
    geo.timezone = geo.timezone or (h.get("cf-timezone") or None)
    try:
        if geo.latitude is None and h.get("cf-iplatitude"):
            geo.latitude = float(h["cf-iplatitude"])
            geo.longitude = float(h["cf-iplongitude"])
    except (TypeError, ValueError, KeyError):
        pass


def lookup(ip: str | None, request: Request | None = None) -> Geo:
    geo = Geo()
    norm = netutil.normalise(ip)
    if norm and netutil.is_public(norm):
        _from_maxmind(norm, geo)
    if request is not None:
        _from_cloudflare(request, geo)
    if geo.country:
        geo.country = geo.country.upper()[:2]
    for attr in ("city", "region", "timezone", "asn_org"):
        val = getattr(geo, attr)
        if isinstance(val, str):
            setattr(geo, attr, val[:128])
    return geo


def country(request: Request, ip: str | None = None) -> str | None:
    """Cheapest answer to "which country is this request from?"."""
    cc = client_country(request)
    if cc:
        return cc
    if ip is None:
        from ..common import client_ip

        ip = client_ip(request)
    return lookup(ip).country
