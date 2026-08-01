"""Publisher interface."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PublishResult:
    video_id: str
    status: str
    raw: dict


class Publisher:
    def upload(self, *, file_path: str, title: str, description: str,
               tags: list[str], category: int, privacy: str) -> PublishResult:
        raise NotImplementedError
