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
    smtp_ssl: bool = False  # implicit TLS (port 465) instead of STARTTLS
    session_idle_hours: int = 72  # a session unused this long is dead even inside session_days
    admin_session_idle_minutes: int = 60  # admins idle out much sooner
    reauth_minutes: int = 10  # window in which sensitive actions skip the password prompt
    password_min_length: int = 10
    hibp_enabled: bool = True  # breached-password check (k-anonymity range API); fails open
    hibp_timeout_s: float = 2.0
    verify_token_hours: int = 48
    reset_token_minutes: int = 60
    magic_token_minutes: int = 15
    login_max_per_account_15m: int = 10  # failed logins per account before throttling
    login_max_per_ip_15m: int = 30  # login attempts per IP
    signup_max_per_ip_hour: int = 5

    # ---- billing (core agent) ------------------------------------------------------------------
    default_currency: str = "USD"
    signup_free_credits: int = 30  # minutes of source video, granted once per verified identity
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    stripe_api_base: str = "https://api.stripe.com"
    razorpay_api_base: str = "https://api.razorpay.com"
    webhook_tolerance_s: int = 300  # Stripe signature timestamp tolerance
    # Which provider takes which currency. "dev" is only ever honoured when env=dev. Buy Me a Coffee is
    # primary (no company needed); stripe / razorpay / dodo stay dormant until configured and selected.
    payment_provider_default: str = "bmc"
    payment_provider_inr: str = "bmc"
    bmc_page_url: str = ""  # e.g. https://buymeacoffee.com/<handle>; checkout redirects here
    bmc_webhook_secret: str = ""  # the webhook's "Signing Secret" from the BMC dashboard
    bmc_max_event_age_hours: int = 72  # events whose signed `created` is older are ignored (replays)
    bmc_accept_test_events: bool = False  # dashboard test events (live_mode=false); never in prod
    referral_reward_credits: int = 30  # to the referrer, after the referred user's first paid purchase
    referral_max_rewards: int = 50  # per referrer, lifetime
    promo_pending_hours: int = 24  # an unpaid checkout holds its promo redemption this long

    # ---- jobs (core agent) ---------------------------------------------------------------------
    max_upload_mb: int = 2048
    allowed_source_domains: str = (
        "youtube.com,youtu.be,m.youtube.com,vimeo.com,twitch.tv,loom.com,x.com,twitter.com,"
        "tiktok.com,instagram.com,facebook.com,fb.watch,drive.google.com,dropbox.com,rumble.com,kick.com"
    )
    worker_concurrency: int = 1
    clip_retention_days: int = 30
    engine: str = "mock"  # mock | clipper. The real clipper needs ffmpeg, keys and the pipeline seam.
    ffprobe_bin: str = "ffprobe"
    ffmpeg_bin: str = "ffmpeg"  # brand-kit video memes are re-encoded with it
    ytdlp_timeout_s: int = 30  # metadata extraction for an estimate
    ytdlp_proxy: str = ""
    estimate_cache_s: int = 600
    dns_timeout_s: float = 3.0
    upload_ttl_hours: int = 48  # an upload no job used is deleted after this
    job_heartbeat_s: int = 15
    job_stuck_minutes: int = 10  # no heartbeat this long -> requeue (or fail after max attempts)
    job_max_attempts: int = 2
    job_timeout_minutes: int = 120
    worker_poll_s: float = 2.0
    signed_url_ttl_s: int = 3600

    # ---- tracking & security (tracking agent) --------------------------------------------------
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""
    geoip_city_db: str = ""  # path to GeoLite2-City.mmdb (optional)
    geoip_asn_db: str = ""  # path to GeoLite2-ASN.mmdb (optional)
    consent_mode: str = "eu_strict"  # eu_strict | always_ask | notice_only
    replay_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    # What eu_strict does when the country is unknown (no GeoIP db, not behind Cloudflare): ask | notice.
    consent_unknown_country: str = "ask"
    tracking_enabled: bool = True  # false: /api/t accepts and drops everything; forms still get fp
    tracking_allowed_origins: str = ""  # extra beacon origins (comma-separated); public_url's always counts
    event_retention_days: int = 90  # raw tracker events
    replay_retention_days: int = 30
    replay_max_session_mb: int = 20  # compressed replay bytes kept per session
    admin_ip_allowlist: str = ""  # comma-separated IPs/CIDRs; empty = any IP (admin still needs role+TOTP)
    # CSP. Self-hosted assets + Turnstile are always allowed; add hosts here instead of editing code.
    csp_extra_script_src: str = ""
    csp_extra_connect_src: str = ""
    csp_extra_frame_src: str = ""
    csp_extra_img_src: str = ""
    csp_extra_form_action: str = ""
    csp_strict_style: bool = False  # true drops 'unsafe-inline' from style-src (breaks style="" attributes)
    hsts_max_age: int = 63072000  # only sent when env=prod
    # Global per-IP limits (requests per minute) applied by the middleware before any route runs.
    rate_limit_global_per_min: int = 600
    rate_limit_write_per_min: int = 120  # state-changing /api/* and /auth/* only
    max_body_kb: int = 256  # default request-body cap; uploads, replay, beacons and webhooks have their own
    tor_exit_list_url: str = "https://check.torproject.org/torbulkexitlist"
    threat_feed_refresh_hours: int = 6
    background_maintenance: bool = True  # hourly retention cleanup + Tor list refresh (never under pytest)
    disposable_extra_domains: str = ""  # comma-separated, blocked on top of the vendored list
    disposable_allow_domains: str = ""  # comma-separated false positives to let through
    form_min_fill_ms: int = 1200  # identity forms submitted faster than this after render are bots
    form_max_age_hours: int = 24
    risk_block_score: int = 80  # assess_signup blocks at or above this score
    risk_no_credit_score: int = 40  # ...and withholds free credits at or above this one
    signup_ip_hourly_limit: int = 5  # identity creations (waitlist + signup) per IP per hour
    waitlist_referral_bump: int = 5  # places moved up per confirmed, non-fraudulent referral
    waitlist_confirm_hours: int = 72
    waitlist_invite_days: int = 14
    # Admin cost-per-job estimate overrides (JSON); defaults come from docs/saas/costs-and-pricing.md.
    cost_rates_json: str = ""

    # ---- mcp / public api (core agent, phase 2) ------------------------------------------------
    mcp_enabled: bool = True
    api_rate_per_minute: int = 60  # per API key, shared by /api/v1 and /mcp
    api_estimate_per_hour: int = 120  # estimates spawn yt-dlp; cap them per key/user

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
