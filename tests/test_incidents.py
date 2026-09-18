import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault(
    "CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml")
)

from planet_express.core.incidents import (
    Observation,
    fingerprint,
    observations_from_snapshot,
    scan_id,
)
from planet_express.core.store import Store


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def _store(tmp_path, clock=None):
    store = Store(tmp_path / "state" / "actions.db", clock=clock or Clock())
    store.init()
    return store


def _observation(condition="failing", severity="HIGH", *, resource="CASA_DB", summary="down"):
    return Observation(
        fingerprint("container_health", resource),
        1,
        "container_health",
        resource,
        condition,
        severity,
        summary,
        {"status": summary},
    ).as_dict()


def test_extracts_every_incident_family_with_stable_identity():
    snapshot = {
        "containers": [
            {"name": "CASA_DB", "status": "Exited (1)", "image": "postgres:17", "issue": "not running"},
            {"name": "CASA_BOOT", "status": "Up", "health": "starting"},
            {"name": "CASA_DB_BOOT", "status": "Up", "health": "starting", "image": "postgres:17", "issue": "healthcheck still initialising"},
            {"name": "CASA_DB_DONE", "status": "Exited (0)", "image": "postgres:17", "issue": "not running"},
        ],
        "stack_completeness": [
            {"stack": "media", "alert": "CRITICAL", "missing_services": ["radarr"], "services": {}},
            {"stack": "boot", "status": "unknown", "alert": "MEDIUM", "error": "unreadable"},
        ],
        "disk": [{"mount": "/", "used_pct": 96, "alert": "CRITICAL"}],
        "mounts": {"missing": ["/mnt/media"]},
        "unraid_exports": {"reachable": False, "error": "timeout", "duplicate_fsids": []},
        "nfs_mount_health": [
            {"container": "CASA_TA", "path": "/youtube", "status": "stale", "alert": "HIGH"},
            {"container": None, "path": None, "status": "unknown", "alert": "MEDIUM", "error": "discovery"},
            {"container": "CASA_SKIP", "path": "/data", "status": "unavailable"},
        ],
        "vpn_port_forwarding": {"reachable": False, "alert": "HIGH", "issue": "dead"},
        "backups": {"daily": {"result": "failed", "last_run": "today"}},
        "services": {"casa-stacks": "inactive"},
        "certs": [
            {"resolver": "internal", "status": "expired", "days_remaining": -1},
            {"resolver": "missing", "error": "cannot read"},
            {"note": "no configured certificates"},
        ],
    }

    rows = {(row.kind, row.resource): row for row in observations_from_snapshot(snapshot)}
    expected = {
        ("container_health", "CASA_DB"): ("failing", "HIGH"),
        ("container_health", "CASA_BOOT"): ("unknown", None),
        ("container_health", "CASA_DB_BOOT"): ("failing", "MEDIUM"),
        ("container_health", "CASA_DB_DONE"): ("failing", "MEDIUM"),
        ("stack_completeness", "media"): ("failing", "CRITICAL"),
        ("stack_completeness", "boot"): ("unknown", "MEDIUM"),
        ("disk_usage", "/"): ("failing", "CRITICAL"),
        ("configured_mounts", "host-mounts"): ("failing", "HIGH"),
        ("unraid_exports", "unraid"): ("unknown", "MEDIUM"),
        ("nfs_mount", "CASA_TA:/youtube"): ("failing", "HIGH"),
        ("nfs_discovery", "mount-discovery"): ("unknown", "MEDIUM"),
        ("vpn_port_forwarding", "gluetun-qbittorrent"): ("failing", "HIGH"),
        ("backup_job", "daily"): ("failing", "HIGH"),
        ("systemd_service", "casa-stacks"): ("failing", "MEDIUM"),
        ("tls_certificate", "internal"): ("failing", "CRITICAL"),
        ("tls_certificate", "missing"): ("failing", "HIGH"),
    }
    assert {key: (row.condition, row.severity) for key, row in rows.items()} == expected


def test_extraction_is_sorted_redacted_bounded_and_fingerprint_ignores_details():
    long_secret = "API_KEY=incident-secret " + "x" * 2000
    first = {
        "containers": [
            {"name": "z", "status": long_secret, "image": "app", "issue": long_secret},
            {"name": "a", "status": "Exited", "image": "app", "issue": "old wording"},
        ]
    }
    second = {
        "containers": [
            {"name": "a", "status": "Exited 1", "image": "app", "issue": "new wording"},
            {"name": "z", "status": "different", "image": "app", "issue": "different"},
        ]
    }
    left = observations_from_snapshot(first)
    right = observations_from_snapshot(second)
    assert [(row.kind, row.resource) for row in left] == sorted(
        (row.kind, row.resource) for row in left
    )
    assert {row.resource: row.fingerprint for row in left} == {
        row.resource: row.fingerprint for row in right
    }
    encoded = json.dumps([row.as_dict() for row in left])
    assert "incident-secret" not in encoded
    assert all(len(row.summary) <= 1024 for row in left)


def test_malformed_subjects_are_omitted_and_identity_is_rejected():
    rows = observations_from_snapshot({"containers": [{"name": "", "issue": "bad"}], "disk": [{}]})
    assert all(row.kind not in {"container_health", "disk_usage"} for row in rows)
    with pytest.raises(ValueError):
        fingerprint("container_health", "")
    with pytest.raises(ValueError):
        fingerprint("x" * 65, "resource")


def test_absent_global_checks_never_emit_false_healthy_observations():
    assert observations_from_snapshot({}) == []
    assert observations_from_snapshot(
        {"mounts": None, "unraid_exports": {}, "vpn_port_forwarding": {}}
    ) == []
    kinds = {
        row.kind
        for row in observations_from_snapshot(
            {
                "mounts": {"missing": []},
                "unraid_exports": {"reachable": True, "duplicate_fsids": []},
                "vpn_port_forwarding": {"reachable": True},
            }
        )
    }
    assert kinds == {"configured_mounts", "unraid_exports", "vpn_port_forwarding"}


def test_incident_lifecycle_unknown_missing_retry_and_receipts(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    failing = _observation()

    opened = store.reconcile_incidents("1" * 64, "t1", [failing])[0]
    incident_id = opened["id"]
    assert opened["occurrences"] == 1 and opened["status"] == "open"

    clock.value = 101
    observed = store.reconcile_incidents("2" * 64, "t2", [_observation(summary="still down")])[0]
    assert observed["id"] == incident_id and observed["occurrences"] == 2
    assert store.reconcile_incidents("2" * 64, "t2", [failing]) == []

    clock.value = 102
    resolved = store.reconcile_incidents("3" * 64, "t3", [_observation("healthy", None)])[0]
    assert resolved["status"] == "resolved" and resolved["occurrences"] == 2

    clock.value = 103
    assert store.reconcile_incidents("4" * 64, "t4", [_observation("unknown", None)]) == []
    assert store.get_incident(incident_id)["status"] == "resolved"

    clock.value = 104
    reopened = store.reconcile_incidents("5" * 64, "t5", [failing])[0]
    assert reopened["id"] == incident_id and reopened["first_seen"] == 100
    assert reopened["occurrences"] == 3 and reopened["status"] == "open"

    clock.value = 105
    store.reconcile_incidents("6" * 64, "t6", [])
    assert store.get_incident(incident_id)["last_observed_scan_id"] == "5" * 64
    assert store.latest_incident_reconciliation()["scan_id"] == "6" * 64
    assert [event["kind"] for event in store.list_incident_events(incident_id)] == [
        "opened", "observed", "resolved", "reopened"
    ]


def test_alerted_unknown_opens_unalerted_unknown_only_updates_open(tmp_path):
    store = _store(tmp_path)
    quiet = _observation("unknown", None)
    assert store.reconcile_incidents("a" * 64, "t1", [quiet]) == []
    assert store.list_incidents() == []
    alert = _observation("unknown", "MEDIUM")
    opened = store.reconcile_incidents("b" * 64, "t2", [alert])[0]
    updated = store.reconcile_incidents("c" * 64, "t3", [quiet])[0]
    assert opened["id"] == updated["id"] and updated["condition"] == "unknown"
    assert updated["occurrences"] == 2


def test_invalid_batch_rolls_back_and_reads_validate_bounds(tmp_path):
    store = _store(tmp_path)
    item = _observation()
    duplicate = [item, dict(item)]
    with pytest.raises(ValueError, match="duplicate"):
        store.reconcile_incidents("d" * 64, "t", duplicate)
    assert store.latest_incident_reconciliation() is None
    assert store.list_incidents() == []

    bad = dict(item, fingerprint="e" * 64)
    with pytest.raises(ValueError, match="does not match"):
        store.reconcile_incidents("e" * 64, "t", [bad])
    with pytest.raises(ValueError):
        store.list_incidents(status="invalid")
    with pytest.raises(ValueError):
        store.list_incidents(limit=0)


def test_latest_reconciliation_uses_commit_order_when_clock_moves_backward(tmp_path):
    now = [200.0]
    store = Store(tmp_path / "core.db", clock=lambda: now[0])
    store.init()
    store.reconcile_incidents("a" * 64, "first", [])
    now[0] = 100.0
    store.reconcile_incidents("b" * 64, "second", [])
    assert store.latest_incident_reconciliation()["scan_id"] == "b" * 64


def test_concurrent_distinct_scans_share_one_incident(tmp_path):
    store = _store(tmp_path)
    barrier = threading.Barrier(2)
    errors = []

    def reconcile(scan):
        try:
            barrier.wait()
            Store(store.path).reconcile_incidents(scan * 64, scan, [_observation()])
        except (sqlite3.Error, RuntimeError, ValueError) as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=reconcile, args=(value,)) for value in ("1", "2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert len(store.list_incidents()) == 1
    assert store.list_incidents()[0]["occurrences"] == 2


def test_schema_two_upgrade_preserves_existing_data_atomically(tmp_path):
    from planet_express.core.store import _SCHEMA

    path = tmp_path / "old.db"
    legacy_schema = _SCHEMA[_SCHEMA.index("CREATE TABLE IF NOT EXISTS chat_tickets"):]
    with sqlite3.connect(path) as conn:
        conn.executescript(legacy_schema)
        conn.execute("PRAGMA user_version = 2")
        conn.execute("INSERT INTO events (ts, kind, payload) VALUES (1, 'old', '{}')")
    Store(path).init()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT kind FROM events").fetchone()[0] == "old"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"incidents", "incident_events", "incident_reconciliations"} <= tables


def test_scan_id_is_canonical_and_timestamp_participates():
    assert scan_id({"timestamp": "one", "a": 1}) == scan_id({"a": 1, "timestamp": "one"})
    assert scan_id({"timestamp": "one"}) != scan_id({"timestamp": "two"})


def test_latest_reconciliation_uses_insertion_order_when_clocks_tie(tmp_path):
    store = _store(tmp_path, Clock(100))
    store.reconcile_incidents("f" * 64, "older", [])
    store.reconcile_incidents("0" * 64, "newer", [])
    assert store.latest_incident_reconciliation()["scan_id"] == "0" * 64
