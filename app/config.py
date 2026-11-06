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
    scrape_target_pending: int = 20
    discovery_interval_hours: int = 24
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
    # A Netscape-format cookies.txt, used instead of reading a browser. Empty, or a missing file, means none.
    cookies_file: str = ""

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
    # Channel name burned into scraped videos and clips. Empty leaves it off.
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

    # How a wide source fits the 9:16 canvas:
    #   framed - the whole picture centred, over a zoomed and dimmed copy of itself
    #   fill   - cropped to fill the canvas, following faces and motion
    clip_layout: str = "framed"
    # Blur on the framed backdrop, chosen by the owner over a sharp one. 0 keeps it sharp.
    clip_backdrop_blur: int = 14
    # Long-form chopper — turns long downloaded videos into multiple Shorts
    max_clips_per_video: int = 5
    clip_min_sec: int = 15
    clip_max_sec: int = 59
    # No-speech clips have no sentences to end on, so they get a fixed length.
    clip_visual_sec: int = 30
    # Pauses longer than this are cut out of speech clips.
    clip_silence_gap_sec: float = 0.35
    clip_source_max_height: int = 1080
    # Speech clips: how far to tighten. Speed is pitch-preserved.
    clip_speed: float = 1.12
    clip_edit_gap_sec: float = 0.12
    clip_edit_target_sec: int = 28
    clip_sfx: bool = True
    # Music under every clip, from tracks the owner puts in this folder. Empty means <CONTENT_DIR>/music.
    clip_music: bool = True
    clip_music_dir: str = ""
    # How far below the levelled voice the music sits; it dips further while someone talks.
    clip_music_db: float = -16.0
    # Stock cutaways over the voice. Needs either key; without one, clips simply have none.
    clip_broll: bool = True
    pixabay_api_key: str = ""
    pexels_api_key: str = ""
    # Finished clips skip review and go straight to the publish queue.
    clip_auto_queue: bool = False
    clip_font: str = ""
    clip_ai_timeout_sec: int = 240
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    # Empty means PATH, then ~/.opencode/bin/opencode.
    opencode_bin: str = ""
    # Chosen by the owner for every call. It reads images too.
    opencode_model: str = "opencode-go/muse-spark-1.3-contributor"
    # Must accept images: it judges frames from no-speech footage.
    opencode_vision_model: str = "opencode-go/muse-spark-1.3-contributor"
    # When OpenCode fails, the Claude CLI takes the call, on the account it is logged in to.
    claude_fallback: bool = True
    claude_bin: str = ""
    claude_model: str = "sonnet"

    # YouTube
    youtube_client_secrets: str = ""
    youtube_token_path: str = "./secrets/youtube_token.json"
    max_uploads_per_day: int = 50
    youtube_default_title: str = "Shorts"
    youtube_default_tags: str = '["shorts","viral"]'
    youtube_default_category: int = 22
    youtube_default_privacy: str = "private"
    channel_niche: str = "general_viral"

    youtube_made_for_kids: bool = False

    # ---- Growth loop (app/growth) ----
    # Posts, measures and adapts on its own. It takes over publishing from AUTO_PUBLISH.
    growth_enabled: bool = False
    growth_uploads_per_day: int = 3
    # Local time for the posting windows and for where a day starts.
    growth_timezone: str = "UTC"
    # How long the loop tests hard, and how much of each phase goes to testing.
    growth_explore_days: int = 30
    growth_explore_rate: float = 0.5
    growth_settled_explore_rate: float = 0.1
    # A video is judged on its views at this age, so that old and new are compared fairly.
    growth_horizon_hours: int = 72
    growth_min_gap_minutes: int = 120
    # Below this many finished clips, the supply job queues another source.
    growth_min_inventory: int = 9
    growth_clips_per_source: int = 3
    # JSON list of channel or playlist links to cut into clips.
    growth_feeds: str = '[]'
    # Video files dropped here are cut into clips. Empty means <CONTENT_DIR>/inbox.
    growth_inbox_dir: str = ""

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
    def growth_feeds_list(self) -> List[str]:
        try:
            return [str(x) for x in json.loads(self.growth_feeds or "[]")]
        except Exception:
            return []

    @property
    def growth_inbox_path(self) -> Path:
        return Path(self.growth_inbox_dir) if self.growth_inbox_dir else self.content_path / "inbox"

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
        "upscale_sharpen", "clip_auto_queue", "clip_sfx", "clip_broll", "clip_music",
        "growth_enabled", "youtube_made_for_kids", "claude_fallback", mode="before",
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
