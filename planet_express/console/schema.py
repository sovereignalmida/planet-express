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
    turn INTEGER,
    text TEXT NOT NULL,
    consumed_turn INTEGER
);
CREATE INDEX IF NOT EXISTS console_messages_conversation ON console_messages(conversation_id, seq)
"""
