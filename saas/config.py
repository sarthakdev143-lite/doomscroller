"""SaaS settings. Separate from app.config on purpose.

app.config.settings belongs to the owner's single-tenant growth loop, and its .env turns real
YouTube uploads on. Nothing in saas/ may read that file: every value here comes from SAAS_* env vars
or the file named by SAAS_ENV_FILE (default .env.saas).

Ownership: the integrator owns this file. Agents may append fields ONLY inside their own marked
section, with Edit (never Write), and must give every field a safe default so tests run with no env.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SaasSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SAAS_",
        env_file=os.environ.get("SAAS_ENV_FILE", ".env.saas") or None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- core (integrator) -------------------------------------------------------------------
    env: str = "dev"  # dev | prod. prod turns on Secure cookies, HSTS and strict checks.
    brand_name: str = "Doomscroller"  # working name; the owner has not picked the final one
    public_url: str = "http://127.0.0.1:8700"
    host: str = "127.0.0.1"
    port: int = 8700
    database_url: str = "sqlite:///saas.db"
    secret_key: str = ""  # required in prod; dev derives an ephemeral one
    data_dir: str = "saas_data"  # uploads, job workdirs and finished clips, per user
    launch_mode: str = "waitlist"  # waitlist | open. In waitlist mode signup needs an invite.
    support_email: str = "support@example.com"
    admin_emails: str = ""  # comma-separated; these accounts get role=admin on verify

    # Proxy trust. Only honour CF-Connecting-IP / X-Forwarded-For when the app sits behind them,
    # or any client can forge its own IP and walk past every per-IP limit.
    trust_proxy: str = "none"  # none | cloudflare | forwarded

    # ---- auth (core agent) ---------------------------------------------------------------------
    session_days: int = 30
    google_client_id: str = ""
    google_client_secret: str = ""
    smtp_host: str = ""  # empty -> emails are logged to the console (dev)
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # ---- billing (core agent) ------------------------------------------------------------------
    default_currency: str = "USD"
    signup_free_credits: int = 30  # minutes of source video, granted once per verified identity
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # ---- jobs (core agent) ---------------------------------------------------------------------
    max_upload_mb: int = 2048
    allowed_source_domains: str = (
        "youtube.com,youtu.be,m.youtube.com,vimeo.com,twitch.tv,loom.com,x.com,twitter.com,"
        "tiktok.com,instagram.com,facebook.com,fb.watch,drive.google.com,dropbox.com,rumble.com,kick.com"
    )
    worker_concurrency: int = 1
    clip_retention_days: int = 30

    # ---- tracking & security (tracking agent) --------------------------------------------------
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""
    geoip_city_db: str = ""  # path to GeoLite2-City.mmdb (optional)
    geoip_asn_db: str = ""  # path to GeoLite2-ASN.mmdb (optional)
    consent_mode: str = "eu_strict"  # eu_strict | always_ask | notice_only
    replay_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)

    # ---- mcp / public api (core agent, phase 2) ------------------------------------------------

    @property
    def is_prod(self) -> bool:
        return self.env.lower() == "prod"

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def allowed_source_domain_list(self) -> list[str]:
        return [d.strip().lower() for d in self.allowed_source_domains.split(",") if d.strip()]


@lru_cache
def get_settings() -> SaasSettings:
    return SaasSettings()


settings = get_settings()
