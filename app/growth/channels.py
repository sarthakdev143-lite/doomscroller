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
import math
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.models import Channel, Feed, GrowthState

log = logging.getLogger(__name__)

DEFAULT_SLUG = "default"
_CONFIG_HEALTH = "channels_config"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_BARE_HANDLE_RE = re.compile(r"^@([A-Za-z0-9._%-]{1,64})/?$")
_BARE_UC_RE = re.compile(r"^(UC[0-9A-Za-z_-]{20,40})$")
_YT_BARE_DOMAIN_RE = re.compile(r"^(?:www\.|m\.)?youtube\.com/(.+)$", re.IGNORECASE)
_YT_HANDLE_PATH_RE = re.compile(
    r"^/@([A-Za-z0-9._%-]{1,64})(?:/(?:videos|featured|shorts|streams|playlists|community|about))?/?$",
    re.IGNORECASE,
)
_YT_CHANNEL_PATH_RE = re.compile(
    r"^/channel/(UC[0-9A-Za-z_-]{20,40})(?:/(?:videos|featured|shorts|streams|playlists|community|about))?/?$",
    re.IGNORECASE,
)
_YT_CUSTOM_PATH_RE = re.compile(
    r"^/(c|user)/([A-Za-z0-9._%-]{1,64})(?:/(?:videos|featured|shorts|streams|playlists|community|about))?/?$",
    re.IGNORECASE,
)
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
    problem = _config_problem(valid)
    if problem:
        raise ChannelsConfigError(f"channels.json, {problem}. Fix the file; the fleet is unchanged.")
    return valid


def normalize_feed(url: str) -> str:
    """Canonicalize pasted YouTube handles, UC... IDs, or mobile/tab URLs into clean https://www.youtube.com/... links."""
    s = _CTRL_RE.sub("", str(url or "").strip())
    if not s:
        return ""
    m_handle = _BARE_HANDLE_RE.match(s)
    if m_handle:
        return f"https://www.youtube.com/@{m_handle.group(1)}"
    m_uc = _BARE_UC_RE.match(s)
    if m_uc:
        return f"https://www.youtube.com/channel/{m_uc.group(1)}"
    if _YT_BARE_DOMAIN_RE.match(s):
        s = f"https://{s}"
    if s.startswith(("http://", "https://")):
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            path = parts.path or ""
            mh = _YT_HANDLE_PATH_RE.match(path)
            if mh:
                return f"https://www.youtube.com/@{mh.group(1)}"
            mc = _YT_CHANNEL_PATH_RE.match(path)
            if mc:
                return f"https://www.youtube.com/channel/{mc.group(1)}"
            mcustom = _YT_CUSTOM_PATH_RE.match(path)
            if mcustom:
                return f"https://www.youtube.com/{mcustom.group(1).lower()}/{mcustom.group(2)}"
            if path.rstrip("/") == "/playlist":
                lists = parse_qs(parts.query).get("list")
                if lists and lists[0].strip():
                    return f"https://www.youtube.com/playlist?list={lists[0].strip()}"
        if not parts.query and not parts.fragment and s.endswith("/") and len(parts.path or "") > 1:
            return s.rstrip("/")
    return s


def _feed_key(url: str) -> str:
    """One creator's link written two ways is still one feed."""
    norm = normalize_feed(url)
    return re.sub(r"^https?://(www\.|m\.)?", "", norm.casefold()).rstrip("/")


def _dedupe_feeds(urls: Iterable[str]) -> list[str]:
    """Normalize and case-insensitively deduplicate feed entries while preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls or ():
        if not isinstance(raw, str):
            continue
        norm = normalize_feed(raw)
        if not norm:
            continue
        key = _feed_key(norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def _config_problem(entries: list[dict]) -> Optional[str]:
    """Why this set of channels cannot be used, naming the channel, or None."""
    seen: set[str] = set()
    owners: dict[str, tuple[str, bool, Optional[str]]] = {}
    for entry in entries:
        slug = str(entry["slug"]).strip()
        problem = _entry_problem(entry, seen)
        if problem:
            return f"channel {slug!r}: {problem}"
        seen.add(slug)
        is_enabled = True if slug == DEFAULT_SLUG else bool(entry.get("enabled", True))
        cloned_from = entry.get("cloned_from").strip() if isinstance(entry.get("cloned_from"), str) else None
        # A disabled clone may hold template feeds copied from its source until it is switched on.
        if not is_enabled and cloned_from:
            continue
        # The legacy channel's feeds live in .env, and it is running them whenever it is listed.
        feeds = settings.growth_feeds_list if slug == DEFAULT_SLUG else (entry.get("feeds") or [])
        for url in feeds:
            key = _feed_key(url)
            if not key:
                continue
            prev = owners.get(key)
            if prev is None:
                owners[key] = (slug, is_enabled, cloned_from)
            elif prev[0] != slug:
                # Allow a clone to take over feeds once its source channel has been switched off.
                if not prev[1] and cloned_from == prev[0]:
                    owners[key] = (slug, is_enabled, cloned_from)
                    continue
                return f"channel {slug!r}: {_shared_feed(url, prev[0], slug)}"
    return None


def _shared_feed(url: str, first: str, second: str) -> str:
    """A source feeds one channel. Two would cut the same video into the same clips and fight over them."""
    if DEFAULT_SLUG in (first, second):
        return (f"feed {url} is also in GROWTH_FEEDS, which feeds the channel from your .env (\"default\"). "
                "Take it out of GROWTH_FEEDS and restart, or remove the \"default\" entry to retire that channel")
    return f"feed {url} is already listed under {first!r}, and a source can feed only one channel"


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
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0.0 <= float(score) <= 1.0
    ):
        return "auto_approve_score must be a number from 0 to 1"
    for flag in ("require_approval", "enabled", "cadence_backstop"):
        if entry.get(flag) is not None and not isinstance(entry[flag], bool):
            return f"{flag} must be true or false"
    zone = entry.get("timezone")
    if zone is not None:
        if not isinstance(zone, str) or not zone.strip():
            return f"unknown timezone {zone!r}"
        try:
            ZoneInfo(zone.strip())
        except Exception:
            return f"unknown timezone {zone!r}"
    feeds = entry.get("feeds")
    if feeds is not None:
        if not isinstance(feeds, list) or not all(isinstance(f, str) for f in feeds):
            return "feeds must be a list of links"
        for f in feeds:
            if f.strip() and not normalize_feed(f).startswith(("http://", "https://")):
                return f"feeds must contain valid links, @handles, or UC... channel IDs (got {f!r})"
    cloned_from = entry.get("cloned_from")
    if cloned_from is not None:
        if not isinstance(cloned_from, str) or not _SLUG.match(cloned_from.strip()):
            return f"invalid cloned_from slug {cloned_from!r}"
    return None


def _record_config(db, error: Optional[str], count: int = 0) -> None:
    value = {"error": error} if error else {"ok": True, "count": count}
    state = db.get(GrowthState, _CONFIG_HEALTH)
    if state:
        state.value = value
    else:
        db.add(GrowthState(key=_CONFIG_HEALTH, value=value))
        db.flush()  # db.get cannot see a pending row, and reload reads this back in the same session


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
            val = entry[field]
            if field == "auto_approve_score":
                val = float(val)
            elif field == "timezone":
                val = str(val).strip()
            setattr(channel, field, val)


def _sync_feeds(db, channel: Channel, urls: list[str]) -> None:
    """Make the channel's feed rows match the config: add new links, drop ones no longer listed."""
    wanted = _dedupe_feeds(urls)
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


def _write_config_file(data: dict) -> None:
    path = settings.channels_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def edit_config(change: Callable[[list[dict]], None]) -> dict:
    """Change channels.json through code rather than by hand: validated, written whole, then applied.

    Nothing is written if any entry would be invalid, and the running loop picks the result up on its next tick.
    """
    data = _read_file()
    entries = data["channels"]
    change(entries)
    if not all(isinstance(entry, dict) and entry.get("slug") for entry in entries):
        raise ChannelsConfigError("every channel needs a slug")
    problem = _config_problem(entries)
    if problem:
        raise ChannelsConfigError(problem)
    _write_config_file(data)
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
            "uploads_per_day": uploads_per_day, "timezone": timezone.strip() or settings.growth_timezone or "UTC",
            "require_approval": True, "enabled": False, "feeds": _dedupe_feeds(feeds),
        })
    return edit_config(change)


def set_enabled(slug: str, enabled: bool) -> dict:
    def change(entries: list[dict]) -> None:
        _entry(entries, slug)["enabled"] = enabled
    return edit_config(change)


_EDITABLE = (
    "name",
    "niche",
    "brand_name",
    "uploads_per_day",
    "timezone",
    "require_approval",
    "auto_approve_score",
    "cadence_backstop",
)


def update_channel(slug: str, **fields) -> dict:
    """Change a channel's own settings. Only what was given is touched, and the usual checks still apply."""
    unknown = sorted(set(fields) - set(_EDITABLE))
    if unknown:
        raise ChannelsConfigError(f"cannot set {', '.join(unknown)}")

    def change(entries: list[dict]) -> None:
        _entry(entries, slug).update({k: v for k, v in fields.items() if v is not None})
    return edit_config(change)


def next_step(db, channel: Channel) -> str:
    """The one thing this channel needs from its owner next, or '' when it is running.

    Setup is several steps across Google and here, in a fixed order. Naming only the next one means
    nobody has to work out where they stopped.
    """
    if is_default(channel):
        return ""
    slug = channel.slug
    if not channel.client_secrets or not os.path.exists(channel.client_secrets):
        return f"save its Google OAuth client file as {channel.client_secrets or f'secrets/{slug}_client_secret.json'}"
    if not channel.token_path or not os.path.exists(channel.token_path):
        return f"connect it to YouTube: scripts/channels.py connect {slug}"
    if not feeds_for(db, channel):
        return f"give it a creator to clip: scripts/channels.py feed {slug} --add <link>"
    if not channel.enabled:
        return f"switch it on: scripts/channels.py connect {slug}"
    from . import safety

    paused = safety.pause_reason(db, channel)
    if paused:
        return f"paused ({paused}). Resume: scripts/growth.py resume {slug}"
    return ""


def change_feeds(slug: str, *, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> dict:
    def change(entries: list[dict]) -> None:
        entry = _entry(entries, slug)
        remove_keys = {_feed_key(f) for f in remove if isinstance(f, str) and f.strip()}
        kept = [f for f in _dedupe_feeds(entry.get("feeds") or []) if _feed_key(f) not in remove_keys]
        raw_added = [f for f in add if isinstance(f, str) and f.strip()]
        for raw in raw_added:
            if not normalize_feed(raw).startswith(("http://", "https://")):
                raise ChannelsConfigError(f"feeds must contain valid links, @handles, or UC... channel IDs (got {raw!r})")
        entry["feeds"] = _dedupe_feeds([*kept, *raw_added])
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


def _clean_str(val: object) -> str:
    if not isinstance(val, str):
        return ""
    return _CTRL_RE.sub("", val).strip()


def _channel_to_entry(db, channel: Channel) -> dict:
    slug = channel.slug
    return {
        "slug": slug,
        "name": channel.name or slug,
        "niche": channel.niche or "",
        "brand_name": channel.brand_name or "",
        "client_secrets": channel.client_secrets or f"secrets/{slug}_client_secret.json",
        "token_path": channel.token_path or f"secrets/{slug}_token.json",
        "uploads_per_day": uploads_per_day(channel),
        "timezone": (timezone_of(channel) or "UTC").strip(),
        "require_approval": bool(require_approval(channel)),
        "auto_approve_score": float(channel.auto_approve_score or 0.0),
        "cadence_backstop": bool(channel.cadence_backstop),
        "enabled": bool(channel.enabled),
        "feeds": _dedupe_feeds(feeds_for(db, channel)),
    }


def _base_entries(db) -> list[dict]:
    """Current channels.json entries, augmented with any live DB channels not yet materialized on disk."""
    data = _read_file()
    entries = [dict(e) for e in data["channels"] if isinstance(e, dict) and e.get("slug")]
    seen = {str(e["slug"]).strip() for e in entries}
    if not entries and (settings.growth_feeds_list or settings.youtube_client_secrets):
        entries.append({"slug": DEFAULT_SLUG})
        seen.add(DEFAULT_SLUG)
    for ch in db.execute(select(Channel).order_by(Channel.slug)).scalars():
        if not is_default(ch) and ch.slug not in seen:
            entries.append(_channel_to_entry(db, ch))
            seen.add(ch.slug)
    return entries


def export_channels_snapshot(db=None) -> dict:
    """Export portable, sanitized JSON of managed channels and feeds (never OAuth tokens or secret paths)."""
    if db is None:
        from app.db import session_scope

        with session_scope() as s:
            return export_channels_snapshot(s)

    loaded: Optional[list[dict]] = None
    cloned_map: dict[str, str] = {}
    try:
        loaded = _load_config()
    except ChannelsConfigError:
        loaded = None

    listed_slugs: Optional[set[str]] = None
    if loaded is not None:
        listed_slugs = {str(e["slug"]).strip() for e in loaded}
        cloned_map = {
            str(e["slug"]).strip(): e["cloned_from"].strip()
            for e in loaded
            if isinstance(e.get("cloned_from"), str) and e["cloned_from"].strip()
        }
        existing_db_slugs = set(db.execute(select(Channel.slug)).scalars())
        if not listed_slugs.issubset(existing_db_slugs):
            sync_channels(db)
    else:
        _ensure_seeded(db)

    items: list[dict] = []
    for channel in all_channels(db):
        if is_default(channel):
            continue
        if listed_slugs is not None and channel.slug not in listed_slugs:
            continue
        item: dict = {
            "slug": channel.slug,
            "name": channel.name or channel.slug,
            "niche": channel.niche or "",
            "brand_name": channel.brand_name or "",
            "uploads_per_day": uploads_per_day(channel),
            "timezone": (timezone_of(channel) or "UTC").strip(),
            "require_approval": bool(require_approval(channel)),
            "auto_approve_score": float(channel.auto_approve_score or 0.0),
            "cadence_backstop": bool(channel.cadence_backstop),
            "enabled": bool(channel.enabled),
            "feeds": _dedupe_feeds(feeds_for(db, channel)),
        }
        if channel.slug in cloned_map:
            item["cloned_from"] = cloned_map[channel.slug]
        items.append(item)

    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "channels": items,
    }


def import_channels_snapshot(db, payload: dict, *, dry_run: bool = False) -> dict:
    """Validate and upsert portable channel definitions without touching OAuth tokens or secret paths."""
    if db is None:
        from app.db import session_scope

        with session_scope() as s:
            return import_channels_snapshot(s, payload, dry_run=dry_run)

    if not isinstance(payload, dict):
        raise ChannelsConfigError("snapshot payload must be a JSON object with a 'channels' list")
    version = payload.get("version", 1)
    if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        raise ChannelsConfigError(f"unsupported snapshot version {version!r}")
    incoming = payload.get("channels")
    if not isinstance(incoming, list):
        raise ChannelsConfigError("snapshot payload has no 'channels' list")

    seen_incoming: set[str] = set()
    for raw in incoming:
        if not isinstance(raw, dict) or not isinstance(raw.get("slug"), str) or not raw["slug"].strip():
            raise ChannelsConfigError("every imported channel needs a slug")
        slug = raw["slug"].strip()
        if slug == DEFAULT_SLUG:
            raise ChannelsConfigError(f"channel {slug!r}: cannot import into the legacy default channel")
        for text_field in ("name", "niche", "brand_name"):
            if raw.get(text_field) is not None and not isinstance(raw[text_field], str):
                raise ChannelsConfigError(f"channel {slug!r}: {text_field} must be a string")
        problem = _entry_problem(raw, seen_incoming)
        if problem:
            raise ChannelsConfigError(f"channel {slug!r}: {problem}")
        seen_incoming.add(slug)

    merged = _base_entries(db)
    by_slug: dict[str, dict] = {str(e["slug"]).strip(): e for e in merged}
    created: list[str] = []
    updated: list[str] = []
    total_feeds = 0

    for raw in incoming:
        slug = raw["slug"].strip()
        norm_feeds = _dedupe_feeds(raw.get("feeds") or []) if "feeds" in raw and raw["feeds"] is not None else None
        if norm_feeds is not None:
            total_feeds += len(norm_feeds)

        if slug in by_slug:
            updated.append(slug)
            target = by_slug[slug]
            for text_field in ("name", "niche", "brand_name"):
                if raw.get(text_field) is not None:
                    target[text_field] = _clean_str(raw[text_field])
            if raw.get("uploads_per_day") is not None:
                target["uploads_per_day"] = int(raw["uploads_per_day"])
            if raw.get("timezone") is not None:
                target["timezone"] = raw["timezone"].strip()
            for flag in ("require_approval", "enabled", "cadence_backstop"):
                if raw.get(flag) is not None:
                    target[flag] = bool(raw[flag])
            if raw.get("auto_approve_score") is not None:
                target["auto_approve_score"] = float(raw["auto_approve_score"])
            if norm_feeds is not None:
                target["feeds"] = norm_feeds
            if isinstance(raw.get("cloned_from"), str) and raw["cloned_from"].strip():
                target["cloned_from"] = raw["cloned_from"].strip()
            # Never accept token_path, client_secrets, or youtube_channel_id from untrusted snapshots.
            target.setdefault("client_secrets", f"secrets/{slug}_client_secret.json")
            target.setdefault("token_path", f"secrets/{slug}_token.json")
        else:
            created.append(slug)
            new_entry: dict = {
                "slug": slug,
                "name": _clean_str(raw.get("name")) or slug,
                "niche": _clean_str(raw.get("niche")),
                "brand_name": _clean_str(raw.get("brand_name")),
                "client_secrets": f"secrets/{slug}_client_secret.json",
                "token_path": f"secrets/{slug}_token.json",
                "uploads_per_day": int(raw["uploads_per_day"]) if raw.get("uploads_per_day") is not None else 3,
                "timezone": raw["timezone"].strip() if raw.get("timezone") is not None else (settings.growth_timezone or "UTC"),
                "require_approval": bool(raw["require_approval"]) if raw.get("require_approval") is not None else True,
                "auto_approve_score": float(raw["auto_approve_score"]) if raw.get("auto_approve_score") is not None else 0.0,
                "cadence_backstop": bool(raw["cadence_backstop"]) if raw.get("cadence_backstop") is not None else False,
                "enabled": bool(raw["enabled"]) if raw.get("enabled") is not None else False,
                "feeds": norm_feeds if norm_feeds is not None else [],
            }
            if isinstance(raw.get("cloned_from"), str) and raw["cloned_from"].strip():
                new_entry["cloned_from"] = raw["cloned_from"].strip()
            merged.append(new_entry)
            by_slug[slug] = new_entry

    problem = _config_problem(merged)
    if problem:
        raise ChannelsConfigError(problem)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "created": created,
            "updated": updated,
            "count": len(incoming),
            "feeds": total_feeds,
        }

    _write_config_file({"channels": merged})
    active = sync_channels(db)
    return {
        "ok": True,
        "dry_run": False,
        "created": created,
        "updated": updated,
        "count": len(incoming),
        "feeds": total_feeds,
        "active": [c.slug for c in active],
    }


def clone_channel(db, source_slug: str, new_slug: str, *, name: str = "") -> Channel:
    """Clone cadence, niche, brand, approval policy, and feeds into a fresh disabled, unlinked channel."""
    if db is None:
        from app.db import session_scope

        with session_scope() as s:
            return clone_channel(s, source_slug, new_slug, name=name)

    src_slug = str(source_slug or "").strip()
    dst_slug = str(new_slug or "").strip()
    if not _SLUG.match(dst_slug):
        raise ChannelsConfigError(f"channel {dst_slug!r}: the slug must be lowercase letters, digits, - or _")
    if dst_slug in _RESERVED_SLUGS or dst_slug == DEFAULT_SLUG:
        raise ChannelsConfigError(f"channel {dst_slug!r}: {dst_slug!r} is a reserved name")

    if settings.channels_path.exists() and channel_by_slug(db, src_slug) is None:
        sync_channels(db)
    else:
        _ensure_seeded(db)

    src = channel_by_slug(db, src_slug)
    if src is None:
        known = ", ".join(c.slug for c in all_channels(db)) or "none"
        raise ChannelsConfigError(f"no channel {src_slug!r}. Known: {known}.")

    merged = _base_entries(db)
    if any(str(e.get("slug", "")).strip() == dst_slug for e in merged) or channel_by_slug(db, dst_slug) is not None:
        raise ChannelsConfigError(f"channel {dst_slug!r} already exists")

    cloned_entry: dict = {
        "slug": dst_slug,
        "name": _clean_str(name) or f"{src.name or src_slug} (copy)",
        "niche": src.niche or "",
        "brand_name": src.brand_name or "",
        "client_secrets": f"secrets/{dst_slug}_client_secret.json",
        "token_path": f"secrets/{dst_slug}_token.json",
        "uploads_per_day": uploads_per_day(src),
        "timezone": (timezone_of(src) or "UTC").strip(),
        "require_approval": bool(require_approval(src)),
        "auto_approve_score": float(src.auto_approve_score or 0.0),
        "cadence_backstop": bool(src.cadence_backstop),
        "enabled": False,
        "cloned_from": src_slug,
        "feeds": _dedupe_feeds(feeds_for(db, src)),
    }
    merged.append(cloned_entry)
    problem = _config_problem(merged)
    if problem:
        raise ChannelsConfigError(problem)

    _write_config_file({"channels": merged})
    sync_channels(db)
    cloned = channel_by_slug(db, dst_slug)
    if cloned is None:  # pragma: no cover - defensive
        raise ChannelsConfigError(f"failed to persist cloned channel {dst_slug!r}")
    cloned.youtube_channel_id = None
    cloned.enabled = False
    db.flush()
    return cloned


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
        return _dedupe_feeds(settings.growth_feeds_list)
    return list(db.execute(
        select(Feed.url).where(Feed.channel_id == channel.id, Feed.enabled.is_(True))).scalars())


# The legacy channel resolves these live from settings, so a deployment that still drives everything
# through GROWTH_* (and the tests that monkeypatch those settings) behaves exactly as before.
def uploads_per_day(channel: Channel) -> int:
    raw = settings.growth_uploads_per_day if is_default(channel) else channel.uploads_per_day
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        return max(0, min(50, int(raw)))
    except (TypeError, ValueError):
        return 0


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
