-- Allowlisted release artifacts, keyed by project/release/artifact IDs only.
-- Content is stored as a blob so downloads never take a filesystem path.

CREATE TABLE IF NOT EXISTS release_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    release_human_id TEXT NOT NULL,
    artifact_human_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    kind TEXT NOT NULL,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_human_id, release_human_id, artifact_human_id)
);

CREATE INDEX IF NOT EXISTS idx_release_artifacts_release
    ON release_artifacts (project_human_id, release_human_id);
