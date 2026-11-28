"""How people sign in on this deployment: Google only, or Google plus email and password.

`SAAS_AUTH_GOOGLE_ONLY` decides. Unset (or "auto"), it is on when Google is configured and SMTP is not,
because without a mailer no verification, reset or magic link could ever arrive. Everything is read
live from settings, so a test (or an operator flipping the env and restarting) sees the change.

In Google-only mode the server sends no email at all: `mail_allowed()` is the switch every sender checks.
"""
from __future__ import annotations

from ..config import settings
from .errors import ApiError


def google_configured() -> bool:
    from . import google

    return google.enabled()


def google_only() -> bool:
    explicit = settings.auth_google_only
    if explicit is not None:
        return bool(explicit)
    return google_configured() and not (settings.smtp_host or "").strip()


def password_auth() -> bool:
    """Email + password sign-up and login, magic links, verification and reset mail."""
    return not google_only()


def mail_allowed() -> bool:
    """False in Google-only mode: no code path may even try to send mail."""
    return not google_only()


def describe() -> str:
    if google_only():
        return "google-only" if google_configured() else "google-only (GOOGLE NOT CONFIGURED: nobody can sign in)"
    return "password+google" if google_configured() else "password"


GOOGLE_ONLY_MESSAGE = "This app uses Google sign-in only. Continue with Google instead."


def require_password_auth() -> None:
    """Dependency for the email/password routes: a clear 404 when they are switched off."""
    if google_only():
        raise ApiError(404, "google_only", GOOGLE_ONLY_MESSAGE, sign_in="/login")


def mask_email(email: str) -> str:
    """'jane.doe@example.com' -> 'ja•••@example.com': enough to pick the right account, not to harvest one."""
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "•••"
    keep = 2 if len(local) > 3 else 1
    return f"{local[:keep]}•••@{domain}"
