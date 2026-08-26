"""Scrapers package."""
from .base import Scraper, ScrapedItem
from .mock_scraper import MockScraper
from .snapchat_scraper import SnapchatScraper
from .yt_dlp_scraper import YtDlpScraper


def get_scraper(platform: str | None = None) -> Scraper:
    """Return the scraper for ``platform``, or the configured default.

    Mock mode always wins, so the whole pipeline can run with no network.
    Otherwise Snapchat Spotlight is the primary source; yt-dlp covers the
    generic (secondary) platforms.
    """
    from ..config import settings

    if settings.use_mock:
        return MockScraper()
    if platform:
        return SnapchatScraper() if platform.lower() == "snapchat" else YtDlpScraper()
    enabled = [p.lower() for p in settings.enabled_platforms_list]
    if enabled == ["snapchat"] or (enabled and enabled[0] == "snapchat"):
        return SnapchatScraper()
    return YtDlpScraper()


__all__ = [
    "Scraper",
    "ScrapedItem",
    "MockScraper",
    "SnapchatScraper",
    "YtDlpScraper",
    "get_scraper",
]
