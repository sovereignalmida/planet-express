import sqlite3

import pytest
from missions_helpers import Clock, make_store, v5_database

from planet_express.core.store import (
    SCHEMA_VERSION,
    SchemaTooNewError,
    SnapshotRequiredError,
    Store,
    snapshot_marker_path,
    write_snapshot_marker,
)

MISSION_TABLES = {"missions", "mission_events", "mission_leases", "resource_fences", "standing_grants",
                  "mission_approvals", "mission_sessions", "mission_artifacts"}


def tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def version(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_schema_version_is_6_and_fresh_db_has_mission_tables(tmp_path):
    store, _ = make_store(tmp_path)
    assert SCHEMA_VERSION == 6 and version(store.path) == 6
    assert MISSION_TABLES <= tables(store.path)


def test_fresh_db_needs_no_snapshot_marker(tmp_path):
    store = Store(tmp_path / "fresh" / "pe.db")
    store.init()
    assert version(store.path) == 6


def test_v5_to_v6_migrates_keeps_data_and_is_idempotent(tmp_path):
    db = tmp_path / "state" / "pe.db"
    v5_database(db)
    assert version(db) == 5 and not (MISSION_TABLES & tables(db))
    clock = Clock()
    write_snapshot_marker(db, now=clock.t)
    store = Store(db, clock=clock)
    store.init()
    assert version(db) == 6 and MISSION_TABLES <= tables(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='preexisting'").fetchone()[0] == 1
    store.init()
    store.init()
    assert version(db) == 6


def test_upgrade_refused_without_marker_and_db_untouched(tmp_path):
    db = tmp_path / "state" / "pe.db"
    v5_database(db)
    with pytest.raises(SnapshotRequiredError):
        Store(db).init()
    assert version(db) == 5 and not (MISSION_TABLES & tables(db))


def test_upgrade_refused_with_stale_or_future_or_garbage_marker(tmp_path):
    db = tmp_path / "state" / "pe.db"
    v5_database(db)
    clock = Clock()
    store = Store(db, clock=clock)
    write_snapshot_marker(db, now=clock.t - 601)
    with pytest.raises(SnapshotRequiredError):
        store.init()
    write_snapshot_marker(db, now=clock.t + 3600)
    with pytest.raises(SnapshotRequiredError):
        store.init()
    snapshot_marker_path(db).write_text("not a number")
    with pytest.raises(SnapshotRequiredError):
        store.init()
    write_snapshot_marker(db, now=clock.t - 599)
    store.init()
    assert version(db) == 6


def test_newer_schema_is_still_refused(tmp_path):
    store, _ = make_store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA user_version = 7")
    with pytest.raises(SchemaTooNewError):
        store.init()


def test_one_live_session_index_exists(tmp_path):
    store, _ = make_store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='one_live_session'").fetchone()[0]
    assert "WHERE revoked_at IS NULL" in sql and "UNIQUE" in sql


def test_missions_disabled_by_default():
    from config_schema import MissionsConfig, PlanetExpressConfig
    assert MissionsConfig().enabled is False
    assert PlanetExpressConfig(stacks_root="/srv/stacks").missions.enabled is False
