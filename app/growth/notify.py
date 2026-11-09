"""Phone alerts: clips waiting for review, Shorts posted, and problems.

Two free channels, either or both: ntfy (install the ntfy app and subscribe to NTFY_TOPIC, no
account) and a Telegram bot (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID). The job sends one
digest per run of whatever happened since the last one. The review link carries no key: the
phone keeps the sign-in cookie from the first visit, and a notification server should never
hold the password.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy import select

from app.config import PROJECT_ROOT, settings
from app.db import session_scope
from app.models import GrowthEvent, GrowthState, Video, VideoStatus

log = logging.getLogger(__name__)

_STATE = "notified"
TUNNEL_URL_FILE = PROJECT_ROOT / "logs" / "tunnel_url.txt"
_ALERT_KINDS = ("strike", "paused", "account_hold", "publish_failed", "publish_stuck", "missing", "starved", "missed")
_MAX_LINES = 12


def configured() -> bool:
    return bool(settings.ntfy_topic or (settings.telegram_bot_token and settings.telegram_chat_id))


def review_link() -> str:
    try:
        return TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def send(text: str, *, title: str = "seduxing", link: str = "") -> bool:
    """Deliver to every configured channel. Never raises, and never logs a token-bearing URL."""
    sent = False
    if settings.ntfy_topic:
        headers = {"Title": title}
        if link:
            headers["Click"] = link
        try:
            httpx.post(f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}", content=text.encode(),
                       headers=headers, timeout=15).raise_for_status()
            sent = True
        except httpx.HTTPError as exc:
            log.warning("notify: ntfy failed: %s", type(exc).__name__)
    if settings.telegram_bot_token and settings.telegram_chat_id:
        body = f"{text}\n{link}".strip() if link else text
        try:
            httpx.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                       data={"chat_id": settings.telegram_chat_id, "text": body,
                             "disable_web_page_preview": "true"}, timeout=15).raise_for_status()
            sent = True
        except httpx.HTTPError as exc:
            # The token travels in the URL, so only the kind of failure is logged.
            log.warning("notify: telegram failed: %s", type(exc).__name__)
    return sent


def _since(db) -> datetime:
    state = db.get(GrowthState, _STATE)
    if state and state.value.get("at"):
        return datetime.fromisoformat(state.value["at"])
    return datetime.now(timezone.utc)  # first run: only news from now on


def digest(db, since: datetime) -> list[str]:
    lines: list[str] = []
    waiting = db.execute(select(Video).where(Video.status == VideoStatus.PENDING,
                                             Video.scraped_at > since)).scalars().all()
    fresh = [v for v in waiting if (v.meta or {}).get("clip_rendered")]
    if fresh:
        lines.append(f"{len(fresh)} new clip{'s' if len(fresh) != 1 else ''} waiting for your approval")
    events = db.execute(select(GrowthEvent).where(GrowthEvent.created_at > since)
                        .order_by(GrowthEvent.created_at)).scalars().all()
    for event in events:
        if event.kind == "posted" and event.video_id:
            video = db.get(Video, event.video_id)
            if video and video.youtube_video_id:
                lines.append(f"Posted: {(video.title or '')[:70]} https://youtube.com/shorts/{video.youtube_video_id}")
        elif event.kind in _ALERT_KINDS:
            lines.append(f"Problem: {event.summary[:200]}")
    return lines[:_MAX_LINES]


def notify_job(now: Optional[datetime] = None) -> dict:
    if not configured():
        return {"skipped": "no NTFY_TOPIC or Telegram bot set"}
    now = now or datetime.now(timezone.utc)
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
