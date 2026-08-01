"""Scrapers package."""
from .base import Scraper, ScrapedItem
from .yt_dlp_scraper import YtDlpScraper
from .mock_scraper import MockScraper


def get_scraper() -> Scraper:
    """Return the appropriate scraper based on settings."""
    from ..config import settings
    if settings.use_mock:
        return MockScraper()
    return YtDlpScraper()


__all__ = ["Scraper", "ScrapedItem", "YtDlpScraper", "MockScraper", "get_scraper"]
