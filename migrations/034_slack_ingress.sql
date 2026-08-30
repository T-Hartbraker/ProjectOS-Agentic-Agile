CREATE TABLE IF NOT EXISTS slack_ingress_work (
    work_id TEXT PRIMARY KEY,
    envelope_id TEXT NOT NULL,
    work_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    claimed_by TEXT,
    claim_expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_slack_ingress_envelope
    ON slack_ingress_work(envelope_id);

CREATE INDEX IF NOT EXISTS idx_slack_ingress_status
    ON slack_ingress_work(status, created_at);
