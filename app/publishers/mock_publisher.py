"""Mock publisher — fakes successful uploads and returns a fake YouTube ID."""
from __future__ import annotations

import uuid

from .base import PublishResult, Publisher


class MockPublisher(Publisher):
    def upload(self, *, file_path: str, title: str, description: str,
               tags: list[str], category: int, privacy: str) -> PublishResult:
        fake_id = "MOCK_" + uuid.uuid4().hex[:11]
        clean_tags = [str(t) for t in (tags or []) if t is not None and str(t).strip()]
        clean_privacy = (privacy or "public").strip().lower() or "public"
        return PublishResult(
            video_id=fake_id,
            status="uploaded",
            raw={
                "mock": True,
                "title": title or "",
                "privacy": clean_privacy,
                "tags": clean_tags,
            },
        )
