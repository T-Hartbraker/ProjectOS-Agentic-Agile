-- Allow inconclusive assurance verdicts in qa_evidence.

PRAGMA foreign_keys = OFF;

CREATE TABLE qa_evidence_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    delivery_job_id INTEGER REFERENCES orchestration_jobs(id) ON DELETE SET NULL,
    assurance_job_id INTEGER REFERENCES orchestration_jobs(id) ON DELETE SET NULL,
    candidate_git_sha TEXT NOT NULL,
    assurance_role TEXT NOT NULL,
    result TEXT NOT NULL,
    defect_human_id TEXT,
    evidence_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    run_id TEXT,
    remediation_cycle INTEGER NOT NULL DEFAULT 0,
    CHECK (result IN ('pass', 'fail', 'stale_rejected', 'pending', 'inconclusive'))
);

INSERT INTO qa_evidence_new (
    id, project_human_id, repository_root, delivery_job_id, assurance_job_id,
    candidate_git_sha, assurance_role, result, defect_human_id, evidence_ref,
    created_at, run_id, remediation_cycle
)
SELECT
    id, project_human_id, repository_root, delivery_job_id, assurance_job_id,
    candidate_git_sha, assurance_role, result, defect_human_id, evidence_ref,
    created_at, run_id, remediation_cycle
FROM qa_evidence;

DROP TABLE qa_evidence;
ALTER TABLE qa_evidence_new RENAME TO qa_evidence;

CREATE INDEX IF NOT EXISTS idx_qa_evidence_run_candidate
    ON qa_evidence (project_human_id, run_id, candidate_git_sha);

PRAGMA foreign_keys = ON;
