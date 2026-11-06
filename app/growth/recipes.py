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


def choose() -> dict[str, str]:
    """A recipe for the next speech clip. Rendering must not depend on the loop, so failure means no recipe."""
    if not settings.growth_enabled:
        return {}
    from .autopilot import pick_arm, policy

    try:
        current = policy()
        return {factor: pick_arm(current, factor, arms) for factor, arms in RECIPE_FACTORS.items()}
    except Exception as exc:
        log.warning("growth: could not choose a recipe: %s", exc)
        return {}


@contextmanager
def applied(recipe: dict[str, str]) -> Iterator[None]:
    if not recipe:
        yield
        return
    speed, target = PACES[recipe["pace"]]
    with _lock:
        # Inside the lock, or these would read another render's temporary values.
        wanted = {
            "clip_speed": speed, "clip_edit_target_sec": target,
            "clip_sfx": settings.clip_sfx and recipe["sfx"] == "on",
            "clip_broll": settings.clip_broll and recipe["broll"] == "on",
        }
        before = {name: getattr(settings, name) for name in wanted}
        try:
            for name, value in wanted.items():
                setattr(settings, name, value)
            yield
        finally:
            for name, value in before.items():
                setattr(settings, name, value)
