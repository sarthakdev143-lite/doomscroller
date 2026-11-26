"""User-Agent -> browser / OS / device, plus bot classification from every signal we have.

Bot reasons are kept as a list so the admin panel can show *why* a visitor was called a bot.
Strong reasons on their own make a bot; weak ones (datacenter IP) only count in combination.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache

_TOOL_RE = re.compile(
    r"(curl|wget|python-requests|python-urllib|aiohttp|httpx|go-http-client|java/|okhttp|axios|"
    r"node-fetch|undici|libwww|perl|ruby|php/|scrapy|httpclient|postmanruntime|insomnia|powershell)",
    re.I,
)
_HEADLESS_RE = re.compile(r"(headlesschrome|phantomjs|slimerjs|electron/.*headless|puppeteer|playwright|"
                          r"selenium|webdriver|cypress)", re.I)
_CRAWLER_RE = re.compile(r"(bot|crawl|spider|slurp|facebookexternalhit|embedly|preview|monitor|uptime|"
                         r"lighthouse|pagespeed|gtmetrix|pingdom|headless|scan|fetcher|archiver)", re.I)

STRONG = {"empty_ua", "ua_tool", "ua_headless", "ua_crawler", "webdriver", "automation"}


@dataclass
class UAInfo:
    browser: str | None = None
    browser_version: str | None = None
    os: str | None = None
    os_version: str | None = None
    device_type: str = "other"  # desktop | mobile | tablet | bot | other
    device_brand: str | None = None
    device_model: str | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@lru_cache(maxsize=2048)
def parse(ua: str | None) -> UAInfo:
    ua = (ua or "").strip()[:512]
    info = UAInfo()
    if not ua:
        info.reasons.append("empty_ua")
        info.device_type = "bot"
        return info
    try:
        from user_agents import parse as ua_parse

        p = ua_parse(ua)
        info.browser = (p.browser.family or None)
        info.browser_version = (p.browser.version_string or None)
        info.os = (p.os.family or None)
        info.os_version = (p.os.version_string or None)
        info.device_brand = p.device.brand or None
        info.device_model = p.device.model or None
        if p.is_bot:
            info.device_type = "bot"
            info.reasons.append("ua_crawler")
        elif p.is_tablet:
            info.device_type = "tablet"
        elif p.is_mobile:
            info.device_type = "mobile"
        elif p.is_pc:
            info.device_type = "desktop"
    except Exception:
        pass
    if _HEADLESS_RE.search(ua):
        info.reasons.append("ua_headless")
    if _TOOL_RE.search(ua):
        info.reasons.append("ua_tool")
    if "ua_crawler" not in info.reasons and _CRAWLER_RE.search(ua) and not _looks_like_browser(ua):
        info.reasons.append("ua_crawler")
    if info.reasons:
        info.device_type = "bot"
    for attr in ("browser", "os", "device_brand", "device_model"):
        val = getattr(info, attr)
        if val in ("Other", "Generic", ""):
            setattr(info, attr, None)
    return info


def _looks_like_browser(ua: str) -> bool:
    return ua.startswith("Mozilla/5.0") and ("AppleWebKit" in ua or "Gecko/" in ua)


# Client-side automation signals t.js reports in `sig.auto`.
_KNOWN_AUTOMATION = {"webdriver", "phantom", "nightmare", "selenium", "cdc", "headless_ua",
                     "headless_perm", "no_chrome_obj", "zero_outer", "no_languages", "playwright"}


def bot_reasons(ua_info: UAInfo, *, automation: list[str] | None = None, datacenter: bool = False,
                tor: bool = False) -> list[str]:
    reasons = list(ua_info.reasons)
    for a in automation or []:
        if a == "webdriver":
            reasons.append("webdriver")
        elif a in _KNOWN_AUTOMATION:
            reasons.append("automation")
    if datacenter:
        reasons.append("datacenter_ip")
    if tor:
        reasons.append("tor_exit")
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def is_bot(reasons: list[str]) -> bool:
    """Strong reason, or a datacenter IP together with any other automation hint."""
    if any(r in STRONG for r in reasons):
        return True
    return False
