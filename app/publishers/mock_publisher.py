"""Mock publisher — fakes successful uploads and returns a fake YouTube ID."""
from __future__ import annotations

import uuid

from .base import PublishResult, Publisher


class MockPublisher(Publisher):
    def upload(self, *, file_path: str, title: str, description: str,
               tags: list[str], category: int, privacy: str) -> PublishResult:
        fake_id = "MOCK_" + uuid.uuid4().hex[:11]
        return PublishResult(
            video_id=fake_id,
            status="uploaded",
            raw={
                "mock": True,
                "title": title,
                "privacy": privacy,
                "tags": tags,
            },
        )
