-- Sponsor action audit trail for Advisor bridge failures and handoff attempts.

CREATE TABLE IF NOT EXISTS sponsor_action_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_id TEXT,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    sponsor_user_id TEXT NOT NULL DEFAULT '',
    project_human_id TEXT,
    message_text TEXT,
    project_resolution TEXT,
    action_intent TEXT,
    handoff_attempted INTEGER NOT NULL DEFAULT 0,
    failure_stage TEXT,
    error_class TEXT,
    error_detail TEXT,
    pm_reached INTEGER NOT NULL DEFAULT 0,
    mutation_occurred INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sponsor_action_audit_thread
    ON sponsor_action_audit (channel_id, thread_ts, created_at);
