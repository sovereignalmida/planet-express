"""Round-2 review findings for slice 1, each written to fail first."""
import sqlite3
import sys
from pathlib import Path

import pytest
from missions_helpers import (
    advance_to,
    make_store,
    new_mission,
    token,
    v5_database,
)

from planet_express.core.store import Store, snapshot_marker_path
from planet_express.missions import leases
from planet_express.missions.approvals import record_approval
from planet_express.missions.models import Grant
from planet_express.missions.state import transition
from planet_express.missions.store import MissionStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import state_snapshot as snapshot

KW = {"kind": "local_cgroup", "session_ttl_s": 600, "lease_ttl_s": 60}
STACK = "host:local/stack:media"


# ── 1. snapshot create writes the marker ────────────────────────────────────
@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("x: 1\n")
    monkeypatch.setenv("CASA_DATA_DIR", str(data))
    monkeypatch.setenv("CASA_CONFIG", str(config))

    def no_git(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(snapshot.subprocess, "run", no_git)
    return data


def test_snapshot_create_writes_marker_and_v5_host_can_then_upgrade(data_dir):
    db = data_dir / "planetexpress.db"
    v5_database(db)
    with pytest.raises(Exception, match="snapshot marker"):
        Store(db).init()
    snapshot.create("pre-deploy")
    assert snapshot_marker_path(db).exists()
    Store(db).init()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_snapshot_marker_format_matches_the_core_reader(data_dir):
    from planet_express.core.store import write_snapshot_marker
    db = data_dir / "planetexpress.db"
    v5_database(db)
    snapshot.create()
    script_marker = snapshot_marker_path(db).read_text()
    write_snapshot_marker(db, now=123)
    assert snapshot_marker_path(db).read_text() == "123"
    assert script_marker.strip().isdigit()


def test_snapshot_without_a_database_writes_no_marker(data_dir):
    snapshot.create()
    assert not (data_dir / "pe-snapshot.marker").exists()


def test_failed_snapshot_writes_no_marker(data_dir, monkeypatch):
    v5_database(data_dir / "planetexpress.db")

    def boom(_path):
        raise OSError("disk full")

    monkeypatch.setattr(snapshot, "sha256", boom)
    with pytest.raises(OSError):
        snapshot.create()
    assert not (data_dir / "pe-snapshot.marker").exists()


# ── 2. set_plan / set_grant validate before writing ────────────────────────
def test_oversize_plan_is_refused_and_nothing_written(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    with pytest.raises(ValueError):
        ms.set_plan(m.id, {"v": 1, "blob": "x" * 300_000})
    got = ms.get_mission(m.id)
    assert got.plan_json is None and got.plan_hash is None
    assert [x.id for x in ms.list_missions()] == [m.id]


def test_plan_with_floats_is_refused(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    with pytest.raises(ValueError):
        ms.set_plan(m.id, {"v": 1.5})
    assert ms.get_mission(m.id).plan_json is None


def test_oversize_grant_is_refused_and_nothing_written(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    fat = Grant.model_validate({
        "tier": "repair",
        "scope": {k: ["y" * 500] * 256 for k in ("stacks", "services", "paths", "mounts")},
        "exclusions": ["z" * 500] * 256, "credentials": ["w" * 500] * 256,
    })
    with pytest.raises(ValueError):
        ms.set_grant(m.id, fat)
    assert ms.get_mission(m.id).grant_hash == m.grant_hash
    assert len(ms.list_missions()) == 1


# ── 3. events are validated before insert ──────────────────────────────────
@pytest.mark.parametrize(("kind", "actor"), [("bogus", "core"), ("note", ""), ("note", "a" * 129), ("", "core")])
def test_bad_event_is_refused_and_readback_still_works(tmp_path, kind, actor):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    before = len(ms.events(m.id))
    with pytest.raises(ValueError):
        ms.append_event(m.id, kind, actor, {"a": 1})
    assert len(ms.events(m.id)) == before
    ms.append_event(m.id, "note", "core", {"a": 1})
    assert len(ms.events(m.id)) == before + 1


# ── 4. terminal states release leases and revoke the session ───────────────
def started(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms)
    advance_to(ms, m.id, "queued", "investigating", "awaiting_decision")
    record_approval(store, m.id, "grant", m.grant_hash, "approve", "operator:me")
    sid = ms.start_session(m.id, token_hash=token(), **KW)
    return store, ms, m, sid


def lease_count(store):
    with sqlite3.connect(store.path) as conn:
        return conn.execute("SELECT COUNT(*) FROM mission_leases").fetchone()[0]


@pytest.mark.parametrize("path", [("cancelled",), ("verifying", "failed"), ("failed",),
                                  ("paused_tripwire", "rolled_back")])
def test_terminal_transition_frees_leases_and_session(tmp_path, path):
    store, ms, m, sid = started(tmp_path)
    other = new_mission(ms)
    f1 = ms.live_session(m.id)
    old_fence = __import__("json").loads(f1["fence_json"])[STACK]
    with pytest.raises(leases.LeaseConflict):
        leases.acquire(store, [STACK], other.id, 60)
    for state in path:
        transition(store, m.id, state, "core")
    assert lease_count(store) == 0
    assert ms.live_session(m.id) is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT revoked_at FROM mission_sessions WHERE id=?", (sid,)).fetchone()[0] is not None
        assert conn.execute("SELECT fence FROM resource_fences WHERE resource=?", (STACK,)).fetchone()[0] == old_fence
    assert leases.acquire(store, [STACK], other.id, 60)[STACK] > old_fence


def test_terminal_transition_releases_suspect_leases_too(tmp_path):
    store, ms, m, _ = started(tmp_path)
    leases.mark_suspect(store, m.id)
    with pytest.raises(leases.LeaseSuspect):
        leases.release(store, m.id)
    transition(store, m.id, "cancelled", "operator:me")
    assert lease_count(store) == 0


def test_non_terminal_transition_keeps_leases(tmp_path):
    store, ms, m, _ = started(tmp_path)
    transition(store, m.id, "paused_tripwire", "core")
    assert lease_count(store) == 1 and ms.live_session(m.id) is not None


def test_terminal_release_is_atomic_with_the_transition(tmp_path, monkeypatch):
    store, ms, m, _ = started(tmp_path)
    from planet_express.missions import state

    def boom(*_a, **_k):
        raise RuntimeError("release failed")

    monkeypatch.setattr(state, "release_for_terminal", boom)
    with pytest.raises(RuntimeError):
        transition(store, m.id, "cancelled", "core")
    assert ms.get_mission(m.id).status == "executing" and lease_count(store) == 1


def test_terminal_without_session_or_leases_is_fine(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, Grant.model_validate({"tier": "observe"}))
    transition(store, m.id, "cancelled", "operator:me")
    assert ms.get_mission(m.id).status == "cancelled"


# ── 5. FTS5 is optional ─────────────────────────────────────────────────────
MEMORY = {"memory_cards", "memory_fts", "memory_edges", "memory_history"}


def table_names(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fts5_available_probe():
    from planet_express.memory.schema import fts5_available
    assert fts5_available() is True


def test_fresh_init_without_fts5_skips_memory_only(tmp_path, monkeypatch):
    monkeypatch.setattr("planet_express.memory.schema.fts5_available", lambda: False)
    store, _ = make_store(tmp_path)
    names = table_names(store.path)
    assert not (MEMORY & names) and "missions" in names and "mission_leases" in names
    store.init()  # repair path is idempotent too


def test_v5_upgrade_without_fts5_skips_memory_only(tmp_path, monkeypatch):
    from planet_express.core.store import write_snapshot_marker
    monkeypatch.setattr("planet_express.memory.schema.fts5_available", lambda: False)
    db = tmp_path / "state" / "pe.db"
    v5_database(db)
    write_snapshot_marker(db)
    Store(db).init()
    names = table_names(db)
    assert not (MEMORY & names) and "missions" in names


def test_with_fts5_memory_tables_exist(tmp_path):
    store, _ = make_store(tmp_path)
    assert MEMORY <= table_names(store.path)
