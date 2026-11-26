"""Password hashing (argon2id), a minimal strength check and the breached-password check.

* argon2id with OWASP's baseline parameters (m=19 MiB, t=2, p=1). Hashes carry their parameters, so
  raising them later rehashes users transparently on their next login (`needs_rehash`).
* Unknown-email logins still run one full verify against a dummy hash, so response time does not
  reveal whether an account exists.
* The strength check is deliberately small (no zxcvbn dependency): length, a common-password list,
  the user's own email/name, and degenerate patterns (repeats, keyboard runs).
* HIBP's k-anonymity range API only ever sees the first five hex characters of the SHA-1. It fails
  open: an outage must not stop signups.
"""
from __future__ import annotations

import hashlib
import logging
import re
from functools import lru_cache
from typing import Optional

import httpx
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import settings

log = logging.getLogger("saas.auth.passwords")

MAX_LENGTH = 256  # argon2 hashes any length; an unbounded body is still a CPU lever
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)

_COMMON = {
    "password", "password1", "password12", "password123", "password1234", "passw0rd", "p@ssw0rd", "p@ssword",
    "123456", "1234567", "12345678", "123456789", "1234567890", "0123456789", "12345678910", "987654321",
    "qwerty", "qwerty123", "qwertyuiop", "qwerty1234", "asdfghjkl", "zxcvbnm", "1q2w3e4r", "1q2w3e4r5t",
    "1qaz2wsx", "1qaz2wsx3edc", "abc123", "abcd1234", "abcdef", "abcdefgh", "abcdefghij", "iloveyou",
    "iloveyou1", "letmein", "letmein123", "welcome", "welcome1", "welcome123", "admin", "admin123",
    "administrator", "monkey", "dragon", "football", "baseball", "superman", "batman", "trustno1",
    "sunshine", "princess", "starwars", "whatever", "shadow", "master", "michael", "jennifer", "computer",
    "internet", "freedom", "secret", "changeme", "default", "youtube", "youtuber", "subscribe", "creator",
    "doomscroller", "shorts", "tiktok", "instagram", "facebook", "google", "india123", "chennai",
    "mumbai123", "delhi123", "pakistan", "bangladesh", "qazwsxedc", "zaq12wsx", "11111111", "00000000",
    "aaaaaaaa", "1111111111", "123123123", "123321", "666666", "888888", "121212", "112233", "pokemon",
    "minecraft", "fortnite", "loveyou", "hello123", "helloworld", "test1234", "testtest", "guest123",
}
_RUNS = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiopasdfghjklzxcvbnm", "1qaz2wsx3edc4rfv5tgb6yhn")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash("dummy-password-for-timing-equalisation")


def verify_password(stored_hash: Optional[str], password: str) -> bool:
    """Constant-work verify: a missing hash still costs one argon2 verification."""
    if not password or len(password) > MAX_LENGTH:
        try:
            _hasher.verify(_dummy_hash(), "x")
        except VerificationError:
            pass
        return False
    try:
        return _hasher.verify(stored_hash or _dummy_hash(), password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def _has_run(lowered: str, n: int = 6) -> bool:
    for run in _RUNS:
        for i in range(len(run) - n + 1):
            chunk = run[i:i + n]
            if chunk in lowered or chunk[::-1] in lowered:
                return True
    return False


def strength_problem(password: str, *, email: str = "", name: str = "") -> Optional[str]:
    """None when acceptable, else a human sentence explaining what to change."""
    n = settings.password_min_length
    if len(password) < n:
        return f"Use at least {n} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    lowered = password.lower()
    stripped = re.sub(r"[\d\W_]+$", "", lowered)  # "password2024!" -> "password"
    if lowered in _COMMON or stripped in _COMMON:
        return "That password is too common. Pick something less guessable."
    local = email.split("@", 1)[0].lower() if email else ""
    if len(local) >= 4 and local in lowered:
        return "Don't use your email address in your password."
    for part in re.split(r"\s+", (name or "").lower()):
        if len(part) >= 4 and part in lowered:
            return "Don't use your name in your password."
    if len(set(password)) < 5:
        return "Use more varied characters."
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if len(password) < 14 and classes < 2:
        return "Mix letters with numbers or symbols, or use a longer passphrase."
    if _has_run(lowered) and len(password) < 16:
        return "Avoid keyboard or alphabet sequences like 'qwerty' or '123456'."
    return None


def breached_count(password: str, *, client: Optional[httpx.Client] = None) -> Optional[int]:
    """How often HIBP has seen this password; None when the check is off or failed (fail open)."""
    if not settings.hibp_enabled:
        return None
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()  # noqa: S324 - HIBP's protocol
    prefix, suffix = digest[:5], digest[5:]
    try:
        own = client is None
        c = client or httpx.Client(timeout=settings.hibp_timeout_s)
        try:
            r = c.get(f"https://api.pwnedpasswords.com/range/{prefix}",
                      headers={"Add-Padding": "true", "User-Agent": "saas-password-check"})
        finally:
            if own:
                c.close()
        if r.status_code != 200:
            return None
        for line in r.text.splitlines():
            hsh, _, count = line.strip().partition(":")
            if hsh.upper() == suffix:
                try:
                    return int(count)
                except ValueError:
                    return None
        return 0
    except Exception as exc:  # network, timeout, DNS: fail open
        log.info("HIBP check skipped: %s", type(exc).__name__)
        return None


def check_new_password(password: str, *, email: str = "", name: str = "") -> Optional[tuple[str, str]]:
    """(error_code, message) when the password must be refused, else None."""
    problem = strength_problem(password, email=email, name=name)
    if problem:
        return "weak_password", problem
    count = breached_count(password)
    if count:
        return "breached_password", (
            "This password has appeared in a data breach. Choose a different one; "
            "attackers try breached passwords first.")
    return None
