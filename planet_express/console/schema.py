"""Console tables. Additive: `core.store.ddl_v6()` appends this with `IF NOT EXISTS`, exactly like Ship's Memory, so no schema
version bump and no migration guard is involved, and an older Core simply ignores the tables.

Semicolon-delimited (the store runs it statement by statement). Keep semicolons out of comments.
"""

DDL = """
CREATE TABLE IF NOT EXISTS console_conversations (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL UNIQUE REFERENCES missions(id),
    title TEXT NOT NULL,
    origin TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    agent_session_id TEXT,
    uid INTEGER,
    token_budget INTEGER NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_activity_at INTEGER NOT NULL,
    archived_at INTEGER
);
CREATE INDEX IF NOT EXISTS console_conversations_state ON console_conversations(state);
CREATE TABLE IF NOT EXISTS console_messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES console_conversations(id),
    at INTEGER NOT NULL,
    role TEXT NOT NULL,
    actor TEXT,
    turn INTEGER,
    text TEXT NOT NULL,
    consumed_turn INTEGER
);
CREATE INDEX IF NOT EXISTS console_messages_conversation ON console_messages(conversation_id, seq);
CREATE TABLE IF NOT EXISTS console_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES console_conversations(id),
    turn INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS console_plans_conversation ON console_plans(conversation_id, id);
CREATE TABLE IF NOT EXISTS console_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES console_conversations(id),
    plan_id INTEGER NOT NULL REFERENCES console_plans(id),
    option_index INTEGER NOT NULL,
    mission_id TEXT NOT NULL UNIQUE REFERENCES missions(id),
    grant_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at INTEGER NOT NULL,
    authorised_at INTEGER,
    authorised_by TEXT,
    reported TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS console_executions_conversation ON console_executions(conversation_id, id);
CREATE TABLE IF NOT EXISTS console_lessons (
    conversation_id TEXT NOT NULL REFERENCES console_conversations(id),
    card_id TEXT NOT NULL,
    PRIMARY KEY (conversation_id, card_id)
);
CREATE TABLE IF NOT EXISTS console_telegram (
    conversation_id TEXT PRIMARY KEY REFERENCES console_conversations(id),
    last_seq INTEGER NOT NULL DEFAULT 0,
    last_plan_id INTEGER NOT NULL DEFAULT 0
)
"""
