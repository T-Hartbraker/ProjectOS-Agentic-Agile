-- Two-stage governed action: preview persists separately from execution result.

ALTER TABLE slack_chatgpt_proposals ADD COLUMN action_type TEXT NOT NULL DEFAULT '';
ALTER TABLE slack_chatgpt_proposals ADD COLUMN preview_result TEXT;
ALTER TABLE slack_chatgpt_proposals ADD COLUMN preview_generated_at TEXT;
ALTER TABLE slack_chatgpt_proposals ADD COLUMN risk TEXT NOT NULL DEFAULT 'low';
ALTER TABLE slack_chatgpt_proposals ADD COLUMN scope TEXT NOT NULL DEFAULT '';
