"""Mock publisher — fakes successful uploads and returns a fake YouTube ID."""
from __future__ import annotations

import os
import uuid

from .base import PublishResult, Publisher


class MockPublisher(Publisher):
    def upload(self, *, file_path: str, title: str, description: str,
               tags: list[str], category: int, privacy: str,
               default_language: str | None = None,
               default_audio_language: str | None = None) -> PublishResult:
        fake_id = "MOCK_" + uuid.uuid4().hex[:11]
        clean_tags = [str(t) for t in (tags or []) if t is not None and str(t).strip()]
        clean_privacy = (privacy or "public").strip().lower() or "public"
        raw = {
            "mock": True,
            "title": title or "",
            "privacy": clean_privacy,
            "tags": clean_tags,
        }
        if default_language:
            raw["defaultLanguage"] = default_language
        if default_audio_language:
            raw["defaultAudioLanguage"] = default_audio_language
        return PublishResult(
            video_id=fake_id,
            status="uploaded",
            raw=raw,
        )

    def set_thumbnail(self, video_id: str, image_path: str) -> bool:
        return bool(video_id and image_path and os.path.isfile(image_path))

    def post_comment(self, video_id: str, text: str) -> str | None:
        return "MOCK_COMMENT" if (video_id and str(text or "").strip()) else None
