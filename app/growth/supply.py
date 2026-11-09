"""Keep finished clips in stock, so a posting window never finds the shelf empty."""
from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from itertools import takewhile
from pathlib import Path
from typing import Iterator, Optional, Sequence

from sqlalchemy import func, select

from app.clipper.ingest import file_fingerprint, network_options
from app.clipper.job import enqueue
from app.config import settings
from app.db import session_scope
from app.models import ClipJob, GrowthEvent

from . import channels, learn, safety
from .autopilot import ready_clips
from .approval import awaiting_review
from .factors import aware

log = logging.getLogger(__name__)

_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_SETTLE_SECONDS = 60  # a file still being copied in must not be cut yet
_FEED_DEPTHS = (15, 60, 240, 1000)
_AUTHOR_KEYS = ("uploader_id", "channel_id", "uploader", "channel")
_CHANNEL_HOME = re.compile(r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|(?:channel|c|user)/[^/?#]+))/?(?:[?#].*)?$", re.I)
_MAX_RETRIES = 3
_RETRY_WAIT = timedelta(hours=6)
_EMPTY_FEED = "feed_empty"
_EMPTY_FEED_WINDOW = timedelta(days=7)
_RECENT_JOBS = 20
_ERROR_LENGTH = 200
ADD_SOURCES = "Add video files to the inbox or links to GROWTH_FEEDS."
SUPPLY_GAP = "supply_gap"


def _author_names(entry: dict) -> set[str]:
    return {str(entry[key]).casefold() for key in _AUTHOR_KEYS if entry.get(key)}


def _read_feed(feed: str, depth: int) -> dict:
    from yt_dlp import YoutubeDL

    options = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": depth, **network_options()}
    match = _CHANNEL_HOME.match(feed.strip())
    with YoutubeDL(options) as ydl:
        if not match:
            return ydl.extract_info(feed, download=False) or {}
        # The home page also brings in Shorts and old streams, which are not worth cutting.
        try:
            return ydl.extract_info(f"{match[1]}/videos", download=False) or {}
        except Exception as exc:
            log.info("growth: reading all of %s: %s", feed, exc)  # some channels have no Videos tab
            return ydl.extract_info(feed, download=False) or {}


def _links_in(info: dict, blocked: set[str], barred: bool = False) -> Iterator[tuple[str, bool]]:
    """Every video link under a feed, and whether its author is blocked. A channel's tabs are playlists with no url."""
    barred = barred or bool(_author_names(info) & blocked)
    for entry in info.get("entries") or []:
        if not entry:
            continue
        if entry.get("entries") is not None:
            yield from _links_in(entry, blocked, barred)
        elif str(entry.get("url", "")).startswith("http"):
            yield entry["url"], barred or bool(_author_names(entry) & blocked)


def _note_empty_feed(feed: str) -> None:
    log.warning("growth: feed %s lists no videos", feed)
    with session_scope() as db:
        learn.note_once(db, _EMPTY_FEED, f"{feed} lists no videos, so it supplies nothing. Check the link.",
                        key=feed, within=_EMPTY_FEED_WINDOW)


def _forget_empty_feed(feed: str) -> None:
    """It has answered since, so doctor must not fail it for a week and nothing is left for the owner to fix."""
    with session_scope() as db:
        for event in db.execute(select(GrowthEvent).where(GrowthEvent.kind == _EMPTY_FEED)).scalars():
            if (event.data or {}).get("key") == feed:
                db.delete(event)


def empty_feeds(db) -> set[str]:
    """Feeds that came back with no videos lately."""
    since = datetime.now(timezone.utc) - _EMPTY_FEED_WINDOW
    return {(data or {}).get("key", "") for data in db.execute(
        select(GrowthEvent.data).where(GrowthEvent.kind == _EMPTY_FEED, GrowthEvent.created_at >= since)).scalars()}


def _feed_links(feed: str) -> Iterator[str]:
    """Uploads of a channel or playlist, newest first, minus any author with a takedown. Nothing is downloaded here.

    Reads further back only when the caller has used everything so far, so a long back catalogue is reachable
    without listing it every hour.
    """
    given: set[str] = set()
    for depth in _FEED_DEPTHS:
        info = _read_feed(feed, depth)
        with session_scope() as db:
            blocked = safety.blocked_authors(db)
        if _author_names(info) & blocked:  # a channel's entries often name no author, but the channel itself does
            log.warning("growth: skipping feed %s, its author has a takedown block", feed)
            _forget_empty_feed(feed)
            return
        found = list(_links_in(info, blocked))
        if found:
            _forget_empty_feed(feed)
        for link, barred in found:
            if not barred and link not in given:
                given.add(link)
                yield link
        if not found:
            _note_empty_feed(feed)
        if len(found) < depth:
            return


def first_link(feed: str) -> Optional[str]:
    """Any video this feed would supply. Raises if the feed cannot be read at all."""
    return next(_feed_links(feed), None)


def lists_nothing(feed: str) -> bool:
    """After first_link found no link: true if the feed itself is empty, false if a takedown block took everything."""
    with session_scope() as db:
        return feed in empty_feeds(db)


def _inbox_files() -> list[str]:
    inbox = settings.growth_inbox_path
    if not inbox.is_dir():
        return []
    videos = []
    for path in inbox.iterdir():
        try:
            if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES:
                videos.append((path.stat().st_mtime, path))
        except OSError as exc:  # gone since the listing, or a link to nothing
            log.warning("growth: skipping %s in the inbox: %s", path.name, exc)
    now = time.time()
    return [str(path.resolve()) for mtime, path in sorted(videos, key=lambda item: item[0])
            if now - mtime > _SETTLE_SECONDS]


def _job_history(db, scope=None) -> dict[str, list]:
    stmt = select(ClipJob.source, ClipJob.status, ClipJob.result, ClipJob.finished_at, ClipJob.created_at)
    if scope is not None:
        stmt = stmt.where(scope)
    history = defaultdict(list)
    for row in db.execute(stmt):
        history[row.source].append(row)
    return history


def can_take(jobs: Sequence, now: datetime) -> bool:
    """Done, in progress or gone for good closes a source. A failure that may clear up is retried, waiting longer each time."""
    if any(job.status != "failed" or (job.result or {}).get("permanent") for job in jobs):
        return False
    if not jobs:
        return True
    if len(jobs) > _MAX_RETRIES:
        return False
    last = max(aware(job.finished_at or job.created_at) for job in jobs)
    return now - last >= len(jobs) * _RETRY_WAIT


def _can_take_file(path: str, jobs: Sequence, now: datetime) -> bool:
    try:
        fingerprint = file_fingerprint(Path(path))
    except OSError:
        return False
    # A job with no fingerprint predates them, so it can only go by the path.
    return can_take([job for job in jobs if (job.result or {}).get("fingerprint") in (None, fingerprint)], now)


def open_inbox_files() -> list[str]:
    """Inbox files that supply would cut right now."""
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        history = _job_history(db)
        blocked = safety.blocked_sources(db)
    return [path for path in _inbox_files()
            if path not in blocked and _can_take_file(path, history.get(path, ()), now)]


def _feeds_by_turn(history: dict[str, list], feeds: Optional[Sequence[str]] = None) -> list[str]:
    """Fewest jobs first, so every source is in stock from the start and can be compared."""
    feeds = settings.growth_feeds_list if feeds is None else list(feeds)
    taken = Counter((job.result or {}).get("feed") for jobs in history.values() for job in jobs)
    return sorted(feeds, key=lambda feed: taken[feed])


def next_source(unread: Optional[dict[str, str]] = None) -> Optional[tuple[str, Optional[str]]]:
    """The next source to cut, and the feed it came from. Your own files come ahead of feeds.

    Feeds that could not be read go in `unread`, so a caller can tell that from having nothing new.
    """
    files = open_inbox_files()
    if files:
        return files[0], None
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        history = _job_history(db)
        blocked = safety.blocked_sources(db)
    for feed in _feeds_by_turn(history):
        try:
            for link in _feed_links(feed):
                if link not in blocked and can_take(history.get(link, ()), now):
                    return link, feed
        except Exception as exc:
            log.warning("growth: could not read feed %s: %s", feed, exc)
            if unread is not None:
                unread[feed] = str(exc)
    return None


def _clipjob_scope(channel_id: Optional[str]):
    return ClipJob.channel_id.is_(None) if channel_id is None else ClipJob.channel_id == channel_id


def next_source_for(channel, unread: Optional[dict[str, str]] = None) -> Optional[tuple[str, Optional[str]]]:
    """The next source to cut for a named channel, from its own feeds only (the inbox feeds the legacy channel)."""
    now = datetime.now(timezone.utc)
    channel_id = channels.event_channel(channel)
    with session_scope() as db:
        history = _job_history(db, _clipjob_scope(channel_id))
        blocked = safety.blocked_sources(db)
        feeds = channels.feeds_for(db, channel)
    for feed in _feeds_by_turn(history, feeds):
        try:
            for link in _feed_links(feed):
                if link not in blocked and can_take(history.get(link, ()), now):
                    return link, feed
        except Exception as exc:
            log.warning("growth: could not read feed %s: %s", feed, exc)
            if unread is not None:
                unread[feed] = str(exc)
    return None


def _brief(error: Optional[str]) -> str:
    return " ".join((error or "no reason recorded").split())[:_ERROR_LENGTH]


def _failed_in_a_row(db) -> list:
    latest = db.execute(select(ClipJob.status, ClipJob.error).where(ClipJob.finished_at.is_not(None))
                        .order_by(ClipJob.finished_at.desc()).limit(_RECENT_JOBS))
    return list(takewhile(lambda job: job.status == "failed", latest))


def _why_starved(db, unread: dict[str, str]) -> tuple[str, str]:
    """What stopped supply, and the owner's next step. Adding more sources only helps when nothing is failing."""
    causes, steps = [], []
    if unread:
        causes += [f"could not read {feed}: {_brief(error)}" for feed, error in unread.items()]
        steps.append("Check the link and the connection, and update yt-dlp if it keeps failing.")
    failed = _failed_in_a_row(db)
    if failed:
        causes.append(f"the last {len(failed)} clip jobs failed, most recently: {_brief(failed[0].error)}")
        steps.append("See why on the Clip tab, and fix that before adding more sources.")
    return "; ".join(causes) or "no new source in the inbox or feeds", " ".join(steps) or ADD_SOURCES


def _supply_one(channel) -> dict:
    """Keep one managed channel's shelf stocked from its own feeds (and, for the legacy channel, the inbox)."""
    scope = channels.video_scope(channel)
    channel_id = channels.event_channel(channel)
    approval = channels.require_approval(channel)
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        # Clips waiting for the owner count as stock, or supply would keep cutting while they sit unreviewed.
        stock = len(ready_clips(db, scope)) + (len(awaiting_review(db, scope)) if approval else 0)
        active = db.scalar(select(func.count(ClipJob.id)).where(
            ClipJob.status.in_(("queued", "running")), _clipjob_scope(channel_id))) or 0
    if stock >= settings.growth_min_inventory:
        return {"stock": stock}
    if active:
        return {"stock": stock, "waiting": f"{active} clip jobs in progress"}
    with session_scope() as db:
        mined = learn.mine_waiting_winner(db, now, scope=scope, channel_id=channel_id, require_approval=approval)
    if mined:
        return {"stock": stock, "mined": mined}
    unread: dict[str, str] = {}
    picked = next_source(unread) if channels.is_default(channel) else next_source_for(channel, unread)
    gap_key = channels.state_key(channel, SUPPLY_GAP)
    if picked is None:
        with session_scope() as db:
            blocks = safety.block_list(db)
            blocked = f". Blocked: {safety.describe_blocks(blocks)}" if blocks else ""
            cause, step = _why_starved(db, unread)
            starved = f"{cause}{blocked}"
            # Keyed by the step, so a different kind of cause is news within the day but a reworded error is not.
            learn.note_once(db, "starved", f"Supply has nothing new to cut, with {stock} clips left: {starved}",
                            key=channels.state_key(channel, step), channel_id=channel_id)
            learn.set_state(db, gap_key, {"cause": cause, "step": step})
        return {"stock": stock, "starved": starved}
    with session_scope() as db:
        learn.set_state(db, gap_key, {})
    source, feed = picked
    job_id = enqueue(source, count=settings.growth_clips_per_source,
                     auto_queue=not approval, feed=feed, channel_id=channel_id)
    return {"stock": stock, "queued": source, "job": job_id}


def supply_job() -> dict:
    if not settings.growth_enabled:
        return {"skipped": "GROWTH_ENABLED is off"}
    with session_scope() as db:
        active = channels.active_channels(db)
    # A single legacy channel keeps the old flat result shape; several report per channel by slug.
    if len(active) == 1 and channels.is_default(active[0]):
        return _supply_one(active[0])
    return {channel.slug: _supply_one(channel) for channel in active}
