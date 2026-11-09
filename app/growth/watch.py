"""The motion sensor: notice when a watched creator uploads, and cut it at once.

Each feed is polled through YouTube's free per-channel Atom feed
(youtube.com/feeds/videos.xml), which lists a creator's latest uploads with no API quota. A handle
or custom URL is resolved to its channel id once and remembered. The first time a feed is seen the
newest upload only sets the watermark -- nothing is cut, so the back catalogue is left to supply --
and after that any upload newer than the watermark is queued immediately with the channel's id, so a
fresh video is clipped within the poll interval rather than waiting on the hourly supply run.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.models import GrowthState

from . import channels, learn, safety

log = logging.getLogger(__name__)

_RSS = "https://www.youtube.com/feeds/videos.xml"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_UA = "Mozilla/5.0 (compatible; DoomscrollerBot/1.0)"
_CHANNEL_ID = re.compile(r"/channel/(UC[0-9A-Za-z_-]{20,})")
_HANDLE_ID = re.compile(r'"(?:channelId|externalId)":"(UC[0-9A-Za-z_-]{20,})"')
_PLAYLIST = re.compile(r"[?&]list=([0-9A-Za-z_-]+)")
_FRESH_WINDOW = timedelta(hours=6)  # only react to genuinely new uploads, never an old one surfacing
_SEEN_KEEP = 60
_TIMEOUT = 15
_PER_POLL = 3  # at most this many fresh uploads queued per feed per poll, so a burst cannot flood the shelf


def _fetch(url: str) -> Optional[str]:
    """The page or feed body, or None on any network trouble. Never raises."""
    try:
        response = httpx.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        log.info("watch: could not fetch %s: %s", url, type(exc).__name__)
        return None


def _rss_url(feed_url: str) -> Optional[str]:
    """The Atom feed URL for a channel or playlist link, resolving a handle to its channel id once."""
    channel = _CHANNEL_ID.search(feed_url)
    if channel:
        return f"{_RSS}?channel_id={channel.group(1)}"
    playlist = _PLAYLIST.search(feed_url)
    if playlist:
        return f"{_RSS}?playlist_id={playlist.group(1)}"
    page = _fetch(feed_url)
    if not page:
        return None
    found = _HANDLE_ID.search(page) or _CHANNEL_ID.search(page)
    return f"{_RSS}?channel_id={found.group(1)}" if found else None


def _entries(xml: str) -> list[tuple[str, datetime]]:
    """(video id, published) for each upload in the Atom feed, newest first. A broken feed yields nothing."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []
    out = []
    for entry in root.findall(f"{_ATOM}entry"):
        vid = entry.findtext(f"{_YT}videoId")
        published = entry.findtext(f"{_ATOM}published")
        if not vid or not published:
            continue
        try:
            when = datetime.fromisoformat(published)
        except ValueError:
            continue
        out.append((vid, when if when.tzinfo else when.replace(tzinfo=timezone.utc)))
    return sorted(out, key=lambda item: item[1], reverse=True)


def _state_key(feed_url: str) -> str:
    return f"watch:{feed_url}"


def _watch_feed(channel, feed_url: str, now: datetime) -> list[str]:
    """Queue any upload newer than the watermark. Returns the video links queued (empty is the normal case)."""
    from app.clipper.job import enqueue

    rss = _rss_url(feed_url)
    if not rss:
        return []
    body = _fetch(rss)
    if not body:
        return []
    entries = _entries(body)
    if not entries:
        return []
    key = _state_key(feed_url)
    with session_scope() as db:
        state = db.get(GrowthState, key)
        saved = dict(state.value) if state else {}
        seen = set(saved.get("seen", []))
        last = datetime.fromisoformat(saved["last"]) if saved.get("last") else None
        blocked = safety.blocked_sources(db)

    queued, newly_seen = [], []
    first_sight = last is None
    for vid, when in entries:
        newly_seen.append(vid)
        link = f"https://www.youtube.com/watch?v={vid}"
        fresh = not first_sight and when > last and vid not in seen and now - when <= _FRESH_WINDOW
        if fresh and link not in blocked and len(queued) < _PER_POLL:
            enqueue(link, count=settings.growth_clips_per_source,
                    auto_queue=not channels.require_approval(channel),
                    feed=feed_url, channel_id=channels.event_channel(channel))
            queued.append(link)

    watermark = max(when for _, when in entries)
    with session_scope() as db:
        learn.set_state(db, key, {
            "last": max(watermark, last).isoformat() if last else watermark.isoformat(),
            "seen": (newly_seen + list(seen))[:_SEEN_KEEP],
            "channel": channel.slug,
        })
    return queued


def watch_job(now: Optional[datetime] = None) -> dict:
    """Poll every watched feed for fresh uploads and queue them at once."""
    if not settings.growth_enabled or settings.growth_watch_minutes <= 0:
        return {"skipped": "watching is off"}
    now = now or datetime.now(timezone.utc)
    with session_scope() as db:
        active = channels.active_channels(db)
        feeds = [(c, url) for c in active for url in channels.feeds_for(db, c)]
    queued = 0
    for channel, feed_url in feeds:
        try:
            queued += len(_watch_feed(channel, feed_url, now))
        except Exception as exc:  # one bad feed must not stop the rest
            log.warning("watch: %s failed: %s", feed_url, exc)
    return {"feeds": len(feeds), "queued": queued}
