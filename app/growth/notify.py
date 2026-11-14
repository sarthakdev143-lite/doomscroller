"""Phone alerts: clips waiting for review, Shorts posted, and problems.

Two free channels, either or both: ntfy (install the ntfy app and subscribe to NTFY_TOPIC, no
account) and a Telegram bot (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID). The job sends one
digest per run of whatever happened since the last one. The review link carries no key: the
phone keeps the sign-in cookie from the first visit, and a notification server should never
hold the password.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.config import PROJECT_ROOT, settings
from app.db import session_scope
from app.models import GrowthEvent, GrowthState, Video, VideoStatus

from .factors import aware

log = logging.getLogger(__name__)

_STATE = "notified"
TUNNEL_URL_FILE = PROJECT_ROOT / "logs" / "tunnel_url.txt"
_ALERT_KINDS = ("strike", "paused", "account_hold", "publish_failed", "publish_stuck", "missing", "starved", "missed")
_MAX_LINES = 12
_MAX_BODY_CHARS = 3500
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_QUERY_SECRET_RE = re.compile(r"([?&](?:key|token|password|secret)=)[^&#\s]+", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\r\n\x00-\x1f\x7f]+")


def configured() -> bool:
    return bool(settings.ntfy_topic.strip() or (settings.telegram_bot_token.strip() and settings.telegram_chat_id.strip()))


def _redact(text: str) -> str:
    """Scrub Telegram bot tokens and review passwords before any text leaves or is logged."""
    if not text:
        return ""
    out = text
    bot_tok = settings.telegram_bot_token.strip()
    if bot_tok:
        out = out.replace(bot_tok, "[REDACTED]")
        if ":" in bot_tok:
            suffix = bot_tok.split(":", 1)[1]
            if len(suffix) >= 4:
                out = out.replace(suffix, "[REDACTED]")
    rev_tok = settings.review_token.strip()
    if rev_tok:
        out = out.replace(rev_tok, "[REDACTED]")
    out = _BOT_TOKEN_RE.sub("bot[REDACTED]", out)
    out = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", out)
    return out


def _clean_header(value: str, fallback: str = "") -> str:
    """HTTP/1.1 header values must be single-line Latin-1 without control bytes or secrets."""
    cleaned = _CTRL_RE.sub(" ", _redact(value or ""))
    latin = cleaned.encode("latin-1", "ignore").decode("latin-1").strip()
    return latin[:200] or fallback


def review_link() -> str:
    """Public HTTPS/HTTP review URL stripped of any userinfo, query keys, or fragments."""
    try:
        raw = TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{port}" if port else host
    path = _CTRL_RE.sub("", parts.path or "")
    return _redact(f"{parts.scheme}://{netloc}{path}")


def _deliver_ntfy(text: str, *, title: str, link: str) -> tuple[bool, str]:
    headers = {"Title": _clean_header(title, "Doomscroller")}
    safe_link = _clean_header(link)
    if safe_link:
        headers["Click"] = safe_link
    body = _redact(text)[:_MAX_BODY_CHARS]
    try:
        httpx.post(
            f"{settings.ntfy_server.strip().rstrip('/')}/{settings.ntfy_topic.strip()}",
            content=body.encode("utf-8"),
            headers=headers,
            timeout=15,
        ).raise_for_status()
        return True, ""
    except Exception as exc:
        err = _redact(type(exc).__name__)
        log.warning("notify: ntfy failed: %s", err)
        return False, err


def _deliver_telegram(text: str, *, link: str) -> tuple[bool, str]:
    safe_link = _clean_header(link)
    safe_text = _redact(text)
    body = (f"{safe_text}\n{safe_link}".strip() if safe_link else safe_text)[:_MAX_BODY_CHARS]
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token.strip()}/sendMessage",
            data={
                "chat_id": settings.telegram_chat_id.strip(),
                "text": body,
                "disable_web_page_preview": "true",
            },
            timeout=15,
        ).raise_for_status()
        return True, ""
    except Exception as exc:
        # The token travels in the URL, so only the exception class name is logged or returned.
        err = _redact(type(exc).__name__)
        log.warning("notify: telegram failed: %s", err)
        return False, err


def send(text: str, *, title: str = "", link: str = "") -> bool:
    """Deliver to every configured channel. Never raises, and never logs a token-bearing URL."""
    resolved_title = title or settings.brand_name.lstrip("@") or "Doomscroller"
    sent = False
    if settings.ntfy_topic.strip():
        ok, _ = _deliver_ntfy(text, title=resolved_title, link=link)
        sent = sent or ok
    if settings.telegram_bot_token.strip() and settings.telegram_chat_id.strip():
        ok, _ = _deliver_telegram(text, link=link)
        sent = sent or ok
    return sent


def send_test_notification(text: str = "", *, title: str = "") -> dict:
    """Send a one-shot verification alert and report per-channel delivery status safely."""
    if not configured():
        return {"ok": False, "configured": False, "channels": {}, "skipped": "no NTFY_TOPIC or Telegram bot set"}
    msg = text or "Test alert from Doomscroller — phone notifications are working."
    resolved_title = title or settings.brand_name.lstrip("@") or "Doomscroller"
    link = review_link()
    results: dict[str, dict] = {}
    if settings.ntfy_topic.strip():
        ok, err = _deliver_ntfy(msg, title=resolved_title, link=link)
        results["ntfy"] = {"ok": True} if ok else {"ok": False, "error": err}
    if settings.telegram_bot_token.strip() and settings.telegram_chat_id.strip():
        ok, err = _deliver_telegram(msg, link=link)
        results["telegram"] = {"ok": True} if ok else {"ok": False, "error": err}
    return {"ok": any(ch["ok"] for ch in results.values()), "configured": True, "channels": results}


def _since(db) -> datetime:
    state = db.get(GrowthState, _STATE)
    if state and (state.value or {}).get("at"):
        try:
            return aware(datetime.fromisoformat(state.value["at"]))
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)  # first run: only news from now on


def digest(db, since: datetime) -> list[str]:
    from . import channels

    since = aware(since)
    names = {c.id: (c.name or c.slug) for c in channels.all_channels(db)}
    # Which channel an event belongs to, so an alert among several channels is actionable.
    tag = lambda ev: f"[{names[ev.channel_id]}] " if ev.channel_id in names else ""
    waiting_lines: list[str] = []
    waiting = db.execute(select(Video).where(Video.status == VideoStatus.PENDING,
                                             Video.scraped_at > since)).scalars().all()
    fresh = [v for v in waiting if (v.meta or {}).get("clip_rendered")]
    if fresh:
        split = Counter(names[v.channel_id] for v in fresh if v.channel_id in names)
        where = f" ({', '.join(f'{name} {n}' for name, n in split.most_common())})" if split else ""
        waiting_lines.append(_redact(f"{len(fresh)} new clip{'s' if len(fresh) != 1 else ''} waiting for your approval{where}"))
    events = db.execute(
        select(GrowthEvent)
        .where(GrowthEvent.created_at > since, GrowthEvent.kind.in_(("posted", *_ALERT_KINDS)))
        .order_by(GrowthEvent.created_at, GrowthEvent.id)
    ).scalars().all()
    chronological: list[tuple[str, str]] = []
    for event in events:
        if event.kind == "posted" and event.video_id:
            video = db.get(Video, event.video_id)
            yt_id = (video.youtube_video_id or "").strip() if video else ""
            if video and yt_id:
                clean_title = " ".join((video.title or "").split())[:70]
                chronological.append((
                    "posted",
                    _redact(f"{tag(event)}Posted: {clean_title} https://youtube.com/shorts/{yt_id}"),
                ))
        elif event.kind in _ALERT_KINDS:
            clean_summary = " ".join((event.summary or "").split())[:200]
            chronological.append(("problem", _redact(f"{tag(event)}Problem: {clean_summary}")))

    total = len(waiting_lines) + len(chronological)
    if total <= _MAX_LINES:
        return waiting_lines + [line for _, line in chronological]
    # When more updates happen than fit in one alert, problems never lose their spot to routine posts.
    budget = _MAX_LINES - 1
    problems = [line for cat, line in chronological if cat == "problem"]
    posts = [line for cat, line in chronological if cat == "posted"]
    kept = (waiting_lines + problems + posts)[:budget]
    omitted = total - len(kept)
    return [*kept, f"... and {omitted} more update{'s' if omitted != 1 else ''}"]


def notify_job(now: Optional[datetime] = None) -> dict:
    if not configured():
        return {"skipped": "no NTFY_TOPIC or Telegram bot set"}
    now = aware(now) if now is not None else datetime.now(timezone.utc)
    with session_scope() as db:
        since = _since(db)
        lines = digest(db, since)
    if lines and not send("\n".join(lines), link=review_link()):
        return {"error": "could not deliver the alert"}  # keep the window, so the next run retries it
    with session_scope() as db:
        state = db.get(GrowthState, _STATE)
        if state:
            state.value = {"at": now.isoformat()}
        else:
            db.add(GrowthState(key=_STATE, value={"at": now.isoformat()}))
    return {"sent": len(lines)}
