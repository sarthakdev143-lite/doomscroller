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
from sqlalchemy.ext.mutable import MutableDict
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

    author: Mapped[str] = mapped_column(String(128), default="", index=True)
    author_name: Mapped[str] = mapped_column(String(256), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, default="")
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    media_url: Mapped[Optional[str]] = mapped_column(Text, default=None)
    language: Mapped[str] = mapped_column(String(16), default="")
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    local_path: Mapped[Optional[str]] = mapped_column(Text, default=None)  # original downloaded file
    transformed_path: Mapped[Optional[str]] = mapped_column(Text, default=None)
    youtube_video_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)

    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)

    engagement_score: Mapped[float] = mapped_column(Float, default=0.0)
    velocity_score: Mapped[float] = mapped_column(Float, default=0.0)
    reach_score: Mapped[float] = mapped_column(Float, default=0.0)
    viral_score: Mapped[float] = mapped_column(Float, default=0.0)

    status: Mapped[VideoStatus] = mapped_column(
        SAEnum(VideoStatus), default=VideoStatus.PENDING, index=True
    )
    # Which managed channel this clip is destined for. None = the legacy single channel.
    channel_id: Mapped[Optional[str]] = mapped_column(String(32), default=None, index=True)
    meta: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)

    source_published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
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


class ClipJob(Base):
    """One long video waiting to be, or having been, cut into clips."""

    __tablename__ = "clip_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(Text)  # link or local path
    count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    auto_queue: Mapped[Optional[bool]] = mapped_column(default=None)
    channel_id: Mapped[Optional[str]] = mapped_column(String(32), default=None, index=True)  # the managed channel to feed
    mined: Mapped[bool] = mapped_column(default=False)  # queued because an earlier clip of this source won
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)


class VideoSnapshot(Base):
    """A published video's counters at one moment, so videos can be compared at the same age."""

    __tablename__ = "video_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    age_hours: Mapped[float] = mapped_column(Float, default=0.0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)


class GrowthState(Base):
    """Named values the growth loop keeps between runs: today's plan, its start date, its focus."""

    __tablename__ = "growth_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class GrowthEvent(Base):
    """What the growth loop did and why, in words a person can read."""

    __tablename__ = "growth_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    video_id: Mapped[Optional[str]] = mapped_column(String(32), default=None, index=True)
    channel_id: Mapped[Optional[str]] = mapped_column(String(32), default=None, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class QuotaUsage(Base):
    """Tracks daily YouTube upload quota usage."""

    __tablename__ = "quota_usage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    day: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    uploads: Mapped[int] = mapped_column(Integer, default=0)
    cap: Mapped[int] = mapped_column(Integer, default=50)
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)


class Channel(Base):
    """One managed YouTube channel: a single niche, its own Google Cloud project and OAuth token.

    Per-channel settings (uploads/day, timezone, approval) override the global growth settings, so
    the loop can run several channels at once, each posting to its own account at its own pace.
    """

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # stable handle, e.g. "tech"
    name: Mapped[str] = mapped_column(String(128), default="")              # display, e.g. "Tech & Science"
    niche: Mapped[str] = mapped_column(String(256), default="")
    brand_name: Mapped[str] = mapped_column(String(64), default="")         # @handle for the on-screen credit
    token_path: Mapped[str] = mapped_column(Text, default="")               # this channel's YouTube OAuth token
    client_secrets: Mapped[str] = mapped_column(Text, default="")           # its own Cloud project's client_secret.json
    youtube_channel_id: Mapped[Optional[str]] = mapped_column(String(64), default=None)  # captured at oauth; guards a misclick
    uploads_per_day: Mapped[int] = mapped_column(Integer, default=3)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    require_approval: Mapped[bool] = mapped_column(default=True)
    # Opt-in trust: auto-approve a rendered clip scoring >= this (0 = off, a human reviews all).
    auto_approve_score: Mapped[float] = mapped_column(Float, default=0.0)
    # When a window would be missed with clips in review, auto-approve the best one rather than go dark.
    cadence_backstop: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    meta: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)


class Feed(Base):
    """A source channel or playlist URL that supplies exactly one managed channel (one niche per channel)."""

    __tablename__ = "feeds"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    url: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), index=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
    meta: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)


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
