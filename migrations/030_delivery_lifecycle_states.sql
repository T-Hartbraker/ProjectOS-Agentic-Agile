-- Extend delivery release lifecycle and publication status for verified/local releases.

PRAGMA foreign_keys = OFF;

CREATE TABLE delivery_releases_new (
    release_record_id TEXT PRIMARY KEY,
    project_human_id TEXT NOT NULL,
    release_human_id TEXT NOT NULL,
    version TEXT NOT NULL,
    candidate_git_sha TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        lifecycle_status IN (
            'planned',
            'candidate',
            'qa_passed',
            'packaged',
            'verified',
            'local_complete',
            'released'
        )
    ),
    build_executor TEXT,
    build_id TEXT,
    publication_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        publication_status IN (
            'pending',
            'in_progress',
            'published',
            'failed',
            'partial',
            'local_complete'
        )
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

INSERT INTO delivery_releases_new (
    release_record_id, project_human_id, release_human_id, version, candidate_git_sha,
    lifecycle_status, build_executor, build_id, publication_status, github_release_url,
    github_tag, manifest_sha256, slack_announced, proposal_id, approval_message_ts,
    sponsor_user_id, last_error, created_at, updated_at
)
SELECT
    release_record_id, project_human_id, release_human_id, version, candidate_git_sha,
    lifecycle_status, build_executor, build_id, publication_status, github_release_url,
    github_tag, manifest_sha256, slack_announced, proposal_id, approval_message_ts,
    sponsor_user_id, last_error, created_at, updated_at
FROM delivery_releases;

DROP TABLE delivery_releases;
ALTER TABLE delivery_releases_new RENAME TO delivery_releases;

CREATE INDEX IF NOT EXISTS idx_delivery_releases_project
    ON delivery_releases (project_human_id, lifecycle_status);

PRAGMA foreign_keys = ON;
