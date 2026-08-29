-- Organizational learning. AGENT_MEMORY is auto-learned; this is not an approval ledger.

CREATE TABLE IF NOT EXISTS agent_memories (
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_human_id, agent_role, memory_key),
    CHECK (memory_kind IN ('AGENT_MEMORY', 'OTHER')),
    CHECK (status IN ('ACTIVE', 'REJECTED')),
    CHECK (promotion_mode IN ('AUTO_LEARNED')),
    CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (occurrence_count >= 1)
);

CREATE INDEX IF NOT EXISTS idx_agent_memories_project_status
    ON agent_memories (project_human_id, status, agent_role);

CREATE TABLE IF NOT EXISTS agent_memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    memory_human_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    job_human_id TEXT,
    rejection_code TEXT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (event_type IN ('promoted', 'reinforced', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_events_project
    ON agent_memory_events (project_human_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_memory_injections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_human_id TEXT NOT NULL,
    memory_human_id TEXT NOT NULL,
    job_human_id TEXT NOT NULL,
    agent_run_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_injections_project
    ON agent_memory_injections (project_human_id, created_at DESC);
