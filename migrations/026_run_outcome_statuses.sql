-- Expand execution_runs.status for canonical run outcomes.

PRAGMA foreign_keys = OFF;

CREATE TABLE execution_runs_v2 (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    handoff_id TEXT,
    request_type TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'PLANNING', 'WAITING_APPROVAL', 'WAITING_FOR_SPONSOR', 'RUNNING', 'BLOCKED',
            'FAILED', 'COMPLETED', 'CANCELLED', 'ESCALATED'
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

INSERT INTO execution_runs_v2 (
    run_id, project_id, handoff_id, request_type, objective, status,
    current_phase, current_agent, progress, result_summary, evidence_json,
    started_at, completed_at, created_at
)
SELECT
    run_id, project_id, handoff_id, request_type, objective,
    CASE WHEN status = 'WAITING_APPROVAL' THEN 'WAITING_FOR_SPONSOR' ELSE status END,
    current_phase, current_agent, progress, result_summary, evidence_json,
    started_at, completed_at, created_at
FROM execution_runs;

DROP TABLE execution_runs;
ALTER TABLE execution_runs_v2 RENAME TO execution_runs;

CREATE INDEX IF NOT EXISTS idx_execution_runs_project
    ON execution_runs (project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_execution_runs_handoff
    ON execution_runs (handoff_id);

PRAGMA foreign_keys = ON;
