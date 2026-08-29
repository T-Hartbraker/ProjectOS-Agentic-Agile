-- Outbox atomic claims and remediation work sequencing.

PRAGMA foreign_keys = OFF;

ALTER TABLE remediation_work ADD COLUMN work_sequence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE remediation_work ADD COLUMN finding_owner TEXT;
ALTER TABLE remediation_work ADD COLUMN execution_queue TEXT;

CREATE TABLE event_outbox_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    subscriber TEXT NOT NULL DEFAULT 'slack',
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'claimed', 'delivered', 'dead', 'unroutable')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT,
    claimed_by TEXT,
    claim_expires_at TEXT,
    FOREIGN KEY (event_id) REFERENCES projectos_events (event_id)
);

INSERT INTO event_outbox_new (
    id, event_id, subscriber, idempotency_key, payload_json, status,
    attempts, last_error, created_at, delivered_at
)
SELECT
    id, event_id, subscriber, idempotency_key, payload_json, status,
    attempts, last_error, created_at, delivered_at
FROM event_outbox;

DROP TABLE event_outbox;
ALTER TABLE event_outbox_new RENAME TO event_outbox;

CREATE INDEX IF NOT EXISTS idx_event_outbox_pending
    ON event_outbox (subscriber, status, created_at);

PRAGMA foreign_keys = ON;
