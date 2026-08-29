-- GUI control plane: pause, idempotency, in-process operation locks.

CREATE TABLE IF NOT EXISTS project_orchestration_control (
    project_human_id TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    paused_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_idempotency_keys (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS api_operation_locks (
    lock_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
