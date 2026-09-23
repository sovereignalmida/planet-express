import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core.store import (
    SCHEMA_VERSION,
    PendingPlanConflict,
    SchemaTooNewError,
    Store,
)

V4_DDL = """
CREATE TABLE incident_reconciliations (
    scan_id TEXT PRIMARY KEY, snapshot_timestamp TEXT NOT NULL, reconciled_at REAL NOT NULL,
    observation_count INTEGER NOT NULL CHECK (observation_count >= 0));
CREATE TABLE incidents (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, fingerprint_version INTEGER NOT NULL,
    kind TEXT NOT NULL, resource TEXT NOT NULL, status TEXT NOT NULL,
    condition TEXT NOT NULL, severity TEXT, summary TEXT NOT NULL, details_json TEXT NOT NULL,
    last_observed_scan_id TEXT NOT NULL REFERENCES incident_reconciliations(scan_id),
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, resolved_at REAL,
    occurrences INTEGER NOT NULL);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY, action TEXT NOT NULL, target_key TEXT NOT NULL, target_json TEXT NOT NULL,
    risk TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','approved','denied','expired')),
    requested_via TEXT NOT NULL, requested_by TEXT, created_at REAL NOT NULL,
    expires_at REAL NOT NULL, decided_by TEXT, decided_at REAL, message_id INTEGER);
CREATE UNIQUE INDEX approvals_one_pending ON approvals(action,target_key) WHERE status='pending';
CREATE INDEX approvals_action_target ON approvals(action,target_key);
CREATE TABLE incident_proposals (
    approval_id TEXT PRIMARY KEY REFERENCES approvals(id),
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    scan_id TEXT NOT NULL REFERENCES incident_reconciliations(scan_id));
CREATE TABLE executions (
    id TEXT PRIMARY KEY, approval_id TEXT NOT NULL REFERENCES approvals(id),
    status TEXT NOT NULL CHECK (status IN ('running','verifying','passed','failed','interrupted')),
    started_at REAL NOT NULL, finished_at REAL, reason TEXT);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
    approval_id TEXT, execution_id TEXT, payload TEXT NOT NULL DEFAULT '{}');
CREATE INDEX events_kind_ts ON events(kind,ts);
PRAGMA user_version=4;
"""


def make_v4(path: Path):
    with sqlite3.connect(path) as conn:
        conn.executescript(V4_DDL)
        conn.execute("INSERT INTO incident_reconciliations VALUES ('scan','time',1,1)")
        conn.execute(
            "INSERT INTO incidents VALUES "
            "('incident','fingerprint',1,'container','media/web','open','failing','HIGH',"
            "'bad','{}','scan',1,1,NULL,1)"
        )
        approvals = [
            ("pending", "docker.restart_service", "media/web", "pending", 41),
            ("running", "docker.restart_service", "media/db", "approved", 42),
            ("passed", "docker.restart_service", "media/ok", "approved", None),
        ]
        for approval_id, action, target_key, status, message_id in approvals:
            target = {"stack": "media", "service": target_key.split("/")[-1],
                      "container": target_key.split("/")[-1]}
            conn.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,? ,?,?,?, ?,?,?,?,?)",
                (approval_id, action, target_key, json.dumps(target), "R1", status, "telegram",
                 "operator", 1, 100, None, None, message_id),
            )
        conn.execute("INSERT INTO incident_proposals VALUES ('pending','incident','scan')")
        conn.execute("INSERT INTO executions VALUES ('exec-running','running','running',2,NULL,NULL)")
        conn.execute("INSERT INTO executions VALUES ('exec-passed','passed','passed',2,3,'ok')")
        conn.execute("INSERT INTO events(ts,kind,payload) VALUES (1,'existing','{}')")


def approval_and_execution(store: Store, key="media/web"):
    approval, _ = store.propose(
        action="docker.restart_service", target_key=key,
        target={"stack": "media", "service": "web", "container": "web"},
        risk="R1", requested_via="test",
    )
    store.consume(approval["id"], decision="approved", decided_by="test",
                  arrived_at=approval["created_at"])
    return approval, store.create_execution(approval["id"])


def test_fresh_database_gets_full_v5_schema(tmp_path):
    store = Store(tmp_path / "data" / "state.db")
    assert store.init() == {"expired_approvals": [], "interrupted_executions": []}
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"execution_steps", "attempts", "rollback_candidates"} <= tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(approvals)")}
        assert {"plan_json", "plan_sha256", "origin"} <= columns
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_v4_migration_preserves_rows_and_returns_cutover(tmp_path):
    path = tmp_path / "state.db"
    make_v4(path)
    cutover = Store(path, clock=lambda: 50).init()
    assert [row["id"] for row in cutover["expired_approvals"]] == ["pending"]
    assert [row["id"] for row in cutover["interrupted_executions"]] == ["exec-running"]
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT status FROM approvals WHERE id='pending'").fetchone()[0] == "expired"
        running = conn.execute("SELECT * FROM executions WHERE id='exec-running'").fetchone()
        assert running["status"] == "interrupted"
        assert running["reason"] == "interrupted by the v5 upgrade"
        assert running["kind"] == "run" and running["parent_execution_id"] is None
        assert conn.execute("SELECT status FROM executions WHERE id='exec-passed'").fetchone()[0] == "passed"
        assert conn.execute("SELECT COUNT(*) FROM incident_proposals").fetchone()[0] == 1
        events = conn.execute("SELECT kind,payload FROM events ORDER BY id").fetchall()
        assert [row["kind"] for row in events] == [
            "existing", "execution.interrupted", "approval.expired",
        ]
        assert "superseded by the v5 upgrade" in events[-1]["payload"]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_forced_migration_failure_rolls_back_byte_for_byte(tmp_path, monkeypatch):
    from planet_express.core import store as module

    path = tmp_path / "state.db"
    make_v4(path)
    before = path.read_bytes()

    def fail(_conn, _script):
        raise RuntimeError("forced migration failure")

    monkeypatch.setattr(module, "_execute_ddl", fail)
    with pytest.raises(RuntimeError, match="forced"):
        Store(path).init()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {row[1] for row in conn.execute("PRAGMA table_info(executions)")} == {
            "id", "approval_id", "status", "started_at", "finished_at", "reason",
        }


def test_v6_refused_and_v5_reinit_is_noop(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=6")
    before = path.read_bytes()
    with pytest.raises(SchemaTooNewError):
        Store(path).init()
    assert path.read_bytes() == before

    path.unlink()
    store = Store(path)
    store.init()
    approval, execution = approval_and_execution(store)
    before_rows = store.list_events()
    assert Store(path).init() == {"expired_approvals": [], "interrupted_executions": []}
    assert store.get_approval(approval["id"])["id"] == approval["id"]
    assert store.get_execution(execution["id"])["id"] == execution["id"]
    assert store.list_events() == before_rows


def test_step_transitions(tmp_path):
    store = Store(tmp_path / "state.db", clock=lambda: 10)
    store.init()
    _approval, execution = approval_and_execution(store)
    steps = [
        {"type": "wait", "params": {"seconds": 1}, "binding": {}},
        {"type": "wait", "params": {"seconds": 2}, "binding": {}},
        {"type": "wait", "params": {"seconds": 3}, "binding": {}},
    ]
    store.create_steps(execution["id"], steps)
    store.set_step_pre_state(execution["id"], 1, {"before": "value"})
    store.mark_step_dispatched(execution["id"], 1)
    store.finish_step(execution["id"], 1, status="passed", effect="not_applied",
                      output={"ok": True}, reason="done")
    store.finish_step(execution["id"], 2, status="skipped", reason="earlier failure")
    store.finish_step(execution["id"], 3, status="aborted", reason="operator")
    rows = store.list_steps(execution["id"])
    assert [row["status"] for row in rows] == ["passed", "skipped", "aborted"]
    assert rows[0]["pre_state"] == {"before": "value"} and rows[0]["output"] == {"ok": True}
    with pytest.raises(ValueError):
        store.mark_step_dispatched(execution["id"], 2)
    with pytest.raises(ValueError):
        store.finish_step(execution["id"], 1, status="aborted")


def test_attempt_reservation_multiplicity_consume_release_and_reconcile(tmp_path):
    store = Store(tmp_path / "state.db", clock=lambda: 100)
    store.init()
    _approval, execution = approval_and_execution(store)
    steps = [{"type": "service.restart", "params": {}, "binding": {}} for _ in range(3)]
    store.create_steps(execution["id"], steps)
    pairs = [("service.restart", "media/web")] * 2
    assert store.reserve_runbook_attempts(
        execution["id"], pairs, window_start=0, cooldown_start=50, max_per_day=3, now=100,
    ) is None
    store.consume_attempt(execution["id"], 1)
    assert store.release_attempts(execution["id"], [2]) == 1

    _approval2, execution2 = approval_and_execution(store, "media/other")
    store.create_steps(execution2["id"], steps)
    refusal = store.reserve_runbook_attempts(
        execution2["id"], pairs, window_start=0, cooldown_start=50, max_per_day=3, now=100,
    )
    assert "cooling down" in refusal.reason and refusal.step_n == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts WHERE execution_id=?",
                            (execution2["id"],)).fetchone()[0] == 0

    assert store.reserve_runbook_attempts(
        execution2["id"], [(1, "service.restart", "media/other"),
                           (2, "service.restart", "media/other"),
                           (3, "service.restart", "media/other")],
        window_start=0, cooldown_start=101, max_per_day=3, now=100,
    ) is None
    store.mark_step_dispatched(execution2["id"], 1)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE execution_steps SET effect='unknown' WHERE execution_id=? AND n=2",
                     (execution2["id"],))
    assert store.reconcile_reserved_attempts() == {"consumed": 2, "released": 1}


def test_attempt_reservation_allows_exact_cooldown_boundary(tmp_path):
    store = Store(tmp_path / "state.db", clock=lambda: 100)
    store.init()
    steps = [{"type": "service.restart", "params": {}, "binding": {}}]
    _approval, first = approval_and_execution(store)
    store.create_steps(first["id"], steps)
    assert store.reserve_runbook_attempts(
        first["id"], [("service.restart", "media/web")],
        window_start=0, cooldown_start=100, max_per_day=3, now=100,
    ) is None

    _approval, second = approval_and_execution(store, "media/other")
    store.create_steps(second["id"], steps)
    assert store.reserve_runbook_attempts(
        second["id"], [("service.restart", "media/web")],
        window_start=0, cooldown_start=100, max_per_day=3, now=100,
    ) is None


def test_rollback_candidates_and_read_errors_fail_closed(tmp_path):
    store = Store(tmp_path / "state.db", clock=lambda: 10)
    store.init()
    _approval, execution = approval_and_execution(store)
    store.open_rollback_candidate(
        execution["id"], 1, stack="media", service="web", image_reference="repo:tag",
        old_image_id="sha256:old", expires_at=20,
    )
    assert store.any_open_rollback_candidate(19)
    assert not store.any_open_rollback_candidate(20)
    assert store.close_rollback_candidate(execution["id"], 1)
    assert not store.any_open_rollback_candidate(11)
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TABLE rollback_candidates")
    with pytest.raises(sqlite3.Error):
        store.any_open_rollback_candidate(11)


def test_runbook_approval_variants_store_server_origin(tmp_path):
    store = Store(tmp_path / "state.db")
    store.init()
    row, created = store.propose_runbook(
        action="runbook", target_key="media/web", target={"title": "x"}, risk="R1",
        requested_via="planner", plan_json='{"kind":"runbook"}', plan_sha256="a" * 64,
        origin="planner",
    )
    assert created and row["origin"] == "planner" and row["plan_sha256"] == "a" * 64
    assert "origin" not in json.loads(row["plan_json"])


def test_legacy_and_runbook_pending_requests_never_deduplicate_or_adopt(tmp_path):
    store = Store(tmp_path / "state.db", clock=lambda: 10)
    store.init()
    target = {"title": "x"}
    runbook, _ = store.propose_runbook(
        action="runbook", target_key="media/web", target=target, risk="R1",
        requested_via="planner", plan_json='{"kind":"runbook"}', plan_sha256="a" * 64,
        origin="planner",
    )
    with pytest.raises(PendingPlanConflict, match="different request"):
        store.propose(
            action="runbook", target_key="media/web", target=target, risk="R1",
            requested_via="telegram",
        )
    direct = store.create_direct_execution(
        action="runbook", target_key="media/web", target=target, risk="R1",
        operator="operator", arrived_at=10,
    )
    assert not direct["adopted"]
    assert direct["approval"]["id"] != runbook["id"]
    assert store.get_approval(runbook["id"])["status"] == "pending"


def test_v5_cutover_updates_telegram_cards_best_effort():
    from casa_farnsworth import _update_v5_cutover_cards
    from notifier import FakeNotifier

    notifier = FakeNotifier()
    _update_v5_cutover_cards({
        "expired_approvals": [{"id": "a", "message_id": 10}],
        "interrupted_executions": [{
            "id": "e", "message_id": 11, "action": "docker.restart_service",
            "target": {"stack": "media", "service": "web", "container": "web"},
        }],
    }, notifier)
    assert notifier.request_updates[0] == (
        10, "⏻ Superseded by the Planet Express upgrade — propose it again.",
    )
    assert notifier.request_updates[1][0] == 11
    assert "outcome is unknown" in notifier.request_updates[1][1]


def test_pre_dispatch_refusal_can_fail_a_pending_step(tmp_path):
    # Own review, T38: binding drift fails a step before it runs; that must be recordable.
    store = Store(tmp_path / "db.sqlite")
    store.init()
    _approval, execution = approval_and_execution(store)
    store.create_steps(execution["id"], [{"type": "wait", "params": {"seconds": 1}, "binding": {}}])
    store.finish_step(execution["id"], 1, status="failed", effect="not_applied",
                      reason="compose file changed since approval")
    assert store.list_steps(execution["id"])[0]["status"] == "failed"
    with pytest.raises(ValueError):
        # a pending step can only fail as not_applied: it never ran
        store2 = Store(tmp_path / "db2.sqlite")
        store2.init()
        _a, e2 = approval_and_execution(store2)
        store2.create_steps(e2["id"], [{"type": "wait", "params": {"seconds": 1}, "binding": {}}])
        store2.finish_step(e2["id"], 1, status="failed", effect="applied")


def test_attempt_pairs_never_guess_step_numbers(tmp_path):
    # Own review, T38: pairs that don't match stored steps are refused, not renumbered.
    store = Store(tmp_path / "db.sqlite")
    store.init()
    _approval, execution = approval_and_execution(store)
    with pytest.raises(ValueError, match="no stored step"):
        store.reserve_runbook_attempts(
            execution["id"], [("service.restart", "media/web")],
            window_start=0, cooldown_start=0, max_per_day=3, now=1,
        )


def test_concurrent_initializer_that_loses_the_race_takes_the_v5_path(tmp_path, monkeypatch):
    # Codex review, T38: a second process that read version 4 before the first one migrated must
    # re-read under the write lock and not re-run the migration.
    path = tmp_path / "state.db"
    make_v4(path)
    loser = Store(path)
    real_migrate = Store._migrate_4_to_5

    fired = []

    def other_process_wins_first(self, conn):
        if not fired:
            fired.append(True)
            Store(path).init()  # the "other process" completes the whole migration now
        return real_migrate(self, conn)

    monkeypatch.setattr(Store, "_migrate_4_to_5", other_process_wins_first)
    cutover = loser.init()
    assert cutover == {"expired_approvals": [], "interrupted_executions": []}
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_pre_v4_loser_never_stamps_a_migrated_database_back_to_v4(tmp_path, monkeypatch):
    # Codex review round 2, T38: the same race one step earlier, at the v4 baseline.
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE legacy_marker (x INTEGER)")  # a non-empty pre-v4 database
    loser = Store(path)
    real_baseline = Store._create_v4_baseline
    fired = []

    def other_process_wins_first(conn):
        if not fired:
            fired.append(True)
            Store(path).init()
        return real_baseline(conn)

    monkeypatch.setattr(Store, "_create_v4_baseline", staticmethod(other_process_wins_first))
    loser.init()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    Store(path).init()  # and every later startup still works


def test_explicit_attempt_tuples_must_match_stored_steps(tmp_path):
    # Codex review round 3, T38.
    store = Store(tmp_path / "db.sqlite")
    store.init()
    _approval, execution = approval_and_execution(store)
    store.create_steps(execution["id"], [{"type": "wait", "params": {"seconds": 1}, "binding": {}}])
    for bad in ([(1, "service.restart", "media/web")], [(2, "wait", "host")]):
        with pytest.raises(ValueError, match="does not match a stored step"):
            store.reserve_runbook_attempts(
                execution["id"], bad, window_start=0, cooldown_start=0, max_per_day=3, now=1,
            )


def test_direct_request_adopts_an_identical_pending_plan_from_another_origin(tmp_path):
    # Codex review round 3, T38: a *-direct request takes over the same pending plan's card
    # instead of leaving it pending beside a second approved row.
    store = Store(tmp_path / "db.sqlite")
    store.init()
    target = {"stack": "media", "service": "web", "container": "web"}
    pending, created = store.propose_runbook(
        action="docker.restart_service", target_key="media/web", target=target, risk="R1",
        requested_via="telegram", plan_json='{"p":1}', plan_sha256="a" * 64, origin="telegram",
    )
    assert created
    result = store.create_direct_runbook_execution(
        action="docker.restart_service", target_key="media/web", target=target, risk="R1",
        operator="chris", arrived_at=1, plan_json='{"p":1}', plan_sha256="a" * 64,
        origin="dashboard-direct",
    )
    assert result["adopted"] and result["approval"]["id"] == pending["id"]
