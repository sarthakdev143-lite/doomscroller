"""What the channel tests, and how a published video maps onto those tests."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings

log = logging.getLogger(__name__)

SLOTS = ("00-04", "04-08", "08-12", "12-16", "16-20", "20-24")
TITLE_STYLES = ("plain", "question", "curiosity", "quote")
# name -> (speed, seconds of speech the edit aims for)
PACES = {"tight": (1.15, 32), "normal": (1.1, 40), "relaxed": (1.0, 50)}
SWITCHES = ("on", "off")

# Decided when a clip is rendered. They only apply to speech clips, which are the ones with an edit.
# Sound effects are always on: the owner wants them on every clip, so they are not a test.
RECIPE_FACTORS = {"pace": tuple(PACES), "sfx": ("on",), "broll": SWITCHES}
_VALID_RECIPE_ARMS = {"pace": frozenset(PACES), "sfx": frozenset(SWITCHES), "broll": frozenset(SWITCHES)}
# Facts about a finished clip, chosen between when picking what to post next.
CLIP_FACTORS = ("kind", "length", "series", "pace", "sfx", "broll")
FACTORS = ("slot", "title_style", *CLIP_FACTORS)


def _utc_zone():
    try:
        return ZoneInfo("UTC")
    except Exception:
        return timezone.utc


def channel_zone(channel=None):
    """The posting timezone. The legacy channel (or no channel) uses GROWTH_TIMEZONE; a named one its own."""
    if channel is None:
        tz = settings.growth_timezone
    else:
        from . import channels
        tz = channels.timezone_of(channel)
    cleaned = (tz if isinstance(tz, str) else "").strip()
    if not cleaned:
        return _utc_zone()
    try:
        return ZoneInfo(cleaned)
    except Exception:
        log.warning("growth: unknown timezone %r, using UTC", tz)
        return _utc_zone()


def aware(moment: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands datetimes back without a zone, and they were all stored as UTC."""
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def author_names(video) -> tuple[str, ...]:
    return tuple(dict.fromkeys(s for n in (video.author, video.author_name) if n and (s := n.strip().casefold())))


def slot_of(moment: Optional[datetime], zone: Optional[ZoneInfo] = None) -> str:
    resolved = aware(moment) or datetime.now(timezone.utc)
    return SLOTS[resolved.astimezone(zone or channel_zone()).hour // 4]


def length_of(seconds: Optional[float]) -> str:
    if seconds is None or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        seconds = 0.0
    if seconds < 20:
        return "under20"
    if seconds < 35:
        return "20-35"
    return "35-60" if seconds <= 60 else "over60"


def clip_arms(video) -> dict[str, str]:
    """The arms a finished clip already belongs to, read from the row rather than from any plan."""
    meta = video.meta or {}
    arms = {
        "kind": meta.get("clip_kind") or "visual",
        "length": length_of(video.duration_sec),
        "series": (video.author or meta.get("clip_parent") or "unknown")[:64],
    }
    raw_recipe = meta.get("growth_recipe")
    if isinstance(raw_recipe, dict):
        arms.update({
            k: v for k, v in raw_recipe.items()
            if k in _VALID_RECIPE_ARMS and isinstance(v, str) and v in _VALID_RECIPE_ARMS[k]
        })
    return arms


def published_arms(video, zone: Optional[ZoneInfo] = None) -> dict[str, str]:
    """Every arm of a published video. Timing comes from when it really went out.

    `zone` is the posting channel's timezone. Its plan lays the windows out in that zone, so reading the
    slot back in another one would credit the video to a window it was never posted in.
    """
    zone = zone or channel_zone()
    published = aware(video.published_at) or datetime.now(timezone.utc)
    local = published.astimezone(zone)
    return {
        **clip_arms(video),
        "slot": slot_of(published, zone),
        "title_style": (video.meta or {}).get("growth_title_style") or "plain",
        # Not something the loop chooses, but it would otherwise be blamed on the slot.
        "day_type": "weekend" if local.weekday() >= 5 else "weekday",
    }
