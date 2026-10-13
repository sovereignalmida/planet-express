"""Mission tables for store schema v6 (contract §2).

Semicolon-delimited so core.store can run it statement by statement inside one BEGIN IMMEDIATE.
Keep semicolons out of comments for the same reason. One addition to the contract: mission_leases
carries `suspect_at`, because expiry must mark a lease suspect without freeing it (§2.1) and the
contract's column list has nowhere to record that.
"""

DDL_V6 = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    origin TEXT NOT NULL,
    incident_id TEXT REFERENCES incidents(id),
    priority INTEGER NOT NULL DEFAULT 50,
    status TEXT NOT NULL,
    tier TEXT NOT NULL,
    goal TEXT NOT NULL,
    success_criteria TEXT NOT NULL,
    grant_json TEXT NOT NULL,
    grant_hash TEXT NOT NULL,
    standing_grant_id TEXT,
    runtime TEXT NOT NULL,
    session_ref TEXT,
    cancel_requested_at INTEGER,
    cancel_requested_by TEXT,
    plan_json TEXT,
    plan_hash TEXT,
    checkpoint_id TEXT,
    verification_plan_json TEXT,
    verification_result_json TEXT,
    outcome TEXT,
    outcome_flags TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    closed_at INTEGER,
    schema_ver INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS missions_status ON missions(status);
CREATE TABLE IF NOT EXISTS mission_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    body_json TEXT NOT NULL,
    redaction_applied INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS mission_events_mission ON mission_events(mission_id, seq);
CREATE TABLE IF NOT EXISTS mission_leases (
    resource TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    fence INTEGER NOT NULL,
    acquired_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_heartbeat INTEGER NOT NULL,
    suspect_at INTEGER
);
CREATE INDEX IF NOT EXISTS mission_leases_mission ON mission_leases(mission_id);
CREATE TABLE IF NOT EXISTS standing_grants (
    id TEXT PRIMARY KEY,
    scope_json TEXT NOT NULL,
    tier TEXT NOT NULL,
    window_json TEXT,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_approvals (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    phase TEXT NOT NULL,
    bound_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    operator TEXT NOT NULL,
    elevated INTEGER NOT NULL,
    decided_at INTEGER NOT NULL,
    UNIQUE (mission_id, phase, bound_hash)
);
CREATE TABLE IF NOT EXISTS mission_sessions (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    token_hash TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    kind TEXT NOT NULL,
    cgroup_path TEXT,
    leader_pid INTEGER,
    session_uid INTEGER UNIQUE,
    fence_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_session ON mission_sessions(mission_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS resource_fences (
    resource TEXT PRIMARY KEY,
    fence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_artifacts (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    name TEXT,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    path TEXT NOT NULL
)
"""
