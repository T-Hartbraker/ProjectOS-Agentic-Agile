-- At most one pending proposal per sponsor/thread/project.

CREATE UNIQUE INDEX IF NOT EXISTS idx_slack_chatgpt_proposals_one_pending
    ON slack_chatgpt_proposals (team_id, channel_id, thread_ts, sponsor_user_id, project_human_id)
    WHERE status = 'pending';
