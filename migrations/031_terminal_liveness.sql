-- Terminal liveness: release_record_id on events, durable run next actions, outbox blocking.

PRAGMA foreign_keys = OFF;

ALTER TABLE projectos_events ADD COLUMN release_record_id TEXT;

CREATE INDEX IF NOT EXISTS idx_projectos_events_release_record
    ON projectos_events (run_id, release_record_id, occurred_at);

CREATE TABLE IF NOT EXISTS run_next_actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (
        action_type IN (
            'EXECUTABLE_JOB',
            'SCHEDULED_RETRY',
            'REMEDIATION_WORK',
            'ACTIVE_ASSESSMENT',
            'PM_QUEUE'
        )
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'claimed', 'completed', 'cancelled')
    ),
    orchestration_job_id INTEGER,
    remediation_work_id TEXT,
    due_at TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_run_next_actions_run
    ON run_next_actions (run_id, status);

CREATE TABLE event_outbox_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    subscriber TEXT NOT NULL DEFAULT 'slack',
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'claimed', 'blocked', 'delivered', 'dead', 'unroutable')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT,
    claimed_by TEXT,
    claim_expires_at TEXT,
    blocked_by_outbox_id INTEGER,
    FOREIGN KEY (event_id) REFERENCES projectos_events (event_id)
);

INSERT INTO event_outbox_v2 (
    id, event_id, subscriber, idempotency_key, payload_json, status,
    attempts, last_error, created_at, delivered_at, claimed_by, claim_expires_at
)
SELECT
    id, event_id, subscriber, idempotency_key, payload_json, status,
    attempts, last_error, created_at, delivered_at, claimed_by, claim_expires_at
FROM event_outbox;

DROP TABLE event_outbox;
ALTER TABLE event_outbox_v2 RENAME TO event_outbox;

CREATE INDEX IF NOT EXISTS idx_event_outbox_pending
    ON event_outbox (subscriber, status, created_at);

PRAGMA foreign_keys = ON;
