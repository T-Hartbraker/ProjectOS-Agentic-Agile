-- Sponsor decision requests. Chat/free-text is not an approval grant.

CREATE TABLE IF NOT EXISTS governance_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_human_id TEXT NOT NULL UNIQUE,
    project_human_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_human_id TEXT,
    reason TEXT NOT NULL,
    impact TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    decided_by TEXT,
    decision_reason TEXT,
    execution_result TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT,
    CHECK (
        action IN (
            'sponsor_reserved',
            'release_approve',
            'cancel_job',
            'recover_salvage',
            'recover_reconcile',
            'governance_change'
        )
    ),
    CHECK (target_kind IN ('job', 'release', 'project', 'none')),
    CHECK (status IN ('OPEN', 'APPROVED', 'REJECTED'))
);

CREATE INDEX IF NOT EXISTS idx_governance_decisions_project_status
    ON governance_decisions (project_human_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS governance_decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    decision_human_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (event_type IN ('opened', 'approved', 'rejected', 'executed', 'execution_failed'))
);

CREATE INDEX IF NOT EXISTS idx_governance_decision_events_decision
    ON governance_decision_events (project_human_id, decision_human_id, id);
