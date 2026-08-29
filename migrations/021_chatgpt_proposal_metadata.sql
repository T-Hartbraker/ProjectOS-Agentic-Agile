-- Immutable proposal metadata and persisted execution results.

ALTER TABLE slack_chatgpt_proposals ADD COLUMN human_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE slack_chatgpt_proposals ADD COLUMN result_text TEXT;
