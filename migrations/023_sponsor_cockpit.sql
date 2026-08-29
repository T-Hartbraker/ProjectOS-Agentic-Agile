-- Enterprise Sponsor cockpit: handoffs, execution runs, activity events, Slack outbox.

CREATE TABLE IF NOT EXISTS sponsor_handoffs (
    handoff_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    sponsor_user_id TEXT NOT NULL,
    request_type TEXT NOT NULL,
    objective TEXT NOT NULL,
    rationale TEXT,
    scope TEXT,
    constraints_json TEXT,
    acceptance_intent TEXT,
    exclusions TEXT,
    desired_outputs_json TEXT,
    conversation_summary TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('DRAFT', 'VALIDATED', 'ACCEPTED_BY_PM', 'REJECTED', 'SUPERSEDED')
    ),
    run_id TEXT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    validated_at TEXT,
    accepted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sponsor_handoffs_thread
    ON sponsor_handoffs (team_id, channel_id, thread_ts, created_at);

CREATE TABLE IF NOT EXISTS execution_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    handoff_id TEXT,
    request_type TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'PLANNING', 'WAITING_APPROVAL', 'RUNNING', 'BLOCKED',
            'FAILED', 'COMPLETED', 'CANCELLED'
        )
    ),
    current_phase TEXT,
    current_agent TEXT,
    progress INTEGER NOT NULL DEFAULT 0,
    result_summary TEXT,
    evidence_json TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (handoff_id) REFERENCES sponsor_handoffs (handoff_id)
);

CREATE INDEX IF NOT EXISTS idx_execution_runs_project
    ON execution_runs (project_id, created_at);

CREATE INDEX IF NOT EXISTS idx_execution_runs_handoff
    ON execution_runs (handoff_id);

CREATE TABLE IF NOT EXISTS agent_activity_events (
    event_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    project_id TEXT NOT NULL,
    run_id TEXT,
    work_item_id TEXT,
    release_id TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    actor_role TEXT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    phase TEXT,
    status TEXT,
    progress_percent INTEGER,
    summary TEXT NOT NULL,
    detail TEXT,
    evidence_json TEXT,
    metadata_json TEXT,
    visibility TEXT NOT NULL DEFAULT 'SPONSOR' CHECK (
        visibility IN ('INTERNAL', 'SPONSOR', 'AUDIT_ONLY')
    ),
    detail_level TEXT NOT NULL DEFAULT 'normal' CHECK (
        detail_level IN ('milestone', 'normal', 'verbose')
    )
);

CREATE INDEX IF NOT EXISTS idx_agent_activity_run
    ON agent_activity_events (run_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_agent_activity_project
    ON agent_activity_events (project_id, occurred_at);

CREATE TABLE IF NOT EXISTS slack_activity_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    team_id TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'delivered', 'failed', 'dead')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_slack_outbox_pending
    ON slack_activity_outbox (status, created_at);

CREATE TABLE IF NOT EXISTS slack_cockpit_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    activity_detail_level TEXT NOT NULL DEFAULT 'normal' CHECK (
        activity_detail_level IN ('milestone', 'normal', 'verbose')
    )
);

INSERT OR IGNORE INTO slack_cockpit_settings (id, activity_detail_level) VALUES (1, 'normal');
