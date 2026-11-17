"""Tests must never reach the owner's real data. A cleanup run once deleted every real clip this way."""
from pathlib import Path

from app import logging_config
from app.config import settings

ROOT = Path(__file__).resolve().parent.parent


def test_tests_never_touch_the_owners_videos_logs_channels_or_database():
    assert not settings.content_path.resolve().is_relative_to(ROOT)
    assert not logging_config.LOG_DIR.resolve().is_relative_to(ROOT)
    assert not settings.channels_path.resolve().is_relative_to(ROOT)
    assert ":memory:" in settings.db_url
