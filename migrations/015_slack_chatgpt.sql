-- ChatGPT advisor conversation state for Slack threads.

CREATE TABLE IF NOT EXISTS slack_chatgpt_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    sponsor_user_id TEXT NOT NULL,
    project_human_id TEXT,
    openai_response_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    awaiting_projectos INTEGER NOT NULL DEFAULT 0 CHECK (awaiting_projectos IN (0, 1)),
    pending_proposal_json TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (team_id, channel_id, thread_ts)
);

CREATE INDEX IF NOT EXISTS idx_slack_chatgpt_threads_lookup
    ON slack_chatgpt_threads (team_id, channel_id, thread_ts);

CREATE TABLE IF NOT EXISTS slack_chatgpt_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL DEFAULT '',
    message_ts TEXT NOT NULL,
    user_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('sponsor', 'chatgpt', 'projectos')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (team_id, channel_id, thread_ts, message_ts)
);

CREATE INDEX IF NOT EXISTS idx_slack_chatgpt_messages_thread
    ON slack_chatgpt_messages (team_id, channel_id, thread_ts, created_at);
