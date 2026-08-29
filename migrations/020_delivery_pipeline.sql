-- Universal software delivery pipeline: release records, artifacts, gates, audit.

CREATE TABLE IF NOT EXISTS delivery_releases (
    release_record_id TEXT PRIMARY KEY,
    project_human_id TEXT NOT NULL,
    release_human_id TEXT NOT NULL,
    version TEXT NOT NULL,
    candidate_git_sha TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        lifecycle_status IN ('planned', 'candidate', 'qa_passed', 'released')
    ),
    build_executor TEXT,
    build_id TEXT,
    publication_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        publication_status IN ('pending', 'in_progress', 'published', 'failed', 'partial')
    ),
    github_release_url TEXT,
    github_tag TEXT,
    manifest_sha256 TEXT,
    slack_announced INTEGER NOT NULL DEFAULT 0 CHECK (slack_announced IN (0, 1)),
    proposal_id TEXT,
    approval_message_ts TEXT,
    sponsor_user_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_human_id, release_human_id)
);

CREATE INDEX IF NOT EXISTS idx_delivery_releases_project
    ON delivery_releases (project_human_id, lifecycle_status);

CREATE TABLE IF NOT EXISTS delivery_artifacts (
    artifact_id TEXT PRIMARY KEY,
    release_record_id TEXT NOT NULL,
    project_human_id TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    platform TEXT NOT NULL,
    architecture TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    source_git_sha TEXT NOT NULL,
    build_id TEXT,
    build_timestamp TEXT,
    local_build_path TEXT NOT NULL,
    published_url TEXT,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    signature_status TEXT NOT NULL DEFAULT 'not_configured',
    signature_identity TEXT,
    sbom_url TEXT,
    provenance_status TEXT NOT NULL DEFAULT 'pending',
    publication_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (release_record_id) REFERENCES delivery_releases(release_record_id)
);

CREATE INDEX IF NOT EXISTS idx_delivery_artifacts_release
    ON delivery_artifacts (release_record_id);

CREATE TABLE IF NOT EXISTS delivery_gate_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_record_id TEXT NOT NULL,
    gate_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'passed', 'failed', 'skipped', 'not_required')
    ),
    detail TEXT,
    evidence_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (release_record_id, gate_name),
    FOREIGN KEY (release_record_id) REFERENCES delivery_releases(release_record_id)
);

CREATE TABLE IF NOT EXISTS delivery_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_record_id TEXT,
    project_human_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    proposal_id TEXT,
    detail TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_delivery_audit_release
    ON delivery_audit_log (release_record_id, created_at);
