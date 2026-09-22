"""T33 incident provenance, policy routing and stale-approval refusal."""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

import casa_farnsworth as fw
from notifier import FakeNotifier
from planet_express.application import command_service as command_module
from planet_express.application.command_service import CommandService
from planet_express.core.incidents import fingerprint, scan_id
from planet_express.core.store import IncidentSourceError, Store
from planet_express.execution import actions
from state_models import MonitorSnapshot
from tests.binding_fakes import FakeBinder


def observation(condition="failing"):
    return {
        "fingerprint": fingerprint("container_health", "CASA_WEB"),
        "fingerprint_version": 1,
        "kind": "container_health",
        "resource": "CASA_WEB",
        "condition": condition,
        "severity": "HIGH" if condition == "failing" else None,
        "summary": "web is unhealthy" if condition == "failing" else "web is healthy",
        "details": {},
    }


def snapshot(timestamp="2026-09-18T12:00:00Z"):
    return MonitorSnapshot(timestamp=timestamp, mode="full", containers=[{
        "name": "CASA_WEB", "issue": "unhealthy", "health": "unhealthy",
        "status": "Up", "image": "web:latest", "restart_count": 0,
    }]).model_dump(mode="json")


def current_incident(store, monitor_path, timestamp="2026-09-18T12:00:00Z"):
    data = snapshot(timestamp)
    monitor_path.write_text(json.dumps(data))
    current_scan = scan_id(data)
    incident = store.reconcile_incidents(current_scan, timestamp, [observation()])[0]
    return incident, current_scan


def test_schema_three_upgrade_adds_incident_proposals_without_losing_rows(tmp_path):
    from planet_express.core.store import _SCHEMA

    path = tmp_path / "core.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO events (ts, kind, payload) VALUES (1, 'before-upgrade', '{}')")
        conn.execute("DROP TABLE incident_proposals")
        conn.execute("PRAGMA user_version = 3")
    Store(path).init()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='incident_proposals'"
        ).fetchone()
        assert conn.execute("SELECT kind FROM events").fetchone()[0] == "before-upgrade"


def test_incident_provenance_is_atomic_and_exact_pending_retry_dedups(tmp_path):
    store = Store(tmp_path / "core.db")
    store.init()
    incident = store.reconcile_incidents("a" * 64, "t", [observation()])[0]
    kwargs = {
        "action": actions.RESTART_SERVICE, "target_key": "media/web",
        "target": {"stack": "media", "service": "web", "container": "CASA_WEB"},
        "risk": "R1", "requested_via": "incident", "requested_by": "alice",
        "incident_id": incident["id"], "incident_scan_id": "a" * 64,
    }
    row, created = store.propose(**kwargs)
    retry, retry_created = store.propose(**kwargs)
    assert created and not retry_created and retry["id"] == row["id"]
    assert store.get_incident_proposal(row["id"]) == {
        "approval_id": row["id"], "incident_id": incident["id"], "scan_id": "a" * 64,
    }


def test_incident_proposal_listing_lazily_expires_pending_rows(tmp_path):
    now = [100.0]
    store = Store(tmp_path / "core.db", clock=lambda: now[0])
    store.init()
    incident = store.reconcile_incidents("a" * 64, "t", [observation()])[0]
    row, _ = store.propose(
        action=actions.RESTART_SERVICE, target_key="media/web",
        target={"stack": "media", "service": "web", "container": "CASA_WEB"},
        risk="R1", requested_via="incident", requested_by="alice", ttl_seconds=5,
        incident_id=incident["id"], incident_scan_id="a" * 64,
    )
    now[0] = 106.0
    proposals = store.list_incident_proposals(incident["id"])
    assert proposals[0]["id"] == row["id"]
    assert proposals[0]["status"] == "expired"
    assert store.list_events(row["id"])[-1]["kind"] == "approval.expired"


def test_incident_proposal_rejects_stale_and_unrelated_pending_rows(tmp_path):
    store = Store(tmp_path / "core.db")
    store.init()
    incident = store.reconcile_incidents("a" * 64, "t", [observation()])[0]
    store.propose(
        action=actions.RESTART_SERVICE, target_key="media/web",
        target={"stack": "media", "service": "web", "container": "CASA_WEB"},
        risk="R1", requested_via="telegram",
    )
    with pytest.raises(IncidentSourceError, match="another pending"):
        store.propose(
            action=actions.RESTART_SERVICE, target_key="media/web",
            target={"stack": "media", "service": "web", "container": "CASA_WEB"},
            risk="R1", requested_via="incident", requested_by="alice",
            incident_id=incident["id"], incident_scan_id="a" * 64,
        )
    store.reconcile_incidents("b" * 64, "new", [])
    with pytest.raises(IncidentSourceError, match="no longer current"):
        store.propose(
            action=actions.RESTART_SERVICE, target_key="other/web",
            target={"stack": "other", "service": "web", "container": "CASA_WEB"},
            risk="R1", requested_via="incident", requested_by="alice",
            incident_id=incident["id"], incident_scan_id="a" * 64,
        )


class IncidentEnv:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path / "core.db")
        self.store.init()
        self.monitor = tmp_path / "monitor.json"
        self.incident, self.scan = current_incident(self.store, self.monitor)
        self.state = fw.PipelineState()
        self.notifier = FakeNotifier()
        self.argv = []
        self.identity_calls = []

        def identities(names, *, timeout):
            self.identity_calls.append((list(names), timeout))
            return {"CASA_WEB": ("media", "web")}

        self.service = CommandService(
            self.store, self.notifier, self.state, monitor_path=self.monitor,
            compose_identities=identities,
            resolve_target=lambda *args, **kwargs: actions.Target("media", "web", "CASA_WEB"),
            run_argv=lambda argv, timeout: self.argv.append(argv) or (0, "", ""),
            spawn=lambda fn, *args: fn(*args), restart_count=lambda container: 0,
            verify=lambda container, baseline: (True, "ok"), background=lambda fn: fn(),
            binder=FakeBinder(),
        )


def test_current_container_incident_proposes_typed_restart_and_releases_lock(tmp_path):
    env = IncidentEnv(tmp_path)
    result = env.service.propose_incident(env.incident["id"], operator="alice", timeout=4)
    assert result.ok and result.created
    row = env.store.get_approval(result.approval_id)
    assert (row["action"], row["requested_via"], row["requested_by"]) == (
        actions.RESTART_SERVICE, "incident", "alice",
    )
    assert env.store.get_incident_proposal(result.approval_id)["scan_id"] == env.scan
    assert env.state.mutation_owner is None
    listed = env.service.list_incidents(status="open", limit=20, timeout=4)
    assert listed[0]["hint"]["state"] == "awaiting_approval"


def test_incident_card_delivery_is_handed_off_before_releasing_mutation_lock(tmp_path):
    env = IncidentEnv(tmp_path)
    queued = []
    env.service._background = queued.append
    result = env.service.propose_incident(env.incident["id"], operator="alice", timeout=4)
    assert result.ok and len(queued) == 1
    assert env.state.mutation_owner is None
    assert env.service._proposal_card_lock.locked()
    assert env.notifier.approval_requests == []
    queued[0]()
    assert not env.service._proposal_card_lock.locked()
    assert len(env.notifier.approval_requests) == 1


def test_dashboard_decision_after_deferred_delivery_updates_new_card(tmp_path):
    env = IncidentEnv(tmp_path)
    queued = []
    env.service._background = queued.append
    proposal = env.service.propose_incident(env.incident["id"], operator="alice", timeout=4)
    result = env.service.decide(proposal.approval_id, approve=False, decided_by="alice")
    assert result.outcome == "denied" and len(queued) == 2
    queued[0]()  # card delivery records its message ID
    queued[1]()  # finalizer reloads that ID before editing the card
    assert env.notifier.request_updates[-1][0] == 1


def test_execution_completion_after_deferred_delivery_updates_new_card(tmp_path):
    env = IncidentEnv(tmp_path)
    queued = []
    env.service._background = queued.append
    proposal = env.service.propose_incident(env.incident["id"], operator="alice", timeout=4)
    result = env.service.decide(proposal.approval_id, approve=True, decided_by="alice")
    assert result.outcome == "started" and len(queued) == 3
    for task in queued:
        task()
    assert env.notifier.request_updates[-1][0] == 1
    assert "Verified good" in env.notifier.request_updates[-1][1]


def test_proposal_deadline_exhaustion_after_resolution_is_typed(tmp_path, monkeypatch):
    env = IncidentEnv(tmp_path)
    ticks = iter([0.0, 0.0, 5.0])
    monkeypatch.setattr(command_module.time, "monotonic", lambda: next(ticks))
    result = env.service.propose(
        actions.RESTART_SERVICE, "media", "web", requested_via="dashboard",
        requested_by="alice", timeout=4,
    )
    assert not result.ok and result.reason == "host slow, retry"
    assert env.store.list_pending() == []


def test_direct_execution_does_not_adopt_incident_linked_approval(tmp_path):
    env = IncidentEnv(tmp_path)
    proposal = env.service.propose_incident(env.incident["id"], operator="alice", timeout=4)
    target = {"stack": "media", "service": "web", "container": "CASA_WEB"}
    direct = env.store.create_direct_execution(
        action=actions.RESTART_SERVICE, target_key="media/web", target=target,
        risk="R1", operator="alice", arrived_at=env.store._clock(),
    )
    assert direct["adopted"] is False
    assert direct["approval"]["id"] != proposal.approval_id
    assert env.store.get_approval(proposal.approval_id)["status"] == "pending"


def test_incident_proposal_is_busy_during_scan(tmp_path):
    env = IncidentEnv(tmp_path)
    assert env.state.try_start_run()
    result = env.service.propose_incident(env.incident["id"], operator="alice", timeout=4)
    assert not result.ok and "Busy" in result.reason
    assert env.store.list_pending() == []


def test_incident_proposal_refuses_if_typed_resolution_changes_container(tmp_path):
    env = IncidentEnv(tmp_path)
    env.service._resolve = lambda *args, **kwargs: actions.Target("media", "web", "CASA_OTHER")
    result = env.service.propose_incident(env.incident["id"], operator="alice", timeout=4)
    assert not result.ok and "does not match" in result.reason
    assert env.store.list_pending() == []


def test_stale_incident_approval_is_denied_after_lock_and_never_executes(tmp_path, monkeypatch):
    env = IncidentEnv(tmp_path)
    proposal = env.service.propose_incident(env.incident["id"], operator="alice", timeout=4)
    newer = snapshot("2026-09-18T13:00:00Z")
    env.monitor.write_text(json.dumps(newer))
    env.store.reconcile_incidents(scan_id(newer), newer["timestamp"], [observation("healthy")])
    monkeypatch.setitem(
        actions.REGISTRY, actions.RESTART_SERVICE,
        actions.ActionSpec(actions.RESTART_SERVICE, "R4", "restart"),
    )

    result = env.service.decide(proposal.approval_id, approve=True, decided_by="alice")
    assert result.outcome == "refused"
    assert env.store.get_approval(proposal.approval_id)["status"] == "denied"
    assert env.store.list_executions(proposal.approval_id) == []
    assert env.argv == [] and env.state.mutation_owner is None
    assert env.store.list_events(proposal.approval_id)[-1]["kind"] == "approval.refused_stale_incident"


def test_incident_hints_batch_one_lookup_and_keep_unsupported_informational(tmp_path):
    env = IncidentEnv(tmp_path)
    other = {
        "fingerprint": fingerprint("disk_usage", "/"), "fingerprint_version": 1,
        "kind": "disk_usage", "resource": "/", "condition": "failing", "severity": "HIGH",
        "summary": "disk pressure", "details": {},
    }
    data = snapshot("2026-09-18T13:00:00Z")
    env.monitor.write_text(json.dumps(data))
    env.store.reconcile_incidents(scan_id(data), data["timestamp"], [observation(), other])
    rows = env.service.list_incidents(status="open", limit=20, timeout=4)
    hints = {row["kind"]: row["hint"]["state"] for row in rows}
    assert hints == {"container_health": "proposal_available", "disk_usage": "no_typed_remediation"}
    assert len(env.identity_calls) == 1 and env.identity_calls[0][0] == ["CASA_WEB"]
