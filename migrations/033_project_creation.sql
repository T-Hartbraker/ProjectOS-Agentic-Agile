-- Governed Slack new-project intake: idempotency and PRJ ID reservations.

CREATE TABLE IF NOT EXISTS slack_project_creations (
    dedup_key TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_ts TEXT NOT NULL,
    event_id TEXT NOT NULL DEFAULT '',
    project_human_id TEXT NOT NULL,
    handoff_id TEXT,
    run_id TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    objective TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_slack_project_creations_lookup
    ON slack_project_creations (team_id, channel_id, message_ts);

CREATE TABLE IF NOT EXISTS project_id_reservations (
    project_human_id TEXT PRIMARY KEY,
    reserved_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'allocator'
);
