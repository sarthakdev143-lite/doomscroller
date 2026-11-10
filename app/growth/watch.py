"""The motion sensor: notice when a watched creator uploads, and cut it at once.

Each feed is polled through YouTube's free per-channel Atom feed
(youtube.com/feeds/videos.xml), which lists a creator's latest uploads with no API quota. A handle
or custom URL is resolved to its channel id once and remembered. The first time a feed is seen its
current uploads are only recorded -- nothing is cut, so the back catalogue is left to supply -- and
after that any new upload inside the freshness window is queued immediately with the channel's id,
so a fresh video is clipped within the poll interval rather than waiting on the hourly supply run.

The Atom feed also lists Shorts, live streams and premieres that have not aired, none of which
supply would take (it reads the Videos tab). So each candidate is looked at first: a Short or a
stream is skipped for good, and a video that is live or not yet aired is left unseen and checked
again later, because the clipper fails those permanently and would lock the source out.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.db import session_scope
from app.models import ClipJob, GrowthState

from . import channels, learn, safety

log = logging.getLogger(__name__)

_RSS = "https://www.youtube.com/feeds/videos.xml"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_UA = "Mozilla/5.0 (compatible; DoomscrollerBot/1.0)"
_CHANNEL_URL = re.compile(r"/channel/(UC[0-9A-Za-z_-]{20,})")
# The page's own id first: "channelId" also appears for other channels the page links to.
_PAGE_IDS = (re.compile(r'"externalId":"(UC[0-9A-Za-z_-]{20,})"'),
             re.compile(r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{20,})"'),
             re.compile(r'"channelId":"(UC[0-9A-Za-z_-]{20,})"'))
_PLAYLIST = re.compile(r"[?&]list=([0-9A-Za-z_-]+)")
_FRESH_WINDOW = timedelta(hours=6)  # only react to genuinely new uploads, never an old one surfacing
_RECHECK_AFTER = timedelta(minutes=20)  # how often a not-yet-aired video is looked at again
_SEEN_KEEP = 60
_TIMEOUT = 15
_PER_POLL = 3  # at most this many fresh uploads queued per feed per poll, so a burst cannot flood the shelf
_STREAMS = ("was_live", "post_live")
_NOT_AIRED = ("is_live", "is_upcoming")


def _fetch(url: str) -> Optional[str]:
    """The page or feed body, or None on any network trouble. Never raises."""
    try:
        response = httpx.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        log.info("watch: could not fetch %s: %s", url, type(exc).__name__)
        return None


def _channel_id_via_ytdlp(feed_url: str) -> Optional[str]:
    """The slower, sturdier way to name a channel, for when its page does not give the id away."""
    try:
        from yt_dlp import YoutubeDL

        from app.clipper.ingest import network_options

        options = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 1, **network_options()}
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(feed_url, download=False) or {}
        found = info.get("channel_id") or ""
        return found if found.startswith("UC") else None
    except Exception as exc:
        log.info("watch: could not resolve %s: %s", feed_url, exc)
        return None


def _rss_url(feed_url: str) -> Optional[str]:
    """The Atom feed URL for a channel or playlist link. Costs a request unless the link carries the id."""
    channel = _CHANNEL_URL.search(feed_url)
    if channel:
        return f"{_RSS}?channel_id={channel.group(1)}"
    playlist = _PLAYLIST.search(feed_url)
    if playlist:
        return f"{_RSS}?playlist_id={playlist.group(1)}"
    page = _fetch(feed_url) or ""
    for pattern in _PAGE_IDS:
        found = pattern.search(page)
        if found:
            return f"{_RSS}?channel_id={found.group(1)}"
    resolved = _channel_id_via_ytdlp(feed_url)
    return f"{_RSS}?channel_id={resolved}" if resolved else None


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


def _probe(link: str) -> Optional[dict]:
    """What kind of upload this is: {'duration', 'live_status'}. None when it could not be looked at."""
    try:
        from yt_dlp import YoutubeDL

        from app.clipper.ingest import network_options

        options = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                   **network_options()}
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(link, download=False, process=False) or {}
    except Exception as exc:
        log.info("watch: could not look at %s: %s", link, exc)
        return None
    live = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
    return {"duration": info.get("duration") or 0, "live_status": live}


def _verdict(probe: Optional[dict]) -> str:
    """'cut', 'skip' (never worth cutting) or 'later' (look again: not aired yet, or could not be read)."""
    if probe is None or probe["live_status"] in _NOT_AIRED:
        return "later"
    if probe["live_status"] in _STREAMS:
        return "skip"  # supply reads the Videos tab, which leaves stream recordings out too
    if probe["duration"] and probe["duration"] <= settings.growth_watch_min_seconds:
        return "skip"  # a Short, or too little footage to cut clips from
    return "cut"


def _state_key(feed_url: str) -> str:
    return "watch:" + hashlib.sha1(feed_url.encode()).hexdigest()[:20]


def _already_queued(db, link: str) -> bool:
    return bool(db.scalar(select(func.count(ClipJob.id)).where(ClipJob.source == link)))


def _watch_feed(channel, feed_url: str, now: datetime) -> list[str]:
    """Queue this feed's new uploads. Returns the video links queued (empty is the normal case)."""
    from app.clipper.job import enqueue

    key = _state_key(feed_url)
    with session_scope() as db:
        state = db.get(GrowthState, key)
        saved = dict(state.value) if state else {}
        blocked = safety.blocked_sources(db)
    rss = saved.get("rss") or _rss_url(feed_url)
    if not rss:
        return []
    body = _fetch(rss)
    entries = _entries(body) if body else []
    if not entries:
        if rss != saved.get("rss"):  # keep the resolved feed even when it answers nothing this time
            with session_scope() as db:
                learn.set_state(db, key, {**saved, "rss": rss, "feed": feed_url})
        return []

    first_sight = "seen" not in saved
    seen = list(saved.get("seen", []))
    rechecks = dict(saved.get("rechecks", {}))
    queued: list[str] = []
    for vid, when in entries:
        if vid in seen:
            continue
        if first_sight or now - when > _FRESH_WINDOW:
            seen.append(vid)  # the back catalogue is supply's job
            continue
        link = f"https://www.youtube.com/watch?v={vid}"
        last_try = rechecks.get(vid)
        if last_try and now - datetime.fromisoformat(last_try) < _RECHECK_AFTER:
            continue
        if len(queued) >= _PER_POLL:
            continue  # stays unseen, so the next poll takes it
        verdict = "skip" if link in blocked else _verdict(_probe(link))
        if verdict == "later":
            rechecks[vid] = now.isoformat()
            continue
        if verdict == "cut":
            with session_scope() as db:
                taken = _already_queued(db, link)
            if not taken:
                enqueue(link, count=settings.growth_clips_per_source,
                        auto_queue=not channels.require_approval(channel),
                        feed=feed_url, channel_id=channels.event_channel(channel))
                queued.append(link)
        seen.append(vid)
        rechecks.pop(vid, None)

    listed = {vid for vid, _ in entries}
    with session_scope() as db:
        learn.set_state(db, key, {
            "feed": feed_url, "rss": rss, "channel": channel.slug,
            "seen": seen[-_SEEN_KEEP:],
            "rechecks": {vid: at for vid, at in rechecks.items() if vid in listed},
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
