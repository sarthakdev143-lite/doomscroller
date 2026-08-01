"""Publishers package."""
from .youtube_publisher import YouTubePublisher
from .mock_publisher import MockPublisher
from .base import PublishResult, Publisher


def get_publisher() -> Publisher:
    from ..config import settings
    if settings.use_mock or not settings.youtube_client_secrets:
        return MockPublisher()
    return YouTubePublisher()


__all__ = ["Publisher", "PublishResult", "YouTubePublisher", "MockPublisher"]
