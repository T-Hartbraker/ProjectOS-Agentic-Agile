-- Slack-originated projectctl items. Slack evidence is metadata, not triage authority.

CREATE TABLE IF NOT EXISTS slack_intake_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    item_kind TEXT NOT NULL,
    item_human_id TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    message_ts TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (team_id, channel_id, message_ts, item_kind),
    CHECK (item_kind IN ('defect', 'feedback'))
);

CREATE INDEX IF NOT EXISTS idx_slack_intake_items_project
    ON slack_intake_items (project_human_id, created_at DESC);
