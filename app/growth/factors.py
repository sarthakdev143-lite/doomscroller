"""What the channel tests, and how a published video maps onto those tests."""
from __future__ import annotations

import logging
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
# Facts about a finished clip, chosen between when picking what to post next.
CLIP_FACTORS = ("kind", "length", "series", "pace", "sfx", "broll")
FACTORS = ("slot", "title_style", *CLIP_FACTORS)


def channel_zone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.growth_timezone)
    except Exception:
        log.warning("growth: unknown GROWTH_TIMEZONE %r, using UTC", settings.growth_timezone)
        return ZoneInfo("UTC")


def aware(moment: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands datetimes back without a zone, and they were all stored as UTC."""
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def author_names(video) -> tuple[str, ...]:
    return tuple(n.casefold() for n in (video.author, video.author_name) if n)


def slot_of(moment: datetime) -> str:
    return SLOTS[aware(moment).astimezone(channel_zone()).hour // 4]


def length_of(seconds: float) -> str:
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
        "length": length_of(video.duration_sec or 0),
        "series": (video.author or meta.get("clip_parent") or "unknown")[:64],
    }
    arms.update({k: v for k, v in (meta.get("growth_recipe") or {}).items() if k in RECIPE_FACTORS})
    return arms


def published_arms(video) -> dict[str, str]:
    """Every arm of a published video. Timing comes from when it really went out."""
    published = aware(video.published_at)
    local = published.astimezone(channel_zone())
    return {
        **clip_arms(video),
        "slot": slot_of(published),
        "title_style": (video.meta or {}).get("growth_title_style") or "plain",
        # Not something the loop chooses, but it would otherwise be blamed on the slot.
        "day_type": "weekend" if local.weekday() >= 5 else "weekday",
    }
