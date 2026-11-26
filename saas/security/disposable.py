"""Disposable-email detection and alias-collapsing canonical emails.

The domain list is the vendored disposable-email-domains blocklist (CC0, see data/SOURCE.txt).
A subdomain of a listed domain counts too (`x.mailinator.com`). `canonical()` collapses the aliases
people use to farm free credits from one inbox: Gmail ignores dots and `+tags`, most other big
providers ignore `+tags`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..config import settings

_DATA = Path(__file__).resolve().parent / "data" / "disposable_domains.txt"

# Never treat these as disposable, whatever a list says: a false positive here locks real users out.
_NEVER = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me", "aol.com", "gmx.com",
    "gmx.de", "web.de", "yandex.com", "yandex.ru", "mail.ru", "zoho.com", "fastmail.com", "hey.com",
    "tutanota.com", "tuta.io", "qq.com", "163.com", "126.com", "rediffmail.com", "naver.com",
})

_GMAIL = frozenset({"gmail.com", "googlemail.com"})
# Providers that deliver user+tag@ to user@. (Yahoo's alias separator is '-', but '-' is also a
# legal character in real local parts, so it is left alone.)
_PLUS_PROVIDERS = frozenset({
    "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "pm.me", "fastmail.com", "zoho.com", "hey.com", "yandex.com",
    "yandex.ru", "gmx.com", "tutanota.com", "tuta.io", "aol.com",
})


@lru_cache(maxsize=1)
def _domains() -> frozenset[str]:
    out = set()
    try:
        for line in _DATA.read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                out.add(line)
    except OSError:
        pass
    return frozenset(out)


def _csv(value: str) -> set[str]:
    return {d.strip().lower().lstrip("@") for d in (value or "").split(",") if d.strip()}


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower().rstrip(".") if "@" in email else ""


def is_disposable_domain(domain: str) -> bool:
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return False
    allow = _csv(settings.disposable_allow_domains) | _NEVER
    if domain in allow:
        return False
    extra = _csv(settings.disposable_extra_domains)
    listed = _domains() | extra
    parts = domain.split(".")
    # Check the domain and every parent (a.b.example.com -> b.example.com -> example.com).
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in allow:
            return False
        if candidate in listed:
            return True
    return False


def is_disposable(email: str) -> bool:
    return is_disposable_domain(domain_of(email))


def normalise(email: str) -> str:
    """Trimmed, lower-cased address. Local parts are case-insensitive at every provider that matters."""
    return (email or "").strip().lower()


def canonical(email: str) -> str:
    """Collapse provider aliases: j.o.e+promo@gmail.com -> joe@gmail.com."""
    email = normalise(email)
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    if domain in _GMAIL:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    elif domain in _PLUS_PROVIDERS or "+" in local:
        # Unknown providers: still strip +tags. Collapsing too much only withholds a duplicate
        # free grant; collapsing too little hands credits to every alias.
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"
