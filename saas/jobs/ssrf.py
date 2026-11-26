"""The one URL validator for customer-supplied sources (estimate, job creation and the worker).

A source URL must be http(s), carry no credentials, use the default port, name a host (not an IP
literal) on the allowlist (`SAAS_ALLOWED_SOURCE_DOMAINS`, matched on whole labels so evilyoutube.com
and youtube.com.evil.com fail), and every address that host resolves to must be public: no private,
loopback, link-local (cloud metadata), CGNAT, multicast, reserved or IPv4-mapped/6to4/Teredo tricks.
The URL handed on is rebuilt from the parsed parts, never the raw string.

DNS rebinding: yt-dlp resolves the name again when it fetches, so the resolution check here is a
belt; the allowlist is the braces. yt-dlp only ever runs allowlisted, non-generic extractors (see
probe.py), and those only talk to their own platform's hosts, so a rebinding answer for an allowlisted
domain is the only remaining path, which needs control of that platform's DNS.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from ..auth.errors import ApiError
from ..config import settings

MAX_URL_LENGTH = 2000
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_resolver_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="saas-dns")


def _refuse(message: str = "Paste a link to a single video on a supported site.", code: str = "source_not_allowed") -> ApiError:
    return ApiError(400, code, message, supported=settings.allowed_source_domain_list)


def host_allowed(host: str, allowed: Optional[Iterable[str]] = None) -> bool:
    host = host.lower().rstrip(".")
    for d in (allowed if allowed is not None else settings.allowed_source_domain_list):
        d = d.lower().strip().rstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def is_public_address(raw: str) -> bool:
    try:
        addr = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
        elif addr.teredo is not None:
            return False
    if (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved
            or addr.is_unspecified or not addr.is_global):
        return False
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT:
        return False
    return True


def _system_resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list({info[4][0] for info in infos})


# Tests replace this to simulate DNS answers.
resolve_host: Callable[[str, int], list[str]] = _system_resolve


def _resolve_with_timeout(host: str, port: int) -> list[str]:
    fut = _resolver_pool.submit(resolve_host, host, port)
    try:
        return fut.result(timeout=settings.dns_timeout_s)
    except concurrent.futures.TimeoutError:
        raise _refuse("That site's address could not be looked up in time. Try again.", "source_unreachable")
    except OSError:
        raise _refuse("That site's address could not be found.", "source_unreachable")


def validate_url(raw: object, *, resolve: bool = True) -> str:
    """The safe, rebuilt URL, or ApiError(400)."""
    if not isinstance(raw, str):
        raise _refuse()
    raw = raw.strip()
    if not raw or len(raw) > MAX_URL_LENGTH or any(ord(c) < 33 or ord(c) == 127 for c in raw):
        raise _refuse()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise _refuse()
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise _refuse("Only http(s) links are supported.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _refuse()
    if "\\" in raw:
        raise _refuse()
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise _refuse()
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise _refuse("Use the site's address, not an IP address.")
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise _refuse()
    if port is not None and port not in (80, 443):
        raise _refuse()
    if not host_allowed(host):
        raise _refuse()
    if resolve:
        addrs = _resolve_with_timeout(host, port or (443 if scheme == "https" else 80))
        if not addrs or not all(is_public_address(a) for a in addrs):
            raise _refuse()
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, parts.query, ""))
