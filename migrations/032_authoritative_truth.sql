-- Authoritative truth: run lineage on jobs, deterministic retry scheduling.

PRAGMA foreign_keys = OFF;

ALTER TABLE orchestration_jobs ADD COLUMN run_id TEXT;
ALTER TABLE orchestration_jobs ADD COLUMN retry_at TEXT;

CREATE INDEX IF NOT EXISTS idx_orchestration_jobs_run_status
    ON orchestration_jobs (run_id, status);

CREATE INDEX IF NOT EXISTS idx_orchestration_jobs_retry_wait
    ON orchestration_jobs (status, retry_at);

PRAGMA foreign_keys = ON;
