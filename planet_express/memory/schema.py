"""Ship's Memory DDL. Self-contained: slice 2 ships its own schema and the orchestrator merges `DDL`
into the v6 migration. Statements are idempotent (IF NOT EXISTS) and the whole string is safe to hand
to `executescript`.

memory_cards.tags is the space-joined scope, written by the store, because an external-content FTS
table can only index real columns of its content table.
"""

import sqlite3

SCHEMA_VERSION = 1


class FTSUnavailable(RuntimeError):
    """This Python's sqlite3 was built without FTS5. Retrieval cannot work; refuse to start."""


DDL = """
CREATE TABLE IF NOT EXISTS memory_cards (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('fact','procedure','constraint','decision')),
    scope_json       TEXT NOT NULL DEFAULT '[]',
    tags             TEXT NOT NULL DEFAULT '',
    statement        TEXT NOT NULL,
    steps_json       TEXT NOT NULL DEFAULT '[]',
    evidence_json    TEXT NOT NULL DEFAULT '[]',
    source           TEXT NOT NULL CHECK (source IN ('observed','operator-stated','researched','inferred')),
    status           TEXT NOT NULL CHECK (status IN ('candidate','confirmed','deprecated')),
    created_at       TEXT NOT NULL,
    confirmed_at     TEXT,
    last_verified_at TEXT,
    review_after     TEXT,
    content_hash     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS memory_cards_status ON memory_cards (status);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title, statement, tags,
    content='memory_cards', content_rowid='seq'
);

CREATE TRIGGER IF NOT EXISTS memory_cards_ai AFTER INSERT ON memory_cards BEGIN
    INSERT INTO memory_fts (rowid, title, statement, tags) VALUES (new.seq, new.title, new.statement, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memory_cards_ad AFTER DELETE ON memory_cards BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, title, statement, tags)
        VALUES ('delete', old.seq, old.title, old.statement, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memory_cards_au AFTER UPDATE OF title, statement, tags ON memory_cards BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, title, statement, tags)
        VALUES ('delete', old.seq, old.title, old.statement, old.tags);
    INSERT INTO memory_fts (rowid, title, statement, tags) VALUES (new.seq, new.title, new.statement, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_edges (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('supersedes','conflicts_with')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS memory_edges_dst ON memory_edges (dst, kind);

CREATE TABLE IF NOT EXISTS memory_history (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     TEXT NOT NULL,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL CHECK (actor GLOB '[a-z]*:?*'),
    action      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS memory_history_card ON memory_history (card_id, seq);
CREATE TRIGGER IF NOT EXISTS memory_history_no_update BEFORE UPDATE ON memory_history BEGIN
    SELECT RAISE(ABORT, 'memory_history is append-only');
END;
CREATE TRIGGER IF NOT EXISTS memory_history_no_delete BEFORE DELETE ON memory_history BEGIN
    SELECT RAISE(ABORT, 'memory_history is append-only');
END;
"""


def require_fts5(conn) -> None:
    """Fail loudly, with the fix, when FTS5 is missing. Never degrade to unindexed search."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._memory_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._memory_fts5_probe")
    except sqlite3.OperationalError as exc:
        raise FTSUnavailable(
            f"Ship's Memory needs SQLite FTS5 and this Python's sqlite3 does not have it ({exc}). "
            "Use a Python build whose sqlite3 includes FTS5."
        ) from exc


def init_schema(conn: sqlite3.Connection) -> None:
    require_fts5(conn)
    conn.executescript(DDL)
