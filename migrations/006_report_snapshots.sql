-- Non-authoritative report snapshots. Live project/job state is not stored here.

CREATE TABLE IF NOT EXISTS report_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_human_id TEXT NOT NULL,
    project_human_id TEXT NOT NULL,
    report_kind TEXT NOT NULL,
    revision TEXT NOT NULL,
    iteration_human_id TEXT,
    release_human_id TEXT,
    generated_at TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    UNIQUE (project_human_id, snapshot_human_id),
    UNIQUE (project_human_id, report_kind, revision)
);

CREATE INDEX IF NOT EXISTS idx_report_snapshots_project
    ON report_snapshots (project_human_id, saved_at DESC);
