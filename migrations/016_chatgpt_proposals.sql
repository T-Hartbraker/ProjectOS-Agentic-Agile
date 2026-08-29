-- ChatGPT action proposals with explicit Sponsor approval state machine.

CREATE TABLE IF NOT EXISTS slack_chatgpt_proposals (
    proposal_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    sponsor_user_id TEXT NOT NULL,
    project_human_id TEXT NOT NULL,
    intent TEXT NOT NULL,
    instruction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'approved', 'dispatched', 'completed', 'failed', 'expired', 'rejected')
    ),
    approval_message_ts TEXT,
    dispatched_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_slack_chatgpt_proposals_pending
    ON slack_chatgpt_proposals (team_id, channel_id, thread_ts, sponsor_user_id, status);

CREATE INDEX IF NOT EXISTS idx_slack_chatgpt_proposals_thread
    ON slack_chatgpt_proposals (team_id, channel_id, thread_ts, created_at);
