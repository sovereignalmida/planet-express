"""planet_express/memory/schema.py: DDL, FTS5 availability, FTS triggers."""

import sqlite3

import pytest

from planet_express.memory import schema, store
from tests.memory_support import NOW, OPERATOR, make_card, make_conn


def fts_ids(conn, term):
    return {r[0] for r in conn.execute(
        "SELECT c.id FROM memory_fts f JOIN memory_cards c ON c.seq = f.rowid WHERE memory_fts MATCH ?", (term,))}


def raw_insert(conn, card_id, title, statement, tags):
    conn.execute(
        "INSERT INTO memory_cards (id, title, type, scope_json, tags, statement, steps_json, evidence_json,"
        " source, status, created_at) VALUES (?, ?, 'fact', '[]', ?, ?, '[]', '[]', 'inferred', 'candidate', ?)",
        (card_id, title, tags, statement, NOW.isoformat()))


def test_init_is_idempotent_and_creates_tables():
    conn = make_conn()
    schema.init_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"memory_cards", "memory_fts", "memory_edges", "memory_history"} <= names


def test_ddl_is_a_plain_script_the_migration_can_embed():
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema.DDL)
    conn.executescript(schema.DDL)  # re-runnable


def test_missing_fts5_fails_loudly(monkeypatch):
    class NoFts:
        def execute(self, sql, *args):
            if "fts5" in sql.lower():
                raise sqlite3.OperationalError("no such module: fts5")
            return sqlite3.connect(":memory:").execute(sql, *args)

    with pytest.raises(schema.FTSUnavailable, match="FTS5"):
        schema.require_fts5(NoFts())


def test_fts_follows_insert_update_delete():
    conn = make_conn()
    raw_insert(conn, "c1", "Alpha title", "walrus statement", "stack:media")
    assert fts_ids(conn, "walrus") == {"c1"}
    assert fts_ids(conn, "media") == {"c1"}

    conn.execute("UPDATE memory_cards SET statement = 'penguin statement' WHERE id = 'c1'")
    assert fts_ids(conn, "walrus") == set()
    assert fts_ids(conn, "penguin") == {"c1"}

    conn.execute("UPDATE memory_cards SET title = 'Renamed' WHERE id = 'c1'")
    assert fts_ids(conn, "Renamed") == {"c1"} and fts_ids(conn, "Alpha") == set()

    conn.execute("UPDATE memory_cards SET tags = 'mount:/mnt/data' WHERE id = 'c1'")
    assert fts_ids(conn, "data") == {"c1"} and fts_ids(conn, "media") == set()

    conn.execute("DELETE FROM memory_cards WHERE id = 'c1'")
    assert fts_ids(conn, "penguin") == set() and fts_ids(conn, "Renamed") == set()


def test_status_changes_do_not_disturb_fts():
    conn = make_conn()
    store.upsert_candidate(conn, make_card("c1", statement="walrus"), OPERATOR, now=NOW)
    store.confirm(conn, "c1", OPERATOR, now=NOW)
    assert fts_ids(conn, "walrus") == {"c1"}
    assert conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')") is not None


def test_store_writes_keep_fts_in_sync_including_scope_tags():
    conn = make_conn()
    store.upsert_candidate(conn, make_card("c1", scope=["mount:/mnt/media"], statement="walrus"), OPERATOR, now=NOW)
    assert fts_ids(conn, "media") == {"c1"}
    store.upsert_candidate(conn, make_card("c1", scope=["stack:other"], statement="walrus"), OPERATOR, now=NOW)
    assert fts_ids(conn, "media") == set() and fts_ids(conn, "other") == {"c1"}
