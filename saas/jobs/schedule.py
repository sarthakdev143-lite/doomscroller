"""Suggested posting times: a small, transparent rules model ("rules-v1"), not a black box.

We do not post for customers. Each clip's publish package suggests when to post it on each platform,
in the customer's own time zone (User.timezone, or ?tz= on the request). The rules are fixed local
times per platform and day type, chosen from the habits every platform's audience shares: the morning
commute, lunch, and the evening scroll, with weekends shifted later. They are a starting point, not a
promise; the windows are data in RULES below so they can be tuned (or replaced by a per-channel model
learned from results) without touching the API.

| platform        | weekdays (Mon-Fri)          | weekends (Sat-Sun) |
|-----------------|-----------------------------|--------------------|
| youtube_shorts  | 12:00, 15:00, 19:00         | 10:00, 14:00, 19:00 |
| instagram_reels | 09:00, 12:00, 19:00         | 10:00, 13:00, 19:00 |
| tiktok          | 07:00, 12:00, 19:00, 21:00  | 11:00, 19:00, 21:00 |

Clips from one job are staggered: clip N starts N slots later, so a batch isn't posted all at once.
A slot needs at least 30 minutes of lead time from now.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODEL = "rules-v1"
MIN_LEAD = timedelta(minutes=30)

RULES: dict[str, dict] = {
    "youtube_shorts": {"label": "YouTube Shorts", "weekday": ["12:00", "15:00", "19:00"],
                       "weekend": ["10:00", "14:00", "19:00"]},
    "instagram_reels": {"label": "Instagram Reels", "weekday": ["09:00", "12:00", "19:00"],
                        "weekend": ["10:00", "13:00", "19:00"]},
    "tiktok": {"label": "TikTok", "weekday": ["07:00", "12:00", "19:00", "21:00"],
               "weekend": ["11:00", "19:00", "21:00"]},
}
_PART_OF_DAY = ((5, "morning"), (11, "lunch"), (14, "afternoon"), (18, "evening"), (22, "late night"))


def valid_timezone(name: Optional[str]) -> Optional[str]:
    if not name or len(name) > 64 or ".." in name or name.startswith("/"):
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def _label(hour: int) -> str:
    label = "night"
    for start, name in _PART_OF_DAY:
        if hour >= start:
            label = name
    return label


def _slots(platform: str, tz: ZoneInfo, start: datetime, needed: int) -> list[datetime]:
    rules = RULES[platform]
    out: list[datetime] = []
    day = start.astimezone(tz).date()
    for _ in range(60):  # never more than a few weeks ahead
        key = "weekend" if day.weekday() >= 5 else "weekday"
        for hhmm in rules[key]:
            h, m = (int(x) for x in hhmm.split(":"))
            local = datetime.combine(day, time(h, m), tzinfo=tz)
            if local >= start:
                out.append(local)
                if len(out) >= needed:
                    return out
        day = day + timedelta(days=1)
    return out


def suggest(tz_name: Optional[str], *, now: Optional[datetime] = None, offset: int = 0, count: int = 3) -> dict:
    """{"timezone", "model", "platforms": {platform: {"label", "times": [{"local", "utc", "day", "part_of_day"}]}}}"""
    tz_valid = valid_timezone(tz_name) or "UTC"
    tz = ZoneInfo(tz_valid)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now + MIN_LEAD
    platforms = {}
    for platform, rules in RULES.items():
        slots = _slots(platform, tz, start, offset + count)[offset:offset + count]
        platforms[platform] = {
            "label": rules["label"],
            "times": [{"local": s.isoformat(), "utc": s.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                       "day": s.strftime("%a %d %b"), "part_of_day": _label(s.hour)} for s in slots],
        }
    return {"timezone": tz_valid, "model": MODEL, "platforms": platforms,
            "note": "Rule-based suggestions in your time zone (commute, lunch and evening peaks). "
                    "Clips from one job are staggered so they don't all go out at once."}
