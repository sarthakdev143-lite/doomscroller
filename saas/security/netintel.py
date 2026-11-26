"""Is this IP a Tor exit, a datacenter / hosting network, or a commercial VPN?

- Tor: the Tor Project's bulk exit list, fetched in the background, cached on disk under
  `SAAS_DATA_DIR/threat/` and in memory, refreshed every `threat_feed_refresh_hours`. A lookup never
  waits on the network; with no list yet it answers False (fail open: Tor is a risk signal, not a ban).
- Datacenter / VPN: the ASN from GeoIP (needs `SAAS_GEOIP_ASN_DB`), matched against known hosting
  ASNs and organisation-name keywords. Without the ASN database these stay False.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from . import netutil

log = logging.getLogger("saas.security.netintel")

# Big clouds, VPS hosts and colos. Traffic from here is servers and VPNs, not people at a browser.
HOSTING_ASNS = frozenset({
    16509, 14618, 8987,  # Amazon
    15169, 396982, 19527, 36040,  # Google / Google Cloud
    8075, 8068, 8069, 12076,  # Microsoft / Azure
    14061,  # DigitalOcean
    16276, 35540,  # OVH
    24940, 213230,  # Hetzner
    63949,  # Linode / Akamai cloud
    20473,  # Vultr (Choopa)
    51167,  # Contabo
    31898,  # Oracle Cloud
    45102, 37963,  # Alibaba
    132203, 45090,  # Tencent
    12876,  # Scaleway
    60781, 28753, 59253,  # Leaseweb
    9009,  # M247
    212238, 60068,  # Datacamp / CDN77
    46606,  # Unified Layer
    26496,  # GoDaddy
    47583, 204915,  # Hostinger
    62240,  # Clouvider
    53667,  # FranTech / BuyVM
    40021,  # Contabo US
    396356,  # Latitude.sh / Maxihost
    211252,  # Delis
    136787,  # TEFINCOM (NordVPN infra)
    207990,  # HostRoyale
    8100,  # QuadraNet
    29802,  # HIVELOCITY
    11878,  # tzulo
    19318,  # Interserver
    36352,  # ColoCrossing
    55286,  # B2 Net / ServerMania
    174,  # Cogent (mostly transit/hosting)
    13335,  # Cloudflare (WARP / Workers egress)
})

_HOSTING_WORDS = re.compile(
    r"\b(hosting|host|cloud|datacenter|data center|data-center|server|servers|vps|colo|colocation|"
    r"dedicated|amazon|aws|google llc|microsoft|azure|digitalocean|ovh|hetzner|linode|akamai|vultr|"
    r"choopa|contabo|oracle|alibaba|tencent|scaleway|leaseweb|m247|datacamp|cdn77|hostinger|"
    r"godaddy|ionos|hostwinds|kamatera|upcloud|fly\.io|render|heroku)\b",
    re.I,
)
_VPN_WORDS = re.compile(
    r"\b(vpn|nordvpn|expressvpn|surfshark|mullvad|proton|private internet access|pia|ipvanish|"
    r"cyberghost|windscribe|hide\.me|purevpn|torguard|packethub|tefincom|m247|datacamp|cdn77|"
    r"hostroyale|zenlayer)\b",
    re.I,
)
_VPN_ASNS = frozenset({9009, 212238, 60068, 136787, 207990, 31173, 39351, 209103, 41108})


@dataclass
class IpIntel:
    is_tor: bool = False
    is_datacenter: bool = False
    is_vpn: bool = False
    asn: int | None = None
    asn_org: str | None = None

    def flags(self) -> list[str]:
        out = []
        if self.is_tor:
            out.append("tor")
        if self.is_vpn:
            out.append("vpn")
        if self.is_datacenter:
            out.append("datacenter")
        return out


# ---- Tor exit list -----------------------------------------------------------------------------
_tor: set[str] = set()
_tor_loaded_at = 0.0
_tor_lock = threading.Lock()
_tor_refreshing = False


def _cache_file() -> Path:
    return Path(settings.data_dir) / "threat" / "tor_exits.txt"


def network_allowed() -> bool:
    """Background fetches are off under pytest: tests must never reach the network."""
    return "PYTEST_CURRENT_TEST" not in os.environ


def _parse_tor_list(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        norm = netutil.normalise(line.split()[0])
        if norm:
            out.add(norm)
    return out


def load_tor_list(text: str) -> int:
    """Replace the in-memory Tor exit set (tests and the refresher use this)."""
    global _tor, _tor_loaded_at
    ips = _parse_tor_list(text)
    with _tor_lock:
        _tor = ips
        _tor_loaded_at = time.time()
    return len(ips)


def _load_from_disk() -> None:
    global _tor_loaded_at
    f = _cache_file()
    try:
        if f.is_file():
            load_tor_list(f.read_text(encoding="utf-8"))
            # Treat the disk copy as being as old as the file, so a stale one still gets refreshed.
            _tor_loaded_at = f.stat().st_mtime
    except OSError as exc:
        log.debug("tor cache unreadable: %s", exc)


def refresh_tor_list(timeout: float = 20.0) -> int | None:
    """Fetch the exit list now (blocking). Returns the number of exits, or None on failure."""
    url = settings.tor_exit_list_url
    if not url:
        return None
    try:
        import httpx

        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": "saas-abuse-guard/1"})
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:
        log.warning("tor exit list fetch failed: %s", type(exc).__name__)
        return None
    if len(text) > 5_000_000:
        log.warning("tor exit list implausibly large; ignored")
        return None
    n = load_tor_list(text)
    if n < 100:
        log.warning("tor exit list has only %d entries; keeping it but check the source", n)
    try:
        f = _cache_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, f)
    except OSError as exc:
        log.debug("tor cache not written: %s", exc)
    return n


def _maybe_refresh_in_background() -> None:
    global _tor_refreshing
    if _tor_refreshing or not network_allowed() or not settings.tor_exit_list_url:
        return
    if time.time() - _tor_loaded_at < settings.threat_feed_refresh_hours * 3600:
        return
    with _tor_lock:
        if _tor_refreshing:
            return
        _tor_refreshing = True

    def run():
        global _tor_refreshing
        try:
            refresh_tor_list()
        finally:
            _tor_refreshing = False

    threading.Thread(target=run, name="tor-exit-refresh", daemon=True).start()


def is_tor(ip: str | None) -> bool:
    if not _tor_loaded_at:
        _load_from_disk()
    _maybe_refresh_in_background()
    norm = netutil.normalise(ip)
    return bool(norm and norm in _tor)


def reset_state() -> None:
    global _tor, _tor_loaded_at
    with _tor_lock:
        _tor = set()
        _tor_loaded_at = 0.0


# ---- hosting / VPN -----------------------------------------------------------------------------
def classify_asn(asn: int | None, org: str | None) -> tuple[bool, bool]:
    """(is_datacenter, is_vpn) for an ASN and its organisation name."""
    org = org or ""
    vpn = bool(asn in _VPN_ASNS or (org and _VPN_WORDS.search(org)))
    dc = bool(vpn or asn in HOSTING_ASNS or (org and _HOSTING_WORDS.search(org)))
    return dc, vpn


def inspect(ip: str | None, geo=None) -> IpIntel:
    """Everything we can say about an IP without the network. `geo` is a saas.tracking.geo.Geo."""
    intel = IpIntel()
    if not netutil.is_public(ip):
        return intel
    if geo is None:
        from ..tracking import geo as geo_mod

        geo = geo_mod.lookup(ip)
    intel.asn = getattr(geo, "asn", None)
    intel.asn_org = getattr(geo, "asn_org", None)
    intel.is_datacenter, intel.is_vpn = classify_asn(intel.asn, intel.asn_org)
    intel.is_tor = is_tor(ip)
    return intel
