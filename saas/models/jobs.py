"""Job tables: uploads, clip jobs and the clips they produce.

Every row carries `user_id` and every query in saas.jobs is scoped by it, so one customer can never
read another's jobs, clips or files by guessing an id (ids are opaque anyway).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..common import new_id, utcnow
from ..db import Base
from .core import UTCDateTime

JOB_STATUSES = ("queued", "fetching", "transcribing", "selecting", "rendering", "done", "failed", "canceled")
ACTIVE_STATUSES = ("queued", "fetching", "transcribing", "selecting", "rendering")
RUNNING_STATUSES = ("fetching", "transcribing", "selecting", "rendering")
FINAL_STATUSES = ("done", "failed", "canceled")


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("upl_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(200), default="")  # sanitised, display only
    path: Mapped[str] = mapped_column(String(500))  # relative to SAAS_DATA_DIR
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    container: Mapped[str] = mapped_column(String(40), default="")
    credits: Mapped[int] = mapped_column(Integer, default=0)
    job_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("job_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    source_kind: Mapped[str] = mapped_column(String(8))  # url | upload
    source_url: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    upload_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    source_title: Mapped[str] = mapped_column(String(300), default="")
    source_duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    source_thumbnail: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    credits_reserved: Mapped[int] = mapped_column(Integer, default=0)
    credits_charged: Mapped[int] = mapped_column(Integer, default=0)
    funding: Mapped[str] = mapped_column(String(8), default="credits")  # credits | free
    shorts_reserved: Mapped[int] = mapped_column(Integer, default=0)
    shorts_charged: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    watermark: Mapped[bool] = mapped_column(Boolean, default=False)
    via: Mapped[str] = mapped_column(String(8), default="web")  # web | api | mcp
    api_key_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    meter: Mapped[dict] = mapped_column(JSON, default=dict)  # app.clipper.metering summary (admin cost view)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


Index("ix_jobs_queue", Job.status, Job.priority, Job.created_at)
Index("ix_jobs_user_created", Job.user_id, Job.created_at)


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("clp_"))
    job_id: Mapped[str] = mapped_column(String(40), ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    path: Mapped[str] = mapped_column(String(500))  # relative to SAAS_DATA_DIR
    thumbnail_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    language: Mapped[str] = mapped_column(String(8), default="")
    memes_used: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)


Index("ix_clips_user_created", Clip.user_id, Clip.created_at)


class BrandAsset(Base):
    """A customer's brand kit: one logo (re-encoded PNG) and their own memes (re-encoded GIF/PNG/MP4)."""

    __tablename__ = "brand_assets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("bra_"))
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # logo | meme
    path: Mapped[str] = mapped_column(String(500))  # relative to SAAS_DATA_DIR
    media_type: Mapped[str] = mapped_column(String(40))
    original_name: Mapped[str] = mapped_column(String(200), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
