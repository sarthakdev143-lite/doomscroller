"""IP helpers that never raise. Callers pass whatever `saas.common.client_ip` gave them, which in tests
is the literal string "testclient" and behind a broken proxy could be anything."""
from __future__ import annotations

import ipaddress
from functools import lru_cache

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=4096)
def parse_ip(raw: str | None) -> IPAddress | None:
    if not raw:
        return None
    try:
        ip = ipaddress.ip_address(str(raw).strip())
    except ValueError:
        return None
    # ::ffff:1.2.3.4 is the same client as 1.2.3.4; compare and ban them as one.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_network(raw: str | None) -> IPNetwork | None:
    """'1.2.3.4' -> 1.2.3.4/32, '10.0.0.0/8' -> that network, junk -> None."""
    if not raw:
        return None
    try:
        return ipaddress.ip_network(str(raw).strip(), strict=False)
    except ValueError:
        return None


def is_public(raw: str | None) -> bool:
    ip = parse_ip(raw)
    return bool(ip and ip.is_global)


def in_any(raw: str | None, networks: list[IPNetwork]) -> bool:
    ip = parse_ip(raw)
    if ip is None:
        return False
    return any(ip.version == n.version and ip in n for n in networks)


def parse_network_list(csv: str) -> list[IPNetwork]:
    nets = []
    for part in (csv or "").split(","):
        net = parse_network(part)
        if net is not None:
            nets.append(net)
    return nets


def normalise(raw: str | None) -> str | None:
    """Canonical text form of an IP, or None if it is not one."""
    ip = parse_ip(raw)
    return str(ip) if ip else None
