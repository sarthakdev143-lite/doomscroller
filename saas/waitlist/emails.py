"""Waitlist emails. Sent through the core agent's `saas.auth.mailer.send(to, subject, text, html)`;
until that exists (or if it raises) the email is logged instead, so dev and tests still work."""
from __future__ import annotations

import html as html_mod
import logging
from urllib.parse import quote

from ..config import settings

log = logging.getLogger("saas.waitlist.emails")


def send(to: str, subject: str, text: str, html: str, *, paragraphs: list[str] | None = None,
         button: tuple[str, str] | None = None) -> bool:
    """Deliver through the core mailer, whichever of its two shapes it has:
    `send(to, subject, paragraphs, button)` (what it implements) or `send(to, subject, text, html)`
    (what the contract first said). Without a mailer the email is logged (dev)."""
    import inspect

    try:
        from ..auth import mailer  # core agent
        sender = getattr(mailer, "send")
    except Exception:
        sender = None
    if sender is None:
        log.info("[email not sent: no mailer configured] to=%s subject=%s\n%s", to, subject, text)
        return False
    try:
        params = list(inspect.signature(sender).parameters)
        if "paragraphs" in params:
            sender(to, subject, paragraphs or [p for p in text.split("\n\n") if p.strip()], button)
        else:
            sender(to, subject, text, html)
        return True
    except Exception as exc:
        log.warning("waitlist email to %s failed: %s", to.split("@")[-1], type(exc).__name__)
        return False


def _wrap(title: str, body_html: str) -> str:
    brand = html_mod.escape(settings.brand_name)
    return (
        "<!doctype html><html><body style=\"margin:0;padding:24px;background:#0f0f13;color:#ececf1;"
        "font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif\">"
        "<div style=\"max-width:520px;margin:0 auto;background:#17171d;border-radius:14px;padding:28px\">"
        f"<p style=\"margin:0 0 4px;font-weight:700;letter-spacing:.02em\">{brand}</p>"
        f"<h1 style=\"font-size:22px;margin:8px 0 16px\">{html_mod.escape(title)}</h1>{body_html}"
        "<p style=\"margin-top:28px;font-size:13px;color:#9a9aa6\">You are getting this because this address "
        f"was entered on the {brand} waitlist. If that was not you, ignore this email and nothing happens.</p>"
        "</div></body></html>"
    )


def _button(url: str, label: str) -> str:
    u = html_mod.escape(url, quote=True)
    return (f"<p><a href=\"{u}\" style=\"display:inline-block;background:#f2f2f5;color:#111;padding:12px 20px;"
            f"border-radius:10px;text-decoration:none;font-weight:700\">{html_mod.escape(label)}</a></p>"
            f"<p style=\"font-size:13px;color:#9a9aa6;word-break:break-all\">Or paste this link: {u}</p>")


def confirm_email(to: str, token: str, position: int | None) -> bool:
    url = f"{settings.public_url.rstrip('/')}/waitlist/confirm/{token}"
    brand = settings.brand_name
    hours = settings.waitlist_confirm_hours
    subject = f"Confirm your spot on the {brand} waitlist"
    pos = f" You're #{position} in line once confirmed." if position else ""
    text = (f"One click to confirm your spot on the {brand} waitlist.{pos}\n\n{url}\n\n"
            f"The link works for {hours} hours. If you didn't sign up, ignore this email.")
    body = (f"<p>One click and you're in line.{html_mod.escape(pos)}</p>" + _button(url, "Confirm my spot")
            + f"<p style=\"font-size:13px;color:#9a9aa6\">The link works for {hours} hours.</p>")
    paras = [f"One click to confirm your spot on the {brand} waitlist.{pos}",
             f"The link works for {hours} hours. If you didn't sign up, ignore this email and nothing happens."]
    return send(to, subject, text, _wrap("Confirm your spot", body), paragraphs=paras,
                button=("Confirm my spot", url))


def already_email(to: str, referral_url: str, position: int | None) -> bool:
    brand = settings.brand_name
    subject = f"You're already on the {brand} waitlist"
    pos = f"You're #{position} in line. " if position else ""
    text = (f"{pos}Share your link to move up: {referral_url}\n\n"
            "Each friend who joins and confirms moves you up the list.")
    body = (f"<p>{html_mod.escape(pos)}Share your link to move up: every friend who joins and confirms moves you "
            "up the list.</p>" + _button(referral_url, "Copy my referral link"))
    paras = [f"{pos}You're already on the {brand} waitlist.",
             "Share your link to move up: every friend who joins and confirms moves you up the list."]
    return send(to, subject, text, _wrap("You're already on the list", body), paragraphs=paras,
                button=("My referral link", referral_url))


def invite_email(to: str, code: str, expires_days: int) -> bool:
    brand = settings.brand_name
    url = f"{settings.public_url.rstrip('/')}/signup?invite={quote(code)}&email={quote(to)}"
    subject = f"Your {brand} invite is here"
    text = (f"You're in. Create your {brand} account with this invite (it's tied to this email address):\n\n"
            f"{url}\n\nInvite code: {code}\nIt expires in {expires_days} days.")
    body = ("<p>You're in. Your invite is tied to this email address.</p>" + _button(url, "Create my account")
            + f"<p>Invite code: <strong>{html_mod.escape(code)}</strong><br>It expires in {expires_days} days.</p>")
    paras = [f"You're in. Create your {brand} account with this invite; it's tied to this email address.",
             f"Invite code: {code}. It expires in {expires_days} days."]
    return send(to, subject, text, _wrap("Your invite is here", body), paragraphs=paras,
                button=("Create my account", url))
