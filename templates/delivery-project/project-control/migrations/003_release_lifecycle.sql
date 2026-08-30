-- Release lifecycle fields for governed status transitions.

ALTER TABLE releases ADD COLUMN git_sha TEXT;
ALTER TABLE releases ADD COLUMN iteration_id INTEGER REFERENCES iterations(id) ON DELETE SET NULL;
ALTER TABLE releases ADD COLUMN artifact_ref TEXT;
ALTER TABLE releases ADD COLUMN qa_evidence_ref TEXT;
ALTER TABLE releases ADD COLUMN dirty_tree_exception TEXT;

CREATE INDEX IF NOT EXISTS idx_releases_status ON releases(status);
CREATE INDEX IF NOT EXISTS idx_releases_git_sha ON releases(git_sha);
