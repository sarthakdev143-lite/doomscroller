"""Pydantic schemas for API."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    source_id: str
    source_url: str
    author: str = ""
    author_name: str = ""
    title: str = ""
    description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    media_url: Optional[str] = None
    local_path: Optional[str] = None
    transformed_path: Optional[str] = None
    youtube_video_id: Optional[str] = None
    language: str = ""
    keywords: list[str] = Field(default_factory=list)

    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    duration_sec: int = 0
    width: int = 0
    height: int = 0

    engagement_score: float = 0.0
    velocity_score: float = 0.0
    reach_score: float = 0.0
    viral_score: float = 0.0
    status: str
    channel_id: Optional[str] = None
    meta: dict | str | None = None
    error: Optional[str] = None

    source_published_at: Optional[datetime] = None
    scraped_at: datetime
    curated_at: Optional[datetime] = None
    transformed_at: Optional[datetime] = None
    published_at: Optional[datetime] = None

    @field_validator("author", "author_name", "title", "language", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> Any:
        return "" if v is None else v

    @field_validator("views", "likes", "comments", "shares", "duration_sec", "width", "height", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> Any:
        return 0 if v is None else v

    @field_validator("engagement_score", "velocity_score", "reach_score", "viral_score", mode="before")
    @classmethod
    def _coerce_float(cls, v: Any) -> Any:
        return 0.0 if v is None else v

    @field_validator("keywords", mode="before")
    @classmethod
    def _coerce_keywords(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return [str(x) for x in parsed] if isinstance(parsed, list) else []
            except Exception:
                return []
        if isinstance(v, list):
            return [str(x) for x in v]
        return []

    @field_validator("meta", mode="before")
    @classmethod
    def _coerce_meta(cls, v: Any) -> dict | str | None:
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
            # return raw string if we can't parse it
            return v
        return v


class ClipBody(BaseModel):
    source: str
    count: Optional[int] = Field(default=None, ge=1, le=20)
    auto_queue: Optional[bool] = None
    channel: Optional[str] = None


class ApproveBody(BaseModel):
    approved: bool
    note: Optional[str] = Field(default=None, max_length=1000)


class TransformRecipe(BaseModel):
    add_blur_background: bool = True
    add_caption: bool = True
    caption_text: Optional[str] = Field(default=None, max_length=200)
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    mirror: bool = False


class TransformBody(TransformRecipe):
    pass


class PublishBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)
    tags: Optional[list[str]] = None
    privacy: Optional[Literal["private", "unlisted", "public"]] = None

    @field_validator("privacy", mode="before")
    @classmethod
    def _normalise_privacy(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return v.strip().lower()
        return v


class MetadataUpdateBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None


class QuotaOut(BaseModel):
    day: str
    uploads: int
    cap: int
    remaining: int


class HealthOut(BaseModel):
    status: str
    use_mock: bool
    db_url: Optional[str] = None
    pending_count: int


class ScrapeBody(BaseModel):
    handles: Optional[list[str]] = None
    hashtags: Optional[list[str]] = None
    max_per_run: Optional[int] = Field(default=None, ge=1, le=200)
