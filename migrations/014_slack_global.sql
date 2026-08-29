-- Global Slack interface channels and per-user project session context.

CREATE TABLE IF NOT EXISTS slack_interface_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (team_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_slack_interface_channels_channel
    ON slack_interface_channels (channel_id);

CREATE TABLE IF NOT EXISTS slack_project_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    project_human_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    UNIQUE (team_id, channel_id, thread_ts, user_id)
);

CREATE INDEX IF NOT EXISTS idx_slack_project_context_lookup
    ON slack_project_context (team_id, channel_id, thread_ts, user_id);
