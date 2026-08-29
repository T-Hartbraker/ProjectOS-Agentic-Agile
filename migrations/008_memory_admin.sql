-- Governed memory administration. History is preserved; ACTIVE injection set is unchanged for retired/superseded.

CREATE TABLE IF NOT EXISTS agent_memories_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_human_id TEXT NOT NULL UNIQUE,
    project_human_id TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'AGENT_MEMORY',
    memory_key TEXT NOT NULL,
    title TEXT NOT NULL,
    evidence_ref TEXT,
    source_job_human_id TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    last_validated_at TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    promotion_mode TEXT NOT NULL DEFAULT 'AUTO_LEARNED',
    rejection_code TEXT,
    rejection_reason TEXT,
    superseded_by_memory_human_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_human_id, agent_role, memory_key),
    CHECK (memory_kind IN ('AGENT_MEMORY', 'OTHER')),
    CHECK (status IN ('ACTIVE', 'REJECTED', 'RETIRED', 'SUPERSEDED')),
    CHECK (promotion_mode IN ('AUTO_LEARNED')),
    CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (occurrence_count >= 1)
);

INSERT INTO agent_memories_v2 (
    id, memory_human_id, project_human_id, agent_role, memory_kind, memory_key,
    title, evidence_ref, source_job_human_id, confidence, occurrence_count,
    last_validated_at, status, promotion_mode, rejection_code, rejection_reason,
    superseded_by_memory_human_id, created_at, updated_at
)
SELECT
    id, memory_human_id, project_human_id, agent_role, memory_kind, memory_key,
    title, evidence_ref, source_job_human_id, confidence, occurrence_count,
    last_validated_at, status, promotion_mode, rejection_code, rejection_reason,
    NULL, created_at, updated_at
FROM agent_memories;

DROP TABLE agent_memories;
ALTER TABLE agent_memories_v2 RENAME TO agent_memories;

CREATE INDEX IF NOT EXISTS idx_agent_memories_project_status
    ON agent_memories (project_human_id, status, agent_role);

CREATE TABLE IF NOT EXISTS agent_memory_events_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    memory_human_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    job_human_id TEXT,
    actor TEXT,
    rejection_code TEXT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (event_type IN ('promoted', 'reinforced', 'rejected', 'retired', 'superseded'))
);

INSERT INTO agent_memory_events_v2 (
    id, project_human_id, memory_human_id, event_type, job_human_id, actor,
    rejection_code, rejection_reason, created_at
)
SELECT
    id, project_human_id, memory_human_id, event_type, job_human_id, NULL,
    rejection_code, rejection_reason, created_at
FROM agent_memory_events;

DROP TABLE agent_memory_events;
ALTER TABLE agent_memory_events_v2 RENAME TO agent_memory_events;

CREATE INDEX IF NOT EXISTS idx_agent_memory_events_project
    ON agent_memory_events (project_human_id, created_at DESC);
