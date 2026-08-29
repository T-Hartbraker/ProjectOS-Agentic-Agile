-- Slack Socket Mode envelope idempotency. Stores envelope ids only, never tokens.

CREATE TABLE IF NOT EXISTS slack_socket_envelopes (
    envelope_id TEXT PRIMARY KEY,
    payload_type TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
