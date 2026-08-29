-- Idempotent Slack delivery-event notices. Not a log of every worker event.

CREATE TABLE IF NOT EXISTS slack_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_human_id TEXT NOT NULL UNIQUE,
    project_human_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    entity_human_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    team_id TEXT NOT NULL DEFAULT '',
    thread_ts TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    dashboard_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_human_id, kind, entity_human_id),
    CHECK (
        kind IN (
            'iteration_review_ready',
            'sponsor_decision_required',
            'blocking_qa_failure',
            'release_ready',
            'released',
            'recovery_failure'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_slack_notifications_project
    ON slack_notifications (project_human_id, created_at DESC);
