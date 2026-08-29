-- Canonical ProjectOS domain events and subscriber outbox.

CREATE TABLE IF NOT EXISTS projectos_events (
    event_id TEXT PRIMARY KEY,
    event_version INTEGER NOT NULL DEFAULT 1,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    project_id TEXT NOT NULL,
    handoff_id TEXT,
    run_id TEXT,
    iteration_id TEXT,
    job_id TEXT,
    work_item_id TEXT,
    release_id TEXT,
    artifact_id TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    event_type TEXT NOT NULL,
    phase TEXT,
    status TEXT,
    severity TEXT NOT NULL DEFAULT 'info',
    progress INTEGER,
    summary TEXT NOT NULL,
    detail TEXT,
    evidence_json TEXT,
    metadata_json TEXT,
    visibility TEXT NOT NULL DEFAULT 'SPONSOR' CHECK (
        visibility IN ('INTERNAL', 'SPONSOR', 'AUDIT')
    ),
    detail_level TEXT NOT NULL DEFAULT 'normal' CHECK (
        detail_level IN ('milestone', 'normal', 'verbose')
    ),
    correlation_id TEXT,
    causation_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_projectos_events_run
    ON projectos_events (run_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_projectos_events_project
    ON projectos_events (project_id, occurred_at);

CREATE TABLE IF NOT EXISTS event_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    subscriber TEXT NOT NULL DEFAULT 'slack',
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'delivered', 'dead')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT,
    FOREIGN KEY (event_id) REFERENCES projectos_events (event_id)
);

CREATE INDEX IF NOT EXISTS idx_event_outbox_pending
    ON event_outbox (subscriber, status, created_at);
