-- Remediation work persistence and candidate-scoped QA evidence.

ALTER TABLE qa_evidence ADD COLUMN run_id TEXT;
ALTER TABLE qa_evidence ADD COLUMN remediation_cycle INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS remediation_work (
    work_item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    remediation_cycle INTEGER NOT NULL DEFAULT 1,
    finding_ids_json TEXT NOT NULL DEFAULT '[]',
    assigned_agent TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_criteria TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'ASSIGNED', 'RUNNING', 'COMPLETED', 'FAILED', 'BLOCKED')
    ),
    source_candidate_id TEXT,
    target_candidate_id TEXT,
    orchestration_job_id INTEGER REFERENCES orchestration_jobs(id),
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_remediation_work_run
    ON remediation_work (run_id, remediation_cycle);

CREATE INDEX IF NOT EXISTS idx_qa_evidence_run_candidate
    ON qa_evidence (project_human_id, run_id, candidate_git_sha);
