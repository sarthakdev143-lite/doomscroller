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
    target_handles: str = '["@khaby.lame","@charlidamelio","@mrbeast","@bellapoarch","@addisonre","@zachking","@kimberly.loaiza","@fifaworldcup","@realmadrid","@bts_official_bighit","@therock","@willsmith","@domelipa","@billieeilish","@cznburak","@lamine.yamal","@jasonderulo","@kyliejenner","@selenagomez","@kylizzlemynizzl"]'
    target_hashtags: str = '["reels","shorts","viral","trending","entertainment","spotlight"]'
    enabled_platforms: str = '["instagram","snapchat","youtube"]'
    proxy_url: str = ""
    # Browser cookies for yt-dlp (e.g. "edge" or "edge,Default" or "chrome,Profile 1")
    # Leave empty to skip cookie extraction.
    cookies_from_browser: str = ""

    # Storage
    content_dir: str = "./content"
    source_video_max_age_days: int = 7

    # Transform
    transform_width: int = 1080
    transform_height: int = 1920
    transform_blur_sigma: int = 20
    transform_caption_font: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Long-form chopper — turns long downloaded videos into multiple Shorts
    long_video_min_duration_sec: int = 120   # only chop videos >= 2 minutes
    clip_target_sec: int = 59               # target length of each Shorts clip
    max_clips_per_video: int = 3            # max clips per source video
    min_clip_duration_sec: int = 30         # discard clips shorter than this

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
            path = (PROJECT_ROOT / "doomscroller.db").as_posix()
            return f"sqlite:///{path}"
        # Accept bare file paths or `file:` URIs and normalise to SQLAlchemy form.
        if url.startswith("file:"):
            url = url[len("file:"):]
            if url.startswith("//"):
                url = url[2:]
        if "://" not in url:
            # Convert Windows backslashes to forward slashes for SQLite URL
            import pathlib
            p = pathlib.Path(url)
            url_path = p.as_posix()
            return f"sqlite:///{url_path}"
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
            return ["instagram", "snapchat", "youtube"]

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
