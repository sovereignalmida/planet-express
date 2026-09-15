"""
planet_express/core/store.py (landing 1b): dedup, one-use approvals, lazy expiry,
arrival-time expiry, racing consumes, execution lifecycle, startup interruption.
"""

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
