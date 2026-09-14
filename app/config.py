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
    # Float so the scrape cadence can go sub-hourly; Spotlight rotates fresh
    # stories faster than the BFS exhausts related ones, so frequent short runs
    # out-yield rare deep ones.
    scrape_interval_hours: float = 6.0
    scrape_max_per_run: int = 20
    # HTTP requests one Spotlight crawl may spend walking the related-story
    # graph. Each request returns ~25 stories; most are already known once
    # the library is large, so a bigger budget is what finds fresh ones.
    scrape_max_requests: int = 12
    scrape_target_pending: int = 20
    discovery_interval_hours: int = 24
    # Transform throughput. Batch is how many APPROVED videos one run drains;
    # the interval is how often that run fires. Raise both to burn down a
    # large approved backlog faster than the default 3/hour.
    transform_batch_size: int = 3
    transform_interval_minutes: int = 60
    uploads_per_day: int = 8
    upload_interval_hours: int = 3
    upload_jitter_minutes: int = 45
    min_approved_queue: int = 1
    auto_publish: bool = True
    enable_publish: bool = True
    auto_discovery: bool = True
    auto_approve: bool = True
    auto_approve_veto_hours: int = 1
    # Minimum blended viral_score an unattended approval requires.
    auto_approve_min_viral_score: float = 0.45

    # Scraping
    target_handles: str = '["@khaby.lame","@charlidamelio","@mrbeast","@bellapoarch","@addisonre","@zachking","@kimberly.loaiza","@fifaworldcup","@realmadrid","@bts_official_bighit","@therock","@willsmith","@domelipa","@billieeilish","@cznburak","@lamine.yamal","@jasonderulo","@kyliejenner","@selenagomez","@kylizzlemynizzl"]'
    target_hashtags: str = '["reels","shorts","viral","trending","entertainment","spotlight"]'
    enabled_platforms: str = '["snapchat"]'
    viral_min_engagement: float = 0.15
    viral_min_velocity: float = 0.12
    viral_preferred_min_duration: int = 5
    viral_preferred_max_duration: int = 35
    # Absolute reach floor for Spotlight, which is already algorithmically
    # filtered upstream -- engagement rate alone is a poor gate there.
    viral_min_views: int = 25_000

    # Source-shape gates, applied to platform metadata before downloading and
    # re-checked against ffprobe afterwards.
    min_source_duration_sec: int = 3
    max_source_duration_sec: int = 179
    min_source_height: int = 480
    min_file_size_bytes: int = 50_000

    # ---- Channel focus ----
    # A channel that posts six languages teaches the recommender nothing about
    # who to show it to. Empty list = accept every language.
    target_languages: str = '["en"]'
    # When true, a video whose language could not be detected is kept. Snapchat
    # usually reports one, but not always, and dropping them is wasteful.
    allow_unknown_language: bool = True
    # Optional topic allowlist matched against the platform keywords, title and
    # description. Empty = accept every topic.
    target_topics: str = '[]'
    # Topics to reject outright even when they clear the safety blocklist.
    excluded_topics: str = '[]'
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
    # Empty means "autodetect a bold system font"; see transformers.fonts.
    transform_caption_font: str = ""

    # ---- Output variation ----
    # Republishing someone else's video verbatim is what YouTube's reused-content
    # policy targets. These add genuine, per-video differentiation.
    transform_variation: bool = True
    # Channel name burned into a corner watermark. Empty disables the watermark.
    brand_name: str = ""
    brand_watermark_opacity: float = 0.45
    # Burn the generated hook caption over the opening seconds.
    # Off by default: Spotlight clips usually arrive with the creator's own
    # burned-in captions plus Snapchat's attribution, so a third text layer
    # reads as clutter rather than branding. Enable it for clean sources.
    caption_enabled: bool = False
    caption_seconds: float = 3.0
    caption_max_chars: int = 42
    # Overlay a subscribe end card over the closing seconds (overlaid, not
    # appended, so it never adds duration to a Short).
    end_card_enabled: bool = True
    end_card_seconds: float = 2.0
    end_card_text: str = "Subscribe for more"
    # Per-video zoom and speed jitter ranges. Deterministic per video id.
    zoom_min: float = 1.02
    zoom_max: float = 1.08
    speed_min: float = 0.97
    speed_max: float = 1.04
    # Sharpen after upscaling; Spotlight sources are commonly 540x960.
    upscale_sharpen: bool = True

    # Trim the top/bottom bands of the source, as a percentage of height.
    # Spotlight clips carry Snapchat branding and the original creator handle
    # burned into those bands. Cropping them produces a cleaner Short, but it
    # also strips the platform's visible attribution, leaving only the credit
    # line in the description. Off by default: enable it deliberately.
    source_crop_top_pct: float = 0.0
    source_crop_bottom_pct: float = 0.0
    # How to get back to 9:16 after the band crop:
    #   fill - centre-crop the width, full bleed, harder upscale
    #   fit  - keep everything, blurred filler makes up the canvas
    source_crop_mode: str = "fill"
    # Which side of the width survives the fill re-crop. Snapchat stacks its
    # attribution against the left edge, so keeping the right half spends
    # width we were discarding anyway on removing the remnant.
    source_crop_keep: str = "right"

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
    def source_crop_active(self) -> bool:
        """Whether rendering removes the platform's burned-in attribution.

        When this is true the description credit is the only attribution left,
        so metadata generation treats it as mandatory.
        """
        return (self.source_crop_top_pct or 0) > 0 or (self.source_crop_bottom_pct or 0) > 0

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
    def languages_list(self) -> List[str]:
        try:
            return [str(x).lower() for x in json.loads(self.target_languages or "[]")]
        except Exception:
            return []

    @property
    def topics_list(self) -> List[str]:
        try:
            return [str(x).lower() for x in json.loads(self.target_topics or "[]")]
        except Exception:
            return []

    @property
    def excluded_topics_list(self) -> List[str]:
        try:
            return [str(x).lower() for x in json.loads(self.excluded_topics or "[]")]
        except Exception:
            return []

    @property
    def tags_list(self) -> List[str]:
        try:
            return [str(x) for x in json.loads(self.youtube_default_tags or "[]")]
        except Exception:
            return ["shorts"]

    @field_validator(
        "use_mock", "use_llm_metadata", "auto_publish", "enable_publish",
        "auto_discovery", "auto_approve", "allow_unknown_language",
        "transform_variation", "caption_enabled", "end_card_enabled",
        "upscale_sharpen", mode="before",
    )
    @classmethod
    def _coerce_bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(v)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
