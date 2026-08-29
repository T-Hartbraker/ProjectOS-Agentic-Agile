-- Active ProjectOS command threads in Slack (separate from ChatGPT advisor threads).

CREATE TABLE IF NOT EXISTS slack_projectos_threads (
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (team_id, channel_id, thread_ts)
);

CREATE INDEX IF NOT EXISTS idx_slack_projectos_threads_lookup
    ON slack_projectos_threads (team_id, channel_id, thread_ts);
