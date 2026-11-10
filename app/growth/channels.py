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
import os
import re
from collections import Counter
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.models import Channel, Feed, GrowthState

log = logging.getLogger(__name__)

DEFAULT_SLUG = "default"
_CONFIG_HEALTH = "channels_config"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# "error" is the key a multi-channel job result uses to say that some channel failed.
_RESERVED_SLUGS = frozenset({"error"})


class ChannelsConfigError(ValueError):
    """channels.json exists but could not be used, so the fleet must not be silently retired."""
# channels.json fields that map straight onto a Channel column.
_CONFIG_FIELDS = ("name", "niche", "brand_name", "token_path", "client_secrets",
                  "uploads_per_day", "timezone", "require_approval", "enabled",
                  "auto_approve_score", "cadence_backstop")


def _load_config() -> Optional[list[dict]]:
    """The channels declared in channels.json, or None when there is no file.

    Raises ChannelsConfigError when the file exists but is unusable, so a typo does not silently retire
    every channel (a trailing comma over SSH must not take the fleet down to the legacy default channel).
    """
    path = settings.channels_path
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None  # no file -> the legacy single-channel fallback is intended
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChannelsConfigError(f"channels.json is not valid JSON ({exc}). Fix the file; the fleet is unchanged.")
    entries = data.get("channels") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ChannelsConfigError("channels.json has no 'channels' list.")
    valid = [e for e in entries if isinstance(e, dict) and e.get("slug")]
    if not valid:
        raise ChannelsConfigError("channels.json declares no channel with a slug.")
    seen: set[str] = set()
    for entry in valid:
        problem = _entry_problem(entry, seen)
        if problem:
            raise ChannelsConfigError(f"channels.json, channel {entry.get('slug')!r}: {problem}. "
                                      "Fix the file; the fleet is unchanged.")
        seen.add(str(entry["slug"]).strip())
    return valid


def _entry_problem(entry: dict, seen: set[str]) -> Optional[str]:
    """Why one channel entry cannot be used, or None. A bad value is refused by name, never half-applied."""
    slug = str(entry["slug"]).strip()
    if not _SLUG.match(slug):
        return "the slug must be lowercase letters, digits, - or _"
    if slug in _RESERVED_SLUGS:
        return f"{slug!r} is a reserved name"
    if slug in seen:
        return "the slug is used twice"
    pace = entry.get("uploads_per_day")
    if pace is not None and (isinstance(pace, bool) or not isinstance(pace, int) or not 0 <= pace <= 50):
        return "uploads_per_day must be a whole number from 0 to 50"
    score = entry.get("auto_approve_score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1):
        return "auto_approve_score must be a number from 0 to 1"
    for flag in ("require_approval", "enabled", "cadence_backstop"):
        if entry.get(flag) is not None and not isinstance(entry[flag], bool):
            return f"{flag} must be true or false"
    zone = entry.get("timezone")
    if zone is not None:
        try:
            ZoneInfo(str(zone))
        except Exception:
            return f"unknown timezone {zone!r}"
    feeds = entry.get("feeds")
    if feeds is not None and (not isinstance(feeds, list) or not all(isinstance(f, str) for f in feeds)):
        return "feeds must be a list of links"
    return None


def _record_config(db, error: Optional[str], count: int = 0) -> None:
    value = {"error": error} if error else {"ok": True, "count": count}
    state = db.get(GrowthState, _CONFIG_HEALTH)
    if state:
        state.value = value
    else:
        db.add(GrowthState(key=_CONFIG_HEALTH, value=value))


def config_health(db) -> dict:
    """What the last sync made of channels.json: {ok, count} or {error}. Doctor surfaces a parse failure."""
    state = db.get(GrowthState, _CONFIG_HEALTH)
    return dict(state.value) if state else {"ok": True, "count": 0}


def duplicate_credentials(db) -> list[str]:
    """A token or client_secret path shared by more than one enabled channel — a copy-paste slip that would
    cross-post two niches to one account or double-spend a project's quota. Named channels only."""
    rows = [c for c in db.execute(select(Channel).where(Channel.enabled.is_(True))).scalars() if not is_default(c)]
    out = []
    for field in ("token_path", "client_secrets"):
        counts = Counter(getattr(c, field) for c in rows if getattr(c, field))
        for path, n in counts.items():
            if n > 1:
                slugs = ", ".join(sorted(c.slug for c in rows if getattr(c, field) == path))
                out.append(f"{field} {path} is shared by {slugs}")
    return out


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
        # Its feeds are read live from GROWTH_FEEDS (see feeds_for), not persisted as rows.
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
    """Reconcile the channels/feeds tables with channels.json (or the legacy settings). Idempotent.

    A broken channels.json leaves the existing channels in place rather than retiring the fleet.
    """
    try:
        loaded = _load_config()
    except ChannelsConfigError as exc:
        _record_config(db, str(exc))
        log.error("growth: %s", exc)
        # Keep the last-known-good channels; do not retire anything. Query directly (no re-seed/recursion).
        return list(db.execute(select(Channel).where(Channel.enabled.is_(True)).order_by(Channel.slug)).scalars())
    _record_config(db, None, count=len(loaded) if loaded else 1)
    config = loaded or [_legacy_channel()]
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
        # Listed means on unless the file says otherwise, so a channel put back after being retired runs again.
        channel.enabled = bool(entry.get("enabled", True))
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


def _read_file() -> dict:
    """channels.json as written, or an empty skeleton when there is no file yet."""
    try:
        raw = settings.channels_path.read_text(encoding="utf-8")
    except OSError:
        return {"channels": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChannelsConfigError(f"channels.json is not valid JSON ({exc}). Fix it by hand first.")
    if isinstance(data, list):
        data = {"channels": data}
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        raise ChannelsConfigError("channels.json has no 'channels' list.")
    return data


def edit_config(change: Callable[[list[dict]], None]) -> dict:
    """Change channels.json through code rather than by hand: validated, written whole, then applied.

    Nothing is written if any entry would be invalid, and the running loop picks the result up on its next tick.
    """
    data = _read_file()
    entries = data["channels"]
    change(entries)
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("slug"):
            raise ChannelsConfigError("every channel needs a slug")
        problem = _entry_problem(entry, seen)
        if problem:
            raise ChannelsConfigError(f"channel {entry.get('slug')!r}: {problem}")
        seen.add(str(entry["slug"]).strip())
    path = settings.channels_path
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return reload_channels()


def _entry(entries: list[dict], slug: str) -> dict:
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("slug", "")).strip() == slug:
            return entry
    known = ", ".join(str(e.get("slug")) for e in entries if isinstance(e, dict)) or "none"
    raise ChannelsConfigError(f"no channel {slug!r} in channels.json. Known: {known}.")


def add_channel(slug: str, *, name: str = "", niche: str = "", brand_name: str = "", feeds: tuple[str, ...] = (),
                uploads_per_day: int = 3, timezone: str = "") -> dict:
    """Declare a new channel, off until its token is in place. Its secret and token paths follow the slug."""
    def change(entries: list[dict]) -> None:
        if any(isinstance(e, dict) and str(e.get("slug", "")).strip() == slug for e in entries):
            raise ChannelsConfigError(f"channel {slug!r} already exists")
        if not entries and (settings.growth_feeds_list or settings.youtube_client_secrets):
            # A channel not listed is retired. The first channels.json must not switch off the channel
            # already running from .env, so it is listed too; remove this entry to retire it.
            entries.append({"slug": DEFAULT_SLUG})
        entries.append({
            "slug": slug, "name": name or slug, "niche": niche, "brand_name": brand_name,
            "client_secrets": f"secrets/{slug}_client_secret.json", "token_path": f"secrets/{slug}_token.json",
            "uploads_per_day": uploads_per_day, "timezone": timezone or settings.growth_timezone,
            "require_approval": True, "enabled": False, "feeds": list(feeds),
        })
    return edit_config(change)


def set_enabled(slug: str, enabled: bool) -> dict:
    def change(entries: list[dict]) -> None:
        _entry(entries, slug)["enabled"] = enabled
    return edit_config(change)


def change_feeds(slug: str, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> dict:
    def change(entries: list[dict]) -> None:
        entry = _entry(entries, slug)
        feeds = [f for f in entry.get("feeds") or [] if f not in remove]
        entry["feeds"] = feeds + [f for f in add if f not in feeds]
    return edit_config(change)


def reload_channels() -> dict:
    """Re-apply channels.json to the DB now. The running daemon reads channels fresh each tick, so an
    edit takes effect on the next tick without a restart (named channels only; GROWTH_* still needs a restart)."""
    from app.db import session_scope

    with session_scope() as db:
        active = sync_channels(db)
        health = config_health(db)
        slugs = [c.slug for c in active]
    return {**health, "active": slugs}


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


def is_default(channel: Channel) -> bool:
    return channel.slug == DEFAULT_SLUG


def for_each(active: list[Channel], run: Callable[[Channel], dict]) -> dict:
    """Run a job once per channel and report by slug. One channel failing must not stop the others,
    and the result carries a top-level "error" naming any that did, so the scheduler records the run as failed."""
    out: dict = {}
    for channel in active:
        try:
            out[channel.slug] = run(channel)
        except Exception as exc:
            log.exception("growth: a job failed for channel %s", channel.slug)
            out[channel.slug] = {"error": f"{type(exc).__name__}: {exc}"}
    failed = [f"{slug}: {result['error']}" for slug, result in out.items()
              if isinstance(result, dict) and result.get("error")]
    if failed:
        out["error"] = "; ".join(failed)
    return out


def feeds_for(db, channel: Channel) -> list[str]:
    """The legacy channel reads its feeds live from GROWTH_FEEDS; a named channel from its Feed rows."""
    if is_default(channel):
        return settings.growth_feeds_list
    return list(db.execute(
        select(Feed.url).where(Feed.channel_id == channel.id, Feed.enabled.is_(True))).scalars())


# The legacy channel resolves these live from settings, so a deployment that still drives everything
# through GROWTH_* (and the tests that monkeypatch those settings) behaves exactly as before.
def uploads_per_day(channel: Channel) -> int:
    return settings.growth_uploads_per_day if is_default(channel) else channel.uploads_per_day


def timezone_of(channel: Channel) -> str:
    return settings.growth_timezone if is_default(channel) else channel.timezone


def require_approval(channel: Channel) -> bool:
    return settings.growth_require_approval if is_default(channel) else channel.require_approval


def state_key(channel: Channel, name: str) -> str:
    """Namespace a GrowthState key by channel, so each channel keeps its own plan, focus and clocks.

    The legacy single channel keeps the un-namespaced keys it already wrote, so an existing
    deployment's plan and start clock are read back unchanged.
    """
    return name if channel.slug == DEFAULT_SLUG else f"{channel.slug}:{name}"


def video_scope(channel: Channel):
    """A SQLAlchemy filter for the videos that belong to a channel.

    The legacy channel also owns rows with no channel_id, so single-channel data written before
    this feature needs no backfill; a named channel owns only its own clips.
    """
    from sqlalchemy import or_

    from app.models import Video

    if channel.slug == DEFAULT_SLUG:
        return or_(Video.channel_id == channel.id, Video.channel_id.is_(None))
    return Video.channel_id == channel.id


def event_channel(channel: Channel) -> Optional[str]:
    """The channel_id to stamp on an event. The legacy channel leaves it None, as before."""
    return None if channel.slug == DEFAULT_SLUG else channel.id


def publisher_for(channel: Channel):
    """A publisher bound to this channel's token. Mock under USE_MOCK or when the channel has no Cloud project."""
    from app.publishers import MockPublisher, YouTubePublisher

    if settings.use_mock or not channel.client_secrets:
        return MockPublisher()
    return YouTubePublisher(token_path=channel.token_path)
