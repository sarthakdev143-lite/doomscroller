"""Transactional email: SMTP when SAAS_SMTP_HOST is set, the console (log) in dev otherwise.

Sending happens on a background thread so an SMTP round trip never shows up in response time (which
would let a timing attacker tell "account exists" from "no account" on forgot-password). Every message
is plain text plus a minimal escaped HTML alternative. `outbox` keeps the last messages in dev so
tests and local runs can read links without an SMTP server; it is never filled in prod.
"""
from __future__ import annotations

import html
import logging
import smtplib
import ssl
import threading
from collections import deque
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from ..config import settings

log = logging.getLogger("saas.auth.mailer")


@dataclass
class Mail:
    to: str
    subject: str
    text: str
    html: str


outbox: deque[Mail] = deque(maxlen=200)
_SYNC_FOR_TESTS = False  # tests flip this so sends happen inline


def _from_address() -> str:
    sender = settings.smtp_from or settings.support_email
    return formataddr((settings.brand_name, sender))


def _html(paragraphs: list[str], button: tuple[str, str] | None = None) -> str:
    body = "".join(f"<p style=\"margin:0 0 14px\">{html.escape(p)}</p>" for p in paragraphs)
    if button:
        label, url = button
        body += (f"<p style=\"margin:20px 0\"><a href=\"{html.escape(url, quote=True)}\" "
                 f"style=\"background:#111;color:#fff;padding:12px 18px;border-radius:8px;text-decoration:none;"
                 f"display:inline-block\">{html.escape(label)}</a></p>"
                 f"<p style=\"margin:0 0 14px;color:#666;font-size:13px\">Or paste this link: "
                 f"{html.escape(url)}</p>")
    return (f"<!doctype html><html><body style=\"font-family:system-ui,-apple-system,sans-serif;font-size:15px;"
            f"line-height:1.5;color:#111;max-width:560px;margin:0 auto;padding:24px\">{body}"
            f"<p style=\"color:#888;font-size:12px;margin-top:28px\">{html.escape(settings.brand_name)}</p>"
            f"</body></html>")


def _deliver(mail: Mail) -> tuple[bool, str]:
    """(sent, detail). Never raises and never logs credentials."""
    if not settings.smtp_host:
        if settings.is_prod:
            log.error("SMTP is not configured in prod; dropped mail '%s'", mail.subject)
            return False, "SMTP is not configured (SAAS_SMTP_HOST is empty)"
        outbox.append(mail)
        # WARNING so it shows on a default console (uvicorn leaves the root logger at WARNING). Dev only.
        log.warning("[dev mail] to=%s subject=%s\n%s", mail.to, mail.subject, mail.text)
        return True, "no SMTP configured: printed to the console (dev)"
    msg = EmailMessage()
    msg["From"] = _from_address()
    msg["To"] = mail.to
    msg["Subject"] = mail.subject
    msg["Message-ID"] = make_msgid(domain=(settings.smtp_from or settings.support_email).split("@")[-1])
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    try:
        ctx = ssl.create_default_context()
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        with server:
            if settings.smtp_starttls and not settings.smtp_ssl:
                server.starttls(context=ctx)
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        log.error("mail '%s' failed: SMTP login refused", mail.subject)
        return False, ("SMTP login refused. For Gmail use an App Password (Google Account > Security > "
                       "2-Step Verification > App passwords), not the account password.")
    except Exception as exc:
        log.error("mail '%s' failed: %s", mail.subject, type(exc).__name__)
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    return True, f"sent via {settings.smtp_host}:{settings.smtp_port}"


def send(to: str, subject: str, paragraphs: list[str], button: tuple[str, str] | None = None) -> None:
    text = "\n\n".join(paragraphs)
    if button:
        text += f"\n\n{button[0]}: {button[1]}"
    text += f"\n\n-- {settings.brand_name}"
    mail = Mail(to=to, subject=subject, text=text, html=_html(paragraphs, button))
    if _SYNC_FOR_TESTS:
        _deliver(mail)
        return
    threading.Thread(target=_deliver, args=(mail,), daemon=True, name="saas-mail").start()


def send_test(to: str) -> tuple[bool, str]:
    """Send one message synchronously and report what happened (scripts/saas_manage.py mail-test)."""
    paragraphs = [f"This is a test message from {settings.brand_name}.",
                  f"If you can read it, outgoing email works (server {settings.smtp_host or 'none'}, "
                  f"from {settings.smtp_from or settings.support_email})."]
    text = "\n\n".join(paragraphs) + f"\n\n-- {settings.brand_name}"
    return _deliver(Mail(to=to, subject=f"{settings.brand_name} mail test", text=text, html=_html(paragraphs)))


def _url(path: str) -> str:
    return settings.public_url.rstrip("/") + path


# ---- templates ----------------------------------------------------------------------------------

def send_verify(to: str, token: str) -> None:
    send(to, f"Confirm your email for {settings.brand_name}",
         ["Confirm this address to start making clips. The link works once and expires in "
          f"{settings.verify_token_hours} hours.",
          "If you didn't sign up, ignore this email."],
         ("Confirm email", _url(f"/auth/verify/{token}")))


def send_reset(to: str, token: str) -> None:
    send(to, f"Reset your {settings.brand_name} password",
         [f"Someone asked to reset your password. The link works once and expires in "
          f"{settings.reset_token_minutes} minutes. Resetting signs you out everywhere.",
          "If it wasn't you, ignore this email: your password stays as it is."],
         ("Choose a new password", _url(f"/reset/{token}")))


def send_magic(to: str, token: str) -> None:
    send(to, f"Your {settings.brand_name} sign-in link",
         [f"Use this link to sign in. It works once and expires in {settings.magic_token_minutes} minutes.",
          "If you didn't ask for it, ignore this email."],
         ("Sign in", _url(f"/auth/magic/{token}")))


def send_account_exists(to: str) -> None:
    send(to, f"You already have a {settings.brand_name} account",
         ["Someone (hopefully you) tried to sign up with this address, but it already has an account.",
          "Sign in instead, or reset your password if you've forgotten it."],
         ("Sign in", _url("/login")))


def send_password_changed(to: str) -> None:
    send(to, f"Your {settings.brand_name} password changed",
         ["Your password was just changed and your other sessions were signed out.",
          f"If this wasn't you, reset your password now and contact {settings.support_email}."])


def send_mfa_changed(to: str, enabled: bool) -> None:
    state = "turned on" if enabled else "turned off"
    send(to, f"Two-factor authentication {state}",
         [f"Two-factor authentication was {state} for your account.",
          f"If this wasn't you, contact {settings.support_email} immediately."])
