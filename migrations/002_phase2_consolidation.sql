-- Phase 2 consolidation: schedules, daemon, plans, QA evidence, integration, budgets

CREATE TABLE IF NOT EXISTS project_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    timezone TEXT NOT NULL DEFAULT 'UTC',
    cadence TEXT NOT NULL DEFAULT 'daily',
    local_time TEXT NOT NULL DEFAULT '09:00',
    approved_budget_tokens INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schedule_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    iteration_run_id INTEGER,
    UNIQUE (project_human_id, window_key)
);

CREATE TABLE IF NOT EXISTS daemon_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER,
    started_at TEXT,
    heartbeat_at TEXT,
    status TEXT NOT NULL DEFAULT 'stopped',
    lock_path TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pm_plan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    iteration_human_id TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    plan_json TEXT,
    output_ref TEXT,
    status TEXT NOT NULL DEFAULT 'accepted',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS qa_evidence (
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
    CHECK (result IN ('pass', 'fail', 'stale_rejected', 'pending'))
);

CREATE TABLE IF NOT EXISTS integration_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    iteration_human_id TEXT,
    source_job_ids_json TEXT NOT NULL,
    source_shas_json TEXT NOT NULL,
    integrated_sha TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    conflict_paths_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
        status IN (
            'planned',
            'integrating',
            'succeeded',
            'conflict',
            'blocked',
            'failed'
        )
    )
);

-- Expand iteration_runs with conductor checkpoint fields (additive via new table overlay)
CREATE TABLE IF NOT EXISTS iteration_run_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_run_id INTEGER NOT NULL REFERENCES iteration_runs(id) ON DELETE CASCADE,
    checkpoint TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ensure scheduler_state row exists pattern remains; add last_due_eval_at
ALTER TABLE scheduler_state ADD COLUMN last_due_eval_at TEXT;
ALTER TABLE scheduler_state ADD COLUMN last_dispatch_at TEXT;

-- Delivery source linkage on jobs for QA provenance
ALTER TABLE orchestration_jobs ADD COLUMN source_delivery_job_id INTEGER REFERENCES orchestration_jobs(id) ON DELETE SET NULL;
ALTER TABLE orchestration_jobs ADD COLUMN source_candidate_sha TEXT;
ALTER TABLE orchestration_jobs ADD COLUMN sponsor_authority TEXT;

INSERT OR IGNORE INTO daemon_state (id, status) VALUES (1, 'stopped');
INSERT OR IGNORE INTO scheduler_state (id) VALUES (1);
