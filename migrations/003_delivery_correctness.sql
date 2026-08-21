-- Delivery correctness: outcomes, assignment context, candidate invalidation

ALTER TABLE orchestration_jobs ADD COLUMN outcome TEXT;
ALTER TABLE orchestration_jobs ADD COLUMN superseded_by_job_id INTEGER
    REFERENCES orchestration_jobs(id) ON DELETE SET NULL;
ALTER TABLE orchestration_jobs ADD COLUMN assignment_json TEXT;
ALTER TABLE orchestration_jobs ADD COLUMN allows_no_change INTEGER NOT NULL DEFAULT 0
    CHECK (allows_no_change IN (0, 1));

CREATE TABLE IF NOT EXISTS candidate_invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_job_id INTEGER NOT NULL REFERENCES orchestration_jobs(id),
    invalidated_candidate_sha TEXT,
    reason TEXT NOT NULL,
    rework_job_id INTEGER REFERENCES orchestration_jobs(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
