"""SQLAlchemy models for Doomscroller."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    ForeignKey,
    Enum as SAEnum,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class VideoStatus(str, enum.Enum):
    PENDING = "pending"          # scraped, awaiting curation
    APPROVED = "approved"        # human approved
    REJECTED = "rejected"        # human rejected
    TRANSFORMING = "transforming"
    TRANSFORMED = "transformed"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


class Video(Base):
    __tablename__ = "videos"

    __table_args__ = (
        UniqueConstraint("source", "source_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(32), index=True)  # tiktok|instagram|youtube|mock
    source_id: Mapped[str] = mapped_column(String(128), index=True)  # id on the source platform
    source_url: Mapped[str] = mapped_column(Text)

    author: Mapped[str] = mapped_column(String(128), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, default="")
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    local_path: Mapped[Optional[str]] = mapped_column(Text, default=None)  # original downloaded file
    transformed_path: Mapped[Optional[str]] = mapped_column(Text, default=None)
    youtube_video_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)

    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)

    engagement_score: Mapped[float] = mapped_column(Float, default=0.0)
    velocity_score: Mapped[float] = mapped_column(Float, default=0.0)

    status: Mapped[VideoStatus] = mapped_column(
        SAEnum(VideoStatus), default=VideoStatus.PENDING, index=True
    )
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)

    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    curated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    transformed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)

    transform_jobs: Mapped[list["TransformJob"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Video {self.source}:{self.source_id} status={self.status.value}>"


class TransformJob(Base):
    __tablename__ = "transform_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    recipe: Mapped[dict] = mapped_column(JSON, default=dict)
    output_path: Mapped[Optional[str]] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)

    video: Mapped[Video] = relationship(back_populates="transform_jobs")


class QuotaUsage(Base):
    """Tracks daily YouTube upload quota usage."""

    __tablename__ = "quota_usage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    day: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    uploads: Mapped[int] = mapped_column(Integer, default=0)
    cap: Mapped[int] = mapped_column(Integer, default=50)
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)


class Creator(Base):
    """Discovered or seed creator/handle for the scraper to follow."""

    __tablename__ = "creators"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    handle: Mapped[str] = mapped_column(String(128), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)  # tiktok|instagram|reddit|youtube
    is_seed: Mapped[bool] = mapped_column(default=False, index=True)
    discovered_from: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    videos_scraped: Mapped[int] = mapped_column(Integer, default=0)
    last_scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class MetadataDraft(Base):
    """LLM-generated YouTube metadata draft for a video, editable before publish."""

    __tablename__ = "metadata_drafts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True, unique=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    detected_language: Mapped[str] = mapped_column(String(16), default="en")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    edited: Mapped[bool] = mapped_column(default=False)
