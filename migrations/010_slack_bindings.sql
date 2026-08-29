-- Slack channel/thread bindings. Slack identifiers are integration metadata,
-- not authoritative project state. Project identity stays in the registry.

CREATE TABLE IF NOT EXISTS slack_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_human_id TEXT NOT NULL UNIQUE,
    project_human_id TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (team_id, channel_id, thread_ts)
);

CREATE INDEX IF NOT EXISTS idx_slack_bindings_project
    ON slack_bindings (project_human_id, channel_id);

CREATE TABLE IF NOT EXISTS slack_message_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    message_ts TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (team_id, channel_id, message_ts)
);

CREATE INDEX IF NOT EXISTS idx_slack_message_refs_project
    ON slack_message_refs (project_human_id, created_at DESC);
