-- ProjectOS orchestration state (Phase 2)

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS registered_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    registry_path TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    identity_snapshot_json TEXT,
    validated_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_human_id, repository_root)
);

CREATE TABLE IF NOT EXISTS orchestration_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    human_id TEXT NOT NULL UNIQUE,
    project_human_id TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    iteration_human_id TEXT,
    work_item_type TEXT,
    work_item_human_id TEXT,
    agent_role TEXT NOT NULL,
    queue TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    priority INTEGER NOT NULL DEFAULT 100,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    worktree_name TEXT,
    worktree_path TEXT,
    base_git_sha TEXT,
    candidate_git_sha TEXT,
    requires_worktree INTEGER NOT NULL DEFAULT 0 CHECK (requires_worktree IN (0, 1)),
    identity_snapshot_json TEXT,
    output_ref TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    ready_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (
        status IN (
            'QUEUED',
            'READY',
            'LEASED',
            'RUNNING',
            'SUCCEEDED',
            'FAILED',
            'BLOCKED',
            'RETRY_WAIT',
            'CANCELLED'
        )
    )
);

CREATE TABLE IF NOT EXISTS orchestration_job_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES orchestration_jobs(id) ON DELETE CASCADE,
    depends_on_job_id INTEGER NOT NULL REFERENCES orchestration_jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (job_id, depends_on_job_id),
    CHECK (job_id != depends_on_job_id)
);

CREATE TABLE IF NOT EXISTS worker_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE REFERENCES orchestration_jobs(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    leased_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES orchestration_jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    status TEXT,
    message TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES orchestration_jobs(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    cursor_command_json TEXT,
    prompt_ref TEXT,
    output_ref TEXT,
    stdout_ref TEXT,
    stderr_ref TEXT,
    exit_code INTEGER,
    started_at TEXT,
    ended_at TEXT,
    duration_ms INTEGER,
    worktree_name TEXT,
    worktree_path TEXT,
    base_git_sha TEXT,
    candidate_git_sha TEXT,
    dirty INTEGER,
    usage_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scheduler_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_tick_at TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS iteration_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    iteration_human_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_human_id, iteration_human_id)
);

CREATE TABLE IF NOT EXISTS release_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    release_human_id TEXT NOT NULL,
    candidate_git_sha TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_human_id, release_human_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_queue
    ON orchestration_jobs (status, queue, priority DESC, ready_at ASC);

CREATE INDEX IF NOT EXISTS idx_jobs_worktree_active
    ON orchestration_jobs (worktree_name, status);

CREATE INDEX IF NOT EXISTS idx_leases_expires
    ON worker_leases (expires_at);

CREATE INDEX IF NOT EXISTS idx_run_events_job
    ON run_events (job_id, created_at);

CREATE INDEX IF NOT EXISTS idx_agent_runs_job
    ON agent_runs (job_id, created_at);
