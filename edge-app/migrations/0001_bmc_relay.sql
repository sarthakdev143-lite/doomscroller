-- Buy Me a Coffee deliveries held for the laptop (edge-app/src/relay.js, saas/billing/bmc_relay.py).
-- One row per distinct signed body. A row stays queued (acked_at IS NULL) until the laptop acknowledges an
-- outcome; acknowledged rows are deleted after RELAY_RETENTION_DAYS. Bodies hold supporter emails.
CREATE TABLE IF NOT EXISTS bmc_events (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- pull cursor, receipt order
  body_sha256  TEXT    NOT NULL UNIQUE,            -- idempotency: the same signed bytes are stored once
  event_id     TEXT,                               -- BMC's delivery-attempt id (1 for every test event)
  type         TEXT    NOT NULL,
  live_mode    INTEGER NOT NULL,                   -- 0 = dashboard test event
  created      INTEGER,                            -- BMC's signed `created` (unix seconds)
  attempt      INTEGER,                            -- BMC's delivery attempt number
  received_at  INTEGER NOT NULL,                   -- when the Worker accepted it (unix seconds)
  signature    TEXT    NOT NULL,                   -- x-signature-sha256 as received (lower-case hex)
  body_b64     TEXT    NOT NULL,                   -- the raw body, base64, byte-exact for re-verification
  acked_at     INTEGER,
  result       TEXT,                               -- the laptop's outcome: processed, duplicate, ignored, ...
  detail       TEXT,
  failures     INTEGER NOT NULL DEFAULT 0,         -- laptop-side errors reported for a still-queued event
  last_error   TEXT
);

CREATE INDEX IF NOT EXISTS bmc_events_queue ON bmc_events (acked_at, seq);
