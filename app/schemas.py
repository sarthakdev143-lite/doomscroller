"""Pydantic schemas for API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    source_id: str
    source_url: str
    author: str
    title: str
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    local_path: Optional[str] = None
    transformed_path: Optional[str] = None
    youtube_video_id: Optional[str] = None

    views: int
    likes: int
    comments: int
    shares: int
    duration_sec: int

    engagement_score: float
    velocity_score: float
    status: str
    meta: dict
    error: Optional[str] = None

    scraped_at: datetime
    curated_at: Optional[datetime] = None
    transformed_at: Optional[datetime] = None
    published_at: Optional[datetime] = None


class ApproveBody(BaseModel):
    approved: bool
    note: Optional[str] = None


class TransformRecipe(BaseModel):
    add_blur_background: bool = True
    add_caption: bool = True
    caption_text: Optional[str] = None
    speed_factor: float = 1.0
    mirror: bool = False


class TransformBody(TransformRecipe):
    pass


class PublishBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    privacy: Optional[str] = None  # private|unlisted|public


class QuotaOut(BaseModel):
    day: str
    uploads: int
    cap: int
    remaining: int


class HealthOut(BaseModel):
    status: str
    use_mock: bool
    db_url: str
    pending_count: int


class ScrapeBody(BaseModel):
    handles: Optional[list[str]] = None
    hashtags: Optional[list[str]] = None
    max_per_run: Optional[int] = None
