-- Waitlist (pre-registration) on D1. No double opt-in at launch: entries start as 'joined';
-- confirmed_at stays NULL until email sending exists.
--
-- Privacy: no raw IP is stored. ip_hash / net_hash are HMAC-SHA256(IP_HASH_SALT, ip or its /24 (/48 for
-- IPv6)), enough for duplicate, rate-limit and referral-fraud checks; country/city/ASN come from
-- Cloudflare's request.cf at join time.

CREATE TABLE IF NOT EXISTS waitlist (
  seq               INTEGER PRIMARY KEY AUTOINCREMENT,  -- join order
  id                TEXT    NOT NULL UNIQUE,            -- opaque public-ish id (wl_<hex>)
  email             TEXT    NOT NULL UNIQUE,            -- trimmed, lower-cased
  email_canonical   TEXT    NOT NULL,                   -- gmail dots / +tags collapsed (dedupe)
  role              TEXT,
  channel_url       TEXT,
  status            TEXT    NOT NULL DEFAULT 'joined',  -- joined | invited | removed
  created_at        TEXT    NOT NULL,                   -- ISO 8601 UTC
  confirmed_at      TEXT,                               -- reserved for double opt-in later
  rank_key          INTEGER NOT NULL,                   -- seq - bump * referrals_counted (lower = earlier)
  referral_code     TEXT    NOT NULL UNIQUE,
  referred_by_seq   INTEGER,                            -- the referrer's seq
  referral_status   TEXT,                               -- counted | rejected (on the referred entry)
  referral_reason   TEXT,                               -- why a referral did not count
  referrals_counted INTEGER NOT NULL DEFAULT 0,
  ip_hash           TEXT,
  net_hash          TEXT,                               -- /24 (IPv4) or /48 (IPv6) network
  country           TEXT,
  region            TEXT,
  city              TEXT,
  timezone          TEXT,
  asn               INTEGER,
  as_org            TEXT,
  colo              TEXT,
  user_agent        TEXT,
  referrer          TEXT,                               -- external referrer (origin + path)
  referrer_host     TEXT,
  landing_path      TEXT,
  utm_source        TEXT,
  utm_medium        TEXT,
  utm_campaign      TEXT,
  utm_term          TEXT,
  utm_content       TEXT,
  turnstile         INTEGER NOT NULL DEFAULT 0,         -- 1 = passed a Turnstile check
  flagged           INTEGER NOT NULL DEFAULT 0,         -- 1 = suspicious; never earns referral credit
  risk              TEXT                                -- comma-separated reasons
);

CREATE INDEX IF NOT EXISTS ix_waitlist_canonical ON waitlist (email_canonical);
CREATE INDEX IF NOT EXISTS ix_waitlist_rank      ON waitlist (status, flagged, rank_key, seq);
CREATE INDEX IF NOT EXISTS ix_waitlist_created   ON waitlist (created_at);
CREATE INDEX IF NOT EXISTS ix_waitlist_ip        ON waitlist (ip_hash, created_at);
CREATE INDEX IF NOT EXISTS ix_waitlist_net       ON waitlist (net_hash);
CREATE INDEX IF NOT EXISTS ix_waitlist_referrer  ON waitlist (referred_by_seq);

-- Fixed windows for a sliding-window rate limiter. Keys are hashes, never raw IPs or emails.
CREATE TABLE IF NOT EXISTS rate_limits (
  bucket       TEXT    NOT NULL,
  key          TEXT    NOT NULL,
  window_start INTEGER NOT NULL,  -- epoch seconds
  count        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket, key, window_start)
);
CREATE INDEX IF NOT EXISTS ix_rate_limits_window ON rate_limits (window_start);
