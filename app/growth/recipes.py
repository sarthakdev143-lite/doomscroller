"""Editing choices tested per clip. The clipper reads these from settings, so a render overrides them."""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from app.config import settings

from .factors import PACES, RECIPE_FACTORS

log = logging.getLogger(__name__)

# Held for the whole render, so a second render cannot read another clip's values.
_lock = threading.RLock()


def choose(channel=None) -> dict[str, str]:
    """A recipe for the next speech clip. Rendering must not depend on the loop, so failure means no recipe."""
    if not settings.growth_enabled:
        return {}
    from .autopilot import pick_arm, policy

    try:
        current = policy(channel=channel)
        return {
            "pace": pick_arm(current, "pace", RECIPE_FACTORS["pace"]),
            "sfx": pick_arm(current, "sfx", RECIPE_FACTORS["sfx"]) if settings.clip_sfx else "off",
            "broll": pick_arm(current, "broll", RECIPE_FACTORS["broll"]) if settings.clip_broll else "off",
        }
    except Exception as exc:
        log.warning("growth: could not choose a recipe: %s", exc)
        return {}


@contextmanager
def applied(recipe: dict[str, str]) -> Iterator[None]:
    if not recipe or not isinstance(recipe, dict):
        yield
        return
    with _lock:
        # Inside the lock, or these would read another render's temporary values.
        wanted: dict[str, object] = {}
        pace = recipe.get("pace")
        if pace in PACES:
            speed, target = PACES[pace]
            wanted["clip_speed"] = speed
            wanted["clip_edit_target_sec"] = target
        if "sfx" in recipe:
            wanted["clip_sfx"] = bool(settings.clip_sfx and recipe["sfx"] == "on")
        if "broll" in recipe:
            wanted["clip_broll"] = bool(settings.clip_broll and recipe["broll"] == "on")
        if not wanted:
            yield
            return
        before = {name: getattr(settings, name) for name in wanted}
        try:
            for name, value in wanted.items():
                setattr(settings, name, value)
            yield
        finally:
            for name, value in before.items():
                setattr(settings, name, value)
