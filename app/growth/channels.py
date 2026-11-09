"""The managed channels: one niche per channel, each its own Cloud project, token and pace.

The declarative source is ``channels.json`` (its path from ``CHANNELS_FILE``). On sync it is
upserted into the ``channels`` and ``feeds`` tables, which are the runtime source of truth. When
no file is present the loop falls back to a single channel built from the ``GROWTH_*`` settings,
``YOUTUBE_TOKEN_PATH`` and ``GROWTH_FEEDS`` -- so an existing single-channel deployment is unchanged.

Everything downstream (supply, autopilot, collect, learn, notify) runs once per enabled channel and
scopes its work by ``channel_id``. Per-channel growth state is namespaced with :func:`state_key`.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select

from app.config import settings
from app.models import Channel, Feed

log = logging.getLogger(__name__)

DEFAULT_SLUG = "default"
# channels.json fields that map straight onto a Channel column.
_CONFIG_FIELDS = ("name", "niche", "brand_name", "token_path", "client_secrets",
                  "uploads_per_day", "timezone", "require_approval", "enabled")


def _load_config() -> Optional[list[dict]]:
    """The channels declared in channels.json, or None when there is no usable file."""
    path = settings.channels_path
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("growth: channels.json is not valid JSON (%s), ignoring it", exc)
        return None
    entries = data.get("channels") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        log.warning("growth: channels.json has no 'channels' list, ignoring it")
        return None
    valid = [e for e in entries if isinstance(e, dict) and e.get("slug")]
    return valid or None


def _legacy_channel() -> dict:
    """One channel from the flat GROWTH_* settings, so a single-channel setup needs no channels.json."""
    return {
        "slug": DEFAULT_SLUG,
        "name": settings.brand_name or "default",
        "niche": settings.channel_niche,
        "brand_name": settings.brand_name,
        "token_path": settings.youtube_token_path,
        "client_secrets": settings.youtube_client_secrets,
        "uploads_per_day": settings.growth_uploads_per_day,
        "timezone": settings.growth_timezone,
        "require_approval": settings.growth_require_approval,
        "enabled": True,
        "feeds": settings.growth_feeds_list,
    }


def _apply(channel: Channel, entry: dict) -> None:
    for field in _CONFIG_FIELDS:
        if field in entry and entry[field] is not None:
            setattr(channel, field, entry[field])


def _sync_feeds(db, channel: Channel, urls: list[str]) -> None:
    """Make the channel's feed rows match the config: add new links, drop ones no longer listed."""
    wanted = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    existing = {f.url: f for f in db.execute(select(Feed).where(Feed.channel_id == channel.id)).scalars()}
    for url in wanted:
        feed = existing.pop(url, None)
        if feed is None:
            db.add(Feed(url=url, channel_id=channel.id, enabled=True))
        else:
            feed.enabled = True
    for leftover in existing.values():
        db.delete(leftover)


def sync_channels(db) -> list[Channel]:
    """Reconcile the channels/feeds tables with channels.json (or the legacy settings). Idempotent."""
    config = _load_config() or [_legacy_channel()]
    seen: set[str] = set()
    for entry in config:
        slug = str(entry["slug"]).strip()
        if not slug:
            continue
        seen.add(slug)
        channel = db.execute(select(Channel).where(Channel.slug == slug)).scalar_one_or_none()
        if channel is None:
            channel = Channel(slug=slug)
            db.add(channel)
        _apply(channel, entry)
        db.flush()  # need channel.id for the feed rows
        _sync_feeds(db, channel, entry.get("feeds") or [])
    # A channel dropped from the config is retired, not deleted: its videos and history stay.
    for channel in db.execute(select(Channel)).scalars():
        if channel.slug not in seen and channel.enabled:
            channel.enabled = False
    db.flush()
    return active_channels(db)


def _ensure_seeded(db) -> None:
    if db.scalar(select(Channel).limit(1)) is None:
        sync_channels(db)


def active_channels(db) -> list[Channel]:
    """Every enabled channel, seeding from config on first use."""
    _ensure_seeded(db)
    return list(db.execute(select(Channel).where(Channel.enabled.is_(True)).order_by(Channel.slug)).scalars())


def all_channels(db) -> list[Channel]:
    _ensure_seeded(db)
    return list(db.execute(select(Channel).order_by(Channel.slug)).scalars())


def get_channel(db, channel_id: Optional[str]) -> Optional[Channel]:
    if not channel_id:
        return None
    return db.get(Channel, channel_id)


def channel_by_slug(db, slug: str) -> Optional[Channel]:
    return db.execute(select(Channel).where(Channel.slug == slug)).scalar_one_or_none()


def default_channel(db) -> Optional[Channel]:
    """The channel an event or clip with no channel_id belongs to: the legacy one, else the first enabled."""
    _ensure_seeded(db)
    return (channel_by_slug(db, DEFAULT_SLUG)
            or db.execute(select(Channel).where(Channel.enabled.is_(True)).order_by(Channel.slug)).scalar())


def feeds_for(db, channel: Channel) -> list[str]:
    return list(db.execute(
        select(Feed.url).where(Feed.channel_id == channel.id, Feed.enabled.is_(True))).scalars())


def state_key(channel: Channel, name: str) -> str:
    """Namespace a GrowthState key by channel, so each channel keeps its own plan, focus and clocks."""
    return f"{channel.slug}:{name}"


def publisher_for(channel: Channel):
    """A publisher bound to this channel's token. Mock under USE_MOCK or when the channel has no Cloud project."""
    from app.publishers import MockPublisher, YouTubePublisher

    if settings.use_mock or not channel.client_secrets:
        return MockPublisher()
    return YouTubePublisher(token_path=channel.token_path)
