"""
planet_express/core/store.py (landing 1b): dedup, one-use approvals, lazy expiry,
arrival-time expiry, racing consumes, execution lifecycle, startup interruption.
"""

import json
import os
import sqlite3
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.core.store import DEFAULT_TTL_SECONDS, Store

TARGET = {"stack": "healthy", "service": "web", "container": "fixture-healthy"}


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _store(tmp_path, clock=None) -> Store:
    s = Store(tmp_path / "data" / "planetexpress.db", clock=clock or Clock())
    s.init()
    return s


def _propose(s: Store, key: str = "healthy/web", **overrides):
    kwargs = {
        "action": "docker.restart_service",
        "target_key": key,
        "target": TARGET,
        "risk": "R1",
        "requested_via": "telegram",
        "requested_by": "@chris (1001)",
    }
    kwargs.update(overrides)
    return s.propose(**kwargs)


def test_init_creates_a_core_only_wal_database(tmp_path):
    s = _store(tmp_path)
    assert stat.S_IMODE(s.path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(s.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_propose_returns_the_live_proposal_instead_of_a_second_one(tmp_path):
    s = _store(tmp_path)
    first, created = _propose(s)
    again, created_again = _propose(s)
    assert created is True and created_again is False
    assert again["id"] == first["id"]
    assert len(first["id"]) <= 16
    assert first["status"] == "pending"
    assert first["expires_at"] == first["created_at"] + DEFAULT_TTL_SECONDS


def test_different_targets_get_different_proposals(tmp_path):
    s = _store(tmp_path)
    a, _ = _propose(s, key="healthy/web")
    b, created = _propose(s, key="slow-start/app")
    assert created and a["id"] != b["id"]


def test_partial_unique_index_blocks_a_second_pending_row(tmp_path):
    s = _store(tmp_path)
    _propose(s)
    with sqlite3.connect(s.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO approvals (id, action, target_key, target_json, risk, status, requested_via, "
            "created_at, expires_at) VALUES ('dup', 'docker.restart_service', 'healthy/web', '{}', 'R1', "
            "'pending', 'dashboard', 1, 2)"
        )


def test_expired_proposal_is_marked_lazily_and_replaced(tmp_path):
    clock = Clock()
    s = _store(tmp_path, clock)
    old, _ = _propose(s)
    clock.t += DEFAULT_TTL_SECONDS

    assert s.get_approval(old["id"])["status"] == "expired"
    new, created = _propose(s)
    assert created and new["id"] != old["id"]
    assert any(e["kind"] == "approval.expired" for e in s.list_events(old["id"]))


def test_consume_is_one_use(tmp_path):
    s = _store(tmp_path)
    row, _ = _propose(s)
    assert s.consume(row["id"], decision="approved", decided_by="@chris (1001)", arrived_at=row["created_at"])
    assert not s.consume(row["id"], decision="approved", decided_by="@sam (2)", arrived_at=row["created_at"])
    assert not s.consume(row["id"], decision="denied", decided_by="@sam (2)", arrived_at=row["created_at"])
    decided = s.get_approval(row["id"])
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "@chris (1001)"


def test_consume_refuses_a_request_that_arrived_at_or_after_expiry(tmp_path):
    s = _store(tmp_path)
    row, _ = _propose(s)
    assert not s.consume(row["id"], decision="approved", decided_by="x", arrived_at=row["expires_at"])


def test_a_decision_that_arrived_before_expiry_is_honoured_even_if_recorded_after(tmp_path):
    clock = Clock()
    s = _store(tmp_path, clock)
    row, _ = _propose(s)
    arrived = row["expires_at"] - 1
    clock.t = row["expires_at"] + 10  # recording finishes after the TTL

    assert s.get_approval(row["id"], as_of=arrived)["status"] == "pending"
    assert s.consume(row["id"], decision="approved", decided_by="@chris (1001)", arrived_at=arrived)


def test_unknown_decision_value_is_rejected(tmp_path):
    s = _store(tmp_path)
    row, _ = _propose(s)
    with pytest.raises(ValueError):
        s.consume(row["id"], decision="maybe", decided_by="x", arrived_at=row["created_at"])


def test_racing_consumes_exactly_one_wins(tmp_path):
    s = _store(tmp_path)
    row, _ = _propose(s)
    n = 16
    barrier = threading.Barrier(n)
    wins = []
    guard = threading.Lock()

    def worker(i):
        barrier.wait()
        if s.consume(row["id"], decision="approved", decided_by=f"op{i}", arrived_at=row["created_at"]):
            with guard:
                wins.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


def test_message_id_is_stored(tmp_path):
    s = _store(tmp_path)
    row, _ = _propose(s)
    s.set_message_id(row["id"], 4242)
    assert s.get_approval(row["id"])["message_id"] == 4242


def test_execution_lifecycle_records_events(tmp_path):
    clock = Clock()
    s = _store(tmp_path, clock)
    row, _ = _propose(s)
    s.consume(row["id"], decision="approved", decided_by="x", arrived_at=row["created_at"])
    execution = s.create_execution(row["id"])
    assert execution["status"] == "running" and execution["finished_at"] is None

    s.set_execution_status(execution["id"], "verifying")
    clock.t += 30
    s.set_execution_status(execution["id"], "passed", reason="healthy for 15s")

    done = s.get_execution(execution["id"])
    assert done["status"] == "passed"
    assert done["reason"] == "healthy for 15s"
    assert done["finished_at"] == clock.t
    kinds = [e["kind"] for e in s.list_events(row["id"])]
    assert kinds == ["proposal.created", "approval.approved", "execution.running",
                     "execution.verifying", "execution.passed"]


def test_approval_and_execution_creation_are_atomic_on_insert_failure(tmp_path, monkeypatch):
    s = _store(tmp_path)
    first, _ = _propose(s, key="healthy/web")
    s.consume(
        first["id"], decision="approved", decided_by="x", arrived_at=first["created_at"]
    )
    existing = s.create_execution(first["id"])
    second, _ = _propose(s, key="slow-start/app")
    monkeypatch.setattr("planet_express.core.store._new_id", lambda: existing["id"])

    with pytest.raises(sqlite3.IntegrityError):
        s.approve_and_create_execution(
            second["id"], decided_by="y", arrived_at=second["created_at"]
        )

    assert s.get_approval(second["id"])["status"] == "pending"


def test_unknown_execution_status_is_rejected(tmp_path):
    s = _store(tmp_path)
    row, _ = _propose(s)
    execution = s.create_execution(row["id"])
    with pytest.raises(ValueError):
        s.set_execution_status(execution["id"], "halfway")


def test_interrupt_unfinished_marks_only_running_and_verifying(tmp_path):
    s = _store(tmp_path)
    a, _ = _propose(s, key="healthy/web")
    b, _ = _propose(s, key="slow-start/app")
    c, _ = _propose(s, key="unhealthy/web")
    s.set_message_id(a["id"], 11)
    for row in (a, b, c):
        s.consume(row["id"], decision="approved", decided_by="x", arrived_at=row["created_at"])
    running = s.create_execution(a["id"])
    verifying = s.create_execution(b["id"])
    s.set_execution_status(verifying["id"], "verifying")
    passed = s.create_execution(c["id"])
    s.set_execution_status(passed["id"], "passed")

    interrupted = s.interrupt_unfinished("core restarted")

    assert {r["id"] for r in interrupted} == {running["id"], verifying["id"]}
    first = next(r for r in interrupted if r["id"] == running["id"])
    assert first["message_id"] == 11
    assert first["target"] == TARGET
    assert s.get_execution(running["id"])["status"] == "interrupted"
    assert s.get_execution(verifying["id"])["reason"] == "core restarted"
    assert s.get_execution(passed["id"])["status"] == "passed"
    assert s.interrupt_unfinished("core restarted") == []


@pytest.mark.parametrize('same_operator', [True, False])
def test_auth_lockout_independent(tmp_path, same_operator):
    clock = Clock()
    store = _store(tmp_path, clock)
    for i in range(3):
        clock.t += 10
        result = store.record_auth_failure('alice' if same_operator else f'op{i}',
                                           f'ip{i}' if same_operator else 'ip')
        assert result['just_locked'] == (i == 2)
        assert result['remaining_attempts'] == 2 - i
    operator, ip = ('alice', 'new') if same_operator else ('new', 'ip')
    assert store.auth_status(operator, ip)['locked_until'] == clock.t + 900
    assert not store.record_auth_failure(operator, ip)['just_locked']
    clock.t += 900
    assert not store.auth_status(operator, ip)['locked']
    assert [e['kind'] for e in store.list_events()].count('auth.locked') == 1


def test_auth_success_clears_both_and_prunes(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    for op, ip in [('alice', 'other'), ('bob', 'ip'), ('keep', 'keep')]:
        store.record_auth_failure(op, ip)
    store.record_auth_success('alice', 'ip')
    assert store.auth_status('alice', 'ip')['remaining_attempts'] == 3
    # bob's own failure still counts: a success resets only alice and ip (Codex re-review, T13a).
    assert store.auth_status('bob', 'other')['remaining_attempts'] == 2
    assert store.auth_status('keep', 'keep')['remaining_attempts'] == 2
    clock.t += 1801
    store.record_auth_failure('new', 'new')
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM auth_failures').fetchone()[0] == 1
    assert any(e['kind'] == 'auth.success' for e in store.list_events())


def test_auth_replay_devices_and_notifications(tmp_path):
    store = _store(tmp_path)
    assert store.consume_totp_step('alice', 10)
    assert not store.consume_totp_step('alice', 10)
    assert not store.consume_totp_step('alice', 9)
    assert store.consume_totp_step('alice', 11)
    assert store.device_epoch('alice') == 0
    assert store.revoke_devices('alice') == 1
    assert store.revoke_devices('alice') == store.device_epoch('alice') == 2
    assert store.device_epoch('bob') == 0
    assert not store.auth_lock_notified('alice', 'ip', 1000.0)
    assert store.auth_lock_notified('alice', 'ip', 1000.0)
    assert not store.auth_lock_notified('alice', 'ip', 1001.0)
    assert not store.auth_lock_notified('alice', 'other', 1000.0)


def test_schema_v1_upgrade(tmp_path):
    from planet_express.core.store import _SCHEMA

    path = tmp_path / 'data' / 'old.db'
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA[_SCHEMA.index('CREATE TABLE IF NOT EXISTS approvals'):])
        conn.execute('PRAGMA user_version = 1')
    store = Store(path)
    store.init()
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {'auth_failures', 'auth_totp_steps', 'auth_device_epochs'} <= tables
    assert store.consume_totp_step('alice', 1)


def test_auth_later_locked_deadline(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    for _ in range(3):
        store.record_auth_failure('alice', 'other')
    clock.t += 10
    for _ in range(3):
        store.record_auth_failure('bob', 'ip')
    assert store.auth_status('alice', 'ip')['locked_until'] == clock.t + 900


def test_auth_atomic_consumes_and_notification_claims(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = _store(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda _: store.consume_totp_step('alice', 10), range(8)))
        notified = list(pool.map(lambda _: store.auth_lock_notified('alice', 'ip', 1000), range(8)))
        failures = list(pool.map(lambda _: store.record_auth_failure('alice', 'ip'), range(8)))
    assert sum(accepted) == 1
    assert sum(not notified_before for notified_before in notified) == 1
    assert sum(result['just_locked'] for result in failures) == 1


# ── Codex review (T13a): a lock holds until its own deadline ────────────────────
def _auth_store(tmp_path):
    now = [0.0]
    store = Store(tmp_path / "data" / "auth.db", clock=lambda: now[0])
    store.init()
    return store, now


def test_lock_holds_until_deadline_when_failures_are_spread(tmp_path):
    store, now = _auth_store(tmp_path)
    for t in (0.0, 400.0, 800.0):
        now[0] = t
        result = store.record_auth_failure("alice", "10.0.0.1")
    assert result["locked"] and result["just_locked"] and result["locked_until"] == 1700.0
    for t in (900.0, 1200.0, 1699.0):
        now[0] = t
        assert store.auth_status("alice", "10.0.0.1")["locked"] is True, t
    now[0] = 1700.5
    status = store.auth_status("alice", "10.0.0.1")
    assert status == {"locked": False, "locked_until": None, "remaining_attempts": 3}


def test_failure_while_locked_does_not_extend_the_lock(tmp_path):
    store, now = _auth_store(tmp_path)
    for t in (0.0, 1.0, 2.0):
        now[0] = t
        store.record_auth_failure("alice", "10.0.0.1")
    now[0] = 500.0
    again = store.record_auth_failure("alice", "10.0.0.1")
    assert again["locked"] and not again["just_locked"] and again["locked_until"] == 902.0


def test_ip_lock_is_independent_of_operator(tmp_path):
    store, now = _auth_store(tmp_path)
    for i, op in enumerate(("alice", "bob", "?")):
        now[0] = float(i)
        store.record_auth_failure(op, "10.0.0.9")
    assert store.auth_status("carol", "10.0.0.9")["locked"] is True
    assert store.auth_status("carol", "10.0.0.10")["locked"] is False


def test_success_clears_locks_for_both_keys(tmp_path):
    store, now = _auth_store(tmp_path)
    for t in (0.0, 1.0, 2.0):
        now[0] = t
        store.record_auth_failure("alice", "10.0.0.1")
    now[0] = 3.0
    store.record_auth_success("alice", "10.0.0.1")
    assert store.auth_status("alice", "10.0.0.1") == {"locked": False, "locked_until": None,
                                                     "remaining_attempts": 3}


# ── Codex re-review (T13a): locking one key never resets another key's counter ──
def test_ip_lock_does_not_reset_operator_counter(tmp_path):
    store, now = _auth_store(tmp_path)
    now[0] = 0.0
    store.record_auth_failure("alice", "ip1")
    now[0] = 1.0
    store.record_auth_failure("alice", "ip1")
    now[0] = 2.0
    locked_ip = store.record_auth_failure("bob", "ip1")      # 3rd failure from ip1
    assert locked_ip["locked"] and locked_ip["just_locked"]
    assert store.auth_status("carol", "ip1")["locked"] is True
    now[0] = 3.0
    alice = store.record_auth_failure("alice", "ip2")         # alice's 3rd within 15 min
    assert alice["locked"] and alice["just_locked"]
    assert store.auth_status("alice", "ip9")["locked"] is True


def test_rotating_ips_cannot_bypass_operator_limit(tmp_path):
    store, now = _auth_store(tmp_path)
    for i in range(3):
        now[0] = float(i)
        result = store.record_auth_failure("alice", f"10.0.0.{i}")
    assert result["locked"] and result["just_locked"]
    assert store.auth_status("alice", "10.0.0.99")["locked"] is True


def test_after_expiry_a_key_gets_three_fresh_attempts(tmp_path):
    store, now = _auth_store(tmp_path)
    for t in (0.0, 1.0, 2.0):
        now[0] = t
        store.record_auth_failure("alice", "10.0.0.1")
    now[0] = 903.0                                             # lock (until 902) expired
    for _ in range(2):
        result = store.record_auth_failure("alice", "10.0.0.2")
        assert not result["locked"], result
    result = store.record_auth_failure("alice", "10.0.0.3")
    assert result["locked"] and result["just_locked"] and result["locked_until"] == 1803.0


def test_success_resets_only_its_own_keys(tmp_path):
    store, now = _auth_store(tmp_path)
    now[0] = 0.0
    store.record_auth_failure("bob", "ip1")
    now[0] = 1.0
    store.record_auth_failure("bob", "ip1")
    now[0] = 2.0
    store.record_auth_success("alice", "ip1")                 # clears ip1 and alice, not bob
    assert store.auth_status("?", "ip1")["remaining_attempts"] == 3
    assert store.auth_status("bob", "ip2")["remaining_attempts"] == 1
    now[0] = 3.0
    assert store.record_auth_failure("bob", "ip3")["just_locked"] is True


def test_failures_during_a_lock_do_not_spend_the_fresh_budget(tmp_path):
    store, now = _auth_store(tmp_path)
    for t in (0.0, 0.0, 0.0):
        now[0] = t
        store.record_auth_failure("alice", "ip1")
    now[0] = 899.0
    for _ in range(3):
        assert not store.record_auth_failure("alice", "ip2")["just_locked"]
    now[0] = 901.0
    assert store.auth_status("alice", "ip3") == {"locked": False, "locked_until": None,
                                               "remaining_attempts": 3}
    assert not store.record_auth_failure("alice", "ip3")["locked"]
    # ...but ip2's own count still holds the three failures alice made from it while locked.
    assert store.auth_status("?", "ip2")["locked"] is True


# ── Codex review (T13b): an unmatched passphrase never locks a global "?" key ──
def test_unknown_operator_failures_lock_only_the_ip(tmp_path):
    store, now = _auth_store(tmp_path)
    for i in range(3):
        now[0] = float(i)
        result = store.record_auth_failure("?", "10.0.0.66")
    assert result["locked"] and result["just_locked"]
    assert store.auth_status("?", "10.0.0.66")["locked"] is True
    assert store.auth_status("?", "10.0.0.7") == {"locked": False, "locked_until": None,
                                                "remaining_attempts": 3}
    assert store.auth_status("alice", "10.0.0.7")["remaining_attempts"] == 3
    for i in range(3):
        store.record_auth_failure("?", f"10.0.1.{i}")
    assert store.auth_status("?", "10.0.2.1")["locked"] is False


def _direct(store, arrived_at=1_000_000):
    return store.create_direct_execution(action='docker.restart_service', target_key='healthy/web',
                                         target=TARGET, risk='R1', operator='chris', arrived_at=arrived_at)


def test_direct_insert_and_new_pending_can_coexist(tmp_path):
    store = _store(tmp_path)
    result = _direct(store)
    row, execution = result['approval'], result['execution']
    assert result['adopted'] is False
    assert row['status'] == 'approved' and row['message_id'] is None
    assert row['requested_via'] == 'dashboard-direct'
    assert row['requested_by'] == row['decided_by'] == 'chris'
    assert row['created_at'] == row['decided_at'] == row['expires_at'] == 1_000_000
    assert execution['approval_id'] == row['id'] and execution['status'] == 'running'
    events = store.list_events()
    assert [event['kind'] for event in events] == ['proposal.created', 'approval.approved', 'execution.running']
    assert events[0]['payload']['requested_via'] == 'dashboard-direct'
    pending, created = _propose(store)
    assert created and pending['id'] != row['id']
    assert store.get_approval(row['id'])['status'] == 'approved'


def test_direct_adopts_pending_with_card(tmp_path):
    store = _store(tmp_path)
    pending, _ = _propose(store)
    store.set_message_id(pending['id'], 42)
    result = _direct(store)
    assert result['adopted'] is True
    row = result['approval']
    assert row['id'] == pending['id'] and row['message_id'] == 42
    assert row['status'] == 'approved' and row['decided_by'] == 'chris'
    assert row['requested_via'] == 'telegram'
    assert [e['kind'] for e in store.list_events()] == [
        'proposal.created', 'approval.approved', 'execution.running',
    ]


@pytest.mark.parametrize('adopt', [False, True])
def test_direct_execution_insert_failure_rolls_back(tmp_path, adopt):
    store = _store(tmp_path)
    pending = _propose(store)[0] if adopt else None
    before = store.list_events()
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER fail_execution BEFORE INSERT ON executions "
                     "BEGIN SELECT RAISE(ABORT, 'insert failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='insert failed'):
        _direct(store)
    assert store.list_events() == before
    with store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM executions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == int(adopt)
    if adopt:
        assert store.get_approval(pending['id'])['status'] == 'pending'


def test_direct_expires_stale_pending(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    pending, _ = _propose(store, ttl_seconds=1)
    clock.t += 1
    result = _direct(store, arrived_at=clock.t)
    assert not result['adopted'] and result['approval']['id'] != pending['id']
    assert store.get_approval(pending['id'])['status'] == 'expired'


# ── Codex review (T14): adopt a pending card only for the target just confirmed ──
def test_direct_execution_does_not_adopt_a_card_for_a_stale_container(tmp_path):
    store, now = _auth_store(tmp_path)
    old = {"stack": "media", "service": "sonarr", "container": "sonarr-old"}
    new = dict(old, container="sonarr-new")
    card, _ = store.propose(action="docker.restart_service", target_key="media/sonarr", target=old, risk="R1",
                            requested_via="telegram", requested_by="@chris")
    result = store.create_direct_execution(action="docker.restart_service", target_key="media/sonarr",
                                           target=new, risk="R1", operator="chris", arrived_at=now[0])
    assert result["adopted"] is False
    assert json.loads(result["approval"]["target_json"]) == new
    assert result["approval"]["requested_via"] == "dashboard-direct"
    assert store.get_approval(card["id"])["status"] == "pending"
    same = store.create_direct_execution(action="docker.restart_service", target_key="media/sonarr",
                                         target=old, risk="R1", operator="chris", arrived_at=now[0])
    assert same["adopted"] is True and same["approval"]["id"] == card["id"]


def test_direct_execution_past_its_deadline_writes_nothing(tmp_path):
    store, now = _auth_store(tmp_path)
    now[0] = 100.0
    target = {"stack": "media", "service": "sonarr", "container": "sonarr"}
    assert store.create_direct_execution(action="docker.restart_service", target_key="media/sonarr", target=target,
                                         risk="R1", operator="chris", arrived_at=95.0, deadline=99.0) is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    assert store.list_events() == []


@pytest.mark.parametrize("existing", [False, True])
def test_events_index_init_and_query_plan(tmp_path, existing):
    from planet_express.core.store import _SCHEMA, SCHEMA_VERSION

    store = Store(tmp_path / "events.db")
    if existing:
        old_schema = _SCHEMA.replace(
            "CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts);", ""
        )
        with sqlite3.connect(store.path) as conn:
            conn.executescript(old_schema)
            conn.execute("PRAGMA user_version = 2")
            conn.execute("INSERT INTO events (ts, kind) VALUES (1, 'old')")
            assert not conn.execute("PRAGMA index_list(events)").fetchall()
    store.init()
    before = store.list_events()
    assert len(before) == int(existing)
    store.init()
    assert store.list_events() == before
    with sqlite3.connect(store.path) as conn:
        assert SCHEMA_VERSION == conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "events_kind_ts" in {row[1] for row in conn.execute("PRAGMA index_list(events)")}
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM events WHERE kind = ? AND ts >= ?",
            ("old", 0),
        ).fetchall()
        assert any("events_kind_ts" in row[3] for row in plan)


@pytest.mark.parametrize("max_age", [None, 100.0])
def test_prune_events_preserves_audit_trails_and_cutoff(tmp_path, max_age):
    from planet_express.core.store import EVENT_RETENTION_SECONDS

    assert EVENT_RETENTION_SECONDS == 90 * 24 * 3600
    clock = Clock(0)
    store = _store(tmp_path, clock)
    store.record_event("old")
    store.record_event("approval", approval_id="approval-id")
    store.record_event("execution", execution_id="execution-id")
    clock.t = 1
    store.record_event("cutoff")
    clock.t = 2
    store.record_event("new")
    clock.t = 1 + (EVENT_RETENTION_SECONDS if max_age is None else max_age)
    deleted = store.prune_events() if max_age is None else store.prune_events(max_age)
    assert deleted == 1
    assert [row["kind"] for row in store.list_events()] == ["approval", "execution", "cutoff", "new"]
    assert store.prune_events(EVENT_RETENTION_SECONDS if max_age is None else max_age) == 0


@pytest.mark.parametrize("max_age", [0, -1])
def test_prune_events_rejects_nonpositive_age_without_deleting(tmp_path, max_age):
    clock = Clock(0)
    store = _store(tmp_path, clock)
    store.record_event("old")
    before = store.list_events()
    clock.t = 100_000_000
    with pytest.raises(ValueError, match="positive"):
        store.prune_events(max_age)
    assert store.list_events() == before


@pytest.mark.parametrize("deleted", [0, 3])
def test_startup_initializes_then_prunes(deleted, caplog):
    from unittest.mock import Mock, call

    from casa_farnsworth import _init_store

    store = Mock(spec=Store)
    store.prune_events.return_value = deleted
    with caplog.at_level("INFO", logger="planetexpress.farnsworth"):
        _init_store(store)
    assert store.mock_calls == [call.init(), call.prune_events()]
    assert [record.getMessage() for record in caplog.records] == (
        ["Pruned 3 old unlinked events at startup"] if deleted else []
    )
    assert all(record.levelname == "INFO" for record in caplog.records)


def test_startup_prune_failure_logs_and_continues(caplog):
    from unittest.mock import Mock, call

    from casa_farnsworth import _init_store

    store = Mock(spec=Store)
    store.prune_events.side_effect = sqlite3.OperationalError("database is locked")
    _init_store(store)
    assert store.mock_calls == [call.init(), call.prune_events()]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert "Event pruning failed at startup: database is locked" in caplog.text


@pytest.mark.parametrize("version_offset", [0, -1, -2, 1])
def test_schema_compatibility_gate(tmp_path, version_offset):
    from planet_express.core.store import SCHEMA_VERSION, SchemaTooNewError

    path = tmp_path / "gate.db"
    version = SCHEMA_VERSION + version_offset
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    if version_offset > 0:
        with pytest.raises(SchemaTooNewError) as caught:
            Store(path).init()
        assert str(path) in str(caught.value)
        assert f"database version {version}" in str(caught.value)
        assert f"code version {SCHEMA_VERSION}" in str(caught.value)
        assert "scripts/state_snapshot.py" in str(caught.value)
        assert path.read_bytes() == before
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
            assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    else:
        Store(path).init()
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'events'").fetchone()


def test_startup_newer_schema_logs_critical_and_propagates(caplog):
    from unittest.mock import Mock

    from casa_farnsworth import _init_store
    from planet_express.core.store import SchemaTooNewError

    store = Mock(spec=Store)
    error = SchemaTooNewError("database schema is newer")
    store.init.side_effect = error
    with pytest.raises(SchemaTooNewError) as caught:
        _init_store(store)
    assert caught.value is error
    store.prune_events.assert_not_called()
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [("CRITICAL", str(error))]


def test_revoke_devices_newer_schema(tmp_path, capsys):
    from planet_express.core.store import SCHEMA_VERSION
    from scripts.revoke_devices import main

    path = tmp_path / "newer.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    assert main(["alice"], store_factory=lambda _: Store(path)) == 1
    assert "database schema is newer" in capsys.readouterr().err


def test_recent_attempts_filters_and_persists(tmp_path):
    clock = Clock(100)
    store = _store(tmp_path, clock)
    row, _ = _propose(store)
    clock.t = 110
    store.approve_and_create_execution(row['id'], decided_by='chris', arrived_at=clock.t)
    clock.t = 120
    _direct(store, arrived_at=clock.t)
    clock.t = 90
    _direct(store, arrived_at=clock.t)
    for action, key in [('other', 'healthy/web'), ('docker.restart_service', 'other/web')]:
        row, _ = _propose(store, key=key, action=action)
        store.approve_and_create_execution(row['id'], decided_by='chris', arrived_at=clock.t)
    _propose(store)  # Pending proposals do not count as attempts.
    for current in (store, Store(store.path)):
        assert current.recent_attempts('docker.restart_service', 'healthy/web', 0) == [90, 110, 120]
        assert current.recent_attempts('docker.restart_service', 'healthy/web', 110) == [110, 120]
        assert current.recent_attempts('docker.restart_service', 'healthy/web', 121) == []
        assert current.recent_attempts('missing', 'healthy/web', 0) == []


def test_attempt_index_added_without_version_change(tmp_path):
    from planet_express.core.store import SCHEMA_VERSION

    store = _store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        before = conn.execute('PRAGMA user_version').fetchone()[0]
        conn.execute('DROP INDEX approvals_action_target')
    Store(store.path).init()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == before == SCHEMA_VERSION == 2
        assert [r[2] for r in conn.execute('PRAGMA index_info(approvals_action_target)')] == [
            'action', 'target_key',
        ]


def test_chat_ticket_dedupe_and_json(tmp_path):
    s = _store(tmp_path)
    first, created = s.create_chat_ticket(operator='one', submission_id='same', question='?')
    again, repeated = s.create_chat_ticket(operator='one', submission_id='same', question='different')
    other, separate = s.create_chat_ticket(operator='two', submission_id='same', question='?')
    assert created and separate and not repeated
    assert first == again and other['id'] != first['id']
    s.finish_chat_ticket(first['id'], status='done', outcome='answer', evidence=[{'stdout': 'yes'}], cited=[0])
    row = s.get_chat_ticket(first['id'])
    assert row['evidence'] == [{'stdout': 'yes'}] and row['cited'] == [0]
    assert s.get_chat_ticket('absent') is None


def test_chat_reservation_race_and_day_boundary(tmp_path):
    clock = Clock(100)
    s = _store(tmp_path, clock)
    ticket, _ = s.create_chat_ticket(operator='one', submission_id='same', question='?')
    assert s.reserve_llm_call(ticket_id=ticket['id'], limit=2, day_start=0)
    barrier = threading.Barrier(2)
    results = []

    def race():
        independent = Store(s.path, clock=clock)
        barrier.wait()
        results.append(independent.reserve_llm_call(ticket_id=ticket['id'], limit=2, day_start=0))

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sorted(results) == [False, True]
    before = s.list_events()
    assert s.get_chat_ticket(ticket['id'])['llm_calls'] == 2
    assert not s.reserve_llm_call(ticket_id=ticket['id'], limit=2, day_start=0)
    assert s.list_events() == before
    assert s.get_chat_ticket(ticket['id'])['llm_calls'] == 2
    assert all(e['approval_id'] is None for e in before)
    clock.t = 200
    assert s.chat_quota(limit=2, day_start=200) == {'used': 0, 'limit': 2}
    assert s.reserve_llm_call(ticket_id=ticket['id'], limit=2, day_start=200)
    assert s.chat_quota(limit=2, day_start=200)['used'] == 1


def test_chat_interrupt_and_existing_v2(tmp_path):
    s = _store(tmp_path)
    with sqlite3.connect(s.path) as conn:
        conn.execute('DROP TABLE chat_tickets')
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
    s.init()
    rows = [s.create_chat_ticket(operator='one', submission_id=str(i), question='?')[0] for i in range(5)]
    s.set_chat_ticket_running(rows[1]['id'])
    for row, status in zip(rows[2:], ['done', 'failed', 'interrupted'], strict=True):
        s.finish_chat_ticket(row['id'], status=status)
    before = [s.get_chat_ticket(row['id']) for row in rows[2:]]
    assert s.interrupt_chat_tickets('core restarted') == 2
    assert [s.get_chat_ticket(row['id']) for row in rows[2:]] == before
    for row in rows[:2]:
        current = s.get_chat_ticket(row['id'])
        assert current['status'] == 'interrupted' and current['finished_at'] is not None
        assert current['error'] == 'core restarted'
    with sqlite3.connect(s.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2


def test_recent_approvals_expiry_order_latest_and_limit(tmp_path):
    clock = Clock(100)
    store = _store(tmp_path, clock)
    expired, _ = _propose(store, key='expired', ttl_seconds=5)
    denied, _ = _propose(store, key='denied')
    clock.t = 101
    store.consume(denied['id'], decision='denied', decided_by='x', arrived_at=101)
    clock.t = 102
    approved = _direct(store, arrived_at=102)
    clock.t = 103
    latest = store.create_execution(approved['approval']['id'])
    store.set_execution_status(latest['id'], 'passed', 'verified')
    _propose(store, key='pending')
    clock.t = 106
    rows = store.list_recent_approvals(20)
    assert [r['id'] for r in rows] == [expired['id'], approved['approval']['id'], denied['id']]
    assert rows[0]['status'] == 'expired' and rows[0]['execution'] is None
    assert rows[1]['execution'] == {'id': latest['id'], 'status': 'passed', 'started_at': 103,
                                     'finished_at': 103, 'reason': 'verified'}
    assert store.list_recent_approvals(1) == rows[:1]
    for limit in (0, 21, True, '1', 1.5):
        with pytest.raises(ValueError):
            store.list_recent_approvals(limit)
