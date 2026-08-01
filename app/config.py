"""Application configuration."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_env: str = "dev"
    use_mock: bool = True
    log_level: str = "INFO"

    # DB
    database_url: str = ""

    # Scheduler
    scrape_interval_hours: int = 6
    scrape_max_per_run: int = 20
    discovery_interval_hours: int = 24
    uploads_per_day: int = 3
    upload_interval_hours: int = 8
    min_approved_queue: int = 1
    auto_publish: bool = True

    # Scraping
    target_handles: str = "[]"
    target_hashtags: str = '["fyp","viral","shorts"]'
    enabled_platforms: str = '["tiktok","reddit","instagram"]'
    proxy_url: str = ""

    # Storage
    content_dir: str = "./content"
    source_video_max_age_days: int = 7

    # Transform
    transform_width: int = 1080
    transform_height: int = 1920
    transform_blur_sigma: int = 20
    transform_caption_font: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # YouTube
    youtube_client_secrets: str = ""
    youtube_token_path: str = "./secrets/youtube_token.json"
    max_uploads_per_day: int = 50
    youtube_default_title: str = "Shorts"
    youtube_default_tags: str = '["shorts","viral"]'
    youtube_default_category: int = 22
    youtube_default_privacy: str = "private"
    channel_niche: str = "general_viral"

    # LLM
    use_llm_metadata: bool = True
    llm_model: str = "glm-4-plus"
    llm_timeout_sec: int = 60

    # Dashboard
    dashboard_title: str = "Doomscroller"

    # Legacy Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ---- Derived helpers ----
    @property
    def db_url(self) -> str:
        url = self.database_url.strip() if self.database_url else ""
        if not url:
            return f"sqlite:///{PROJECT_ROOT / 'doomscroller.db'}"
        # Accept bare file paths or `file:` URIs and normalise to SQLAlchemy form.
        if url.startswith("file:"):
            url = url[len("file:"):]
            if url.startswith("//"):
                url = url[2:]
        if "://" not in url:
            return f"sqlite:///{url}"
        return url

    @property
    def content_path(self) -> Path:
        p = Path(self.content_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def handles_list(self) -> List[str]:
        try:
            return [str(x) for x in json.loads(self.target_handles or "[]")]
        except Exception:
            return []

    @property
    def hashtags_list(self) -> List[str]:
        try:
            return [str(x) for x in json.loads(self.target_hashtags or "[]")]
        except Exception:
            return ["fyp", "viral", "shorts"]

    @property
    def enabled_platforms_list(self) -> List[str]:
        try:
            return [str(x) for x in json.loads(self.enabled_platforms or "[]")]
        except Exception:
            return ["tiktok", "reddit", "instagram"]

    @property
    def tags_list(self) -> List[str]:
        try:
            return [str(x) for x in json.loads(self.youtube_default_tags or "[]")]
        except Exception:
            return ["shorts"]

    @field_validator("use_mock", "use_llm_metadata", "auto_publish", mode="before")
    @classmethod
    def _coerce_bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(v)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
