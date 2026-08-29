-- Slack Events API idempotency by event_id or channel+message ts (not envelope_id).

CREATE TABLE IF NOT EXISTS slack_event_dedup (
    dedup_key TEXT PRIMARY KEY,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    message_ts TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_slack_event_dedup_lookup
    ON slack_event_dedup (team_id, channel_id, message_ts);
