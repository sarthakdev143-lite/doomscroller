"""Publisher interface."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PublishResult:
    video_id: str
    status: str
    raw: dict


class UploadOutcomeUnknown(Exception):
    """The file was being sent when the upload failed, so it may be live on the channel."""


class TokenProblem(RuntimeError):
    """No upload can succeed until the owner logs in again, whichever clip it is."""


class Publisher:
    def check_ready(self) -> None:
        """Raises if an upload could not start, so nothing is spent preparing one."""

    def fetch_stats(self, video_ids: list[str]) -> dict:
        """Return ``{video_id: {"viewCount": int, "likeCount": int, "commentCount": int}}``."""
        return {}

    def upload(self, *, file_path: str, title: str, description: str,
               tags: list[str], category: int, privacy: str,
               default_language: str | None = None,
               default_audio_language: str | None = None) -> PublishResult:
        raise NotImplementedError

    def set_thumbnail(self, video_id: str, image_path: str) -> bool:
        """Upload a custom thumbnail for a YouTube video (no-op unless overridden)."""
        return False
