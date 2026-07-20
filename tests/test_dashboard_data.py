"""
Tests for dashboard_data.py -- pure logic, no real Docker/host dependency. Reuses the
same fixture shapes as tests/test_state_models.py and the same
monkeypatch.setattr(config, "STATE_*", ...) pattern tests/test_sudo_allowlist.py
establishes for pointing a module's config constant at a tmp_path fixture file.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.yaml"))

import config
import dashboard_data


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


# ── load_*() ──────────────────────────────────────────────────────────────────────

def test_load_monitor_reads_fixture(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "mode": "full",
        "containers": [{"name": "CASA_DOZZLE", "status": "Up"}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    result = dashboard_data.load_monitor()
    assert result is not None
    assert result.containers[0]["name"] == "CASA_DOZZLE"


def test_load_monitor_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "does_not_exist.json")
    assert dashboard_data.load_monitor() is None


def test_load_monitor_malformed_json_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    path.write_text("{not valid json")
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.load_monitor() is None


def test_load_monitor_schema_violation_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    # mode must be one of full/status/updates -- this violates the Literal constraint.
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "not_a_real_mode"})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.load_monitor() is None


def test_load_findings_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "nope.json")
    assert dashboard_data.load_findings() is None


def test_load_update_history_reads_fixture(tmp_path, monkeypatch):
    path = tmp_path / "update_history.json"
    _write(path, {
        "entries": [
            {"ts": "2026-07-15T00:00:00+00:00", "stack": "services", "service": "dozzle",
             "old_id": "sha256:abc", "new_id": "sha256:def", "status": "updated"},
        ],
    })
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", path)
    result = dashboard_data.load_update_history()
    assert result is not None
    assert result.entries[0].service == "dozzle"


# ── summarize_*() ─────────────────────────────────────────────────────────────────

def test_summarize_health_no_state_available(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "nope.json")
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "nope2.json")
    health = dashboard_data.summarize_health()
    assert health["status"] == "unknown"
    assert health["state_available"] is False
    assert health["open_findings"] == 0


def test_summarize_health_critical_status(tmp_path, monkeypatch):
    findings_path = tmp_path / "latest_findings.json"
    _write(findings_path, {
        "analyzed_at": "2026-07-15T14:37:08+00:00",
        "findings": [{"id": "f1", "severity": "CRITICAL"}],
        "has_critical": True,
        "has_high": False,
    })
    monkeypatch.setattr(config, "STATE_FINDINGS", findings_path)
    monkeypatch.setattr(config, "STATE_MONITOR", tmp_path / "nope.json")
    health = dashboard_data.summarize_health()
    assert health["status"] == "critical"
    assert health["open_findings"] == 1


def test_summarize_health_reflects_monitor_severity_with_no_findings_yet(tmp_path, monkeypatch):
    # Regression test: the window between Leela writing STATE_MONITOR and Hermes
    # finishing analysis (every pipeline run has one) previously showed "ok" no
    # matter how bad the monitor snapshot looked, since only findings.has_critical/
    # has_high were consulted.
    monitor_path = tmp_path / "latest_monitor.json"
    _write(monitor_path, {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "mode": "full",
        "containers": [{"name": "CASA_BAD", "status": "Restarting", "crash_looping": True}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", monitor_path)
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "nope.json")
    health = dashboard_data.summarize_health()
    assert health["status"] == "critical"
    assert health["crash_looping_count"] == 1


def test_summarize_health_disk_critical_without_findings(tmp_path, monkeypatch):
    monitor_path = tmp_path / "latest_monitor.json"
    _write(monitor_path, {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "mode": "full",
        "disk": [{"mount": "/", "source": "/dev/sdb2", "used_pct": 95, "alert": "CRITICAL"}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", monitor_path)
    monkeypatch.setattr(config, "STATE_FINDINGS", tmp_path / "nope.json")
    assert dashboard_data.summarize_health()["status"] == "critical"


# ── mode-gated availability (an /updates or /status run overwrites STATE_MONITOR
# with a partial snapshot -- fields that mode doesn't populate must never be shown
# as confirmed-zero real data) ──────────────────────────────────────────────────

def test_summarize_containers_unavailable_after_updates_mode_run(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "updates"})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    result = dashboard_data.summarize_containers()
    assert result["available"] is False


def test_summarize_containers_available_after_status_mode_run(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": "2026-07-15T14:36:53+00:00", "mode": "status",
        "containers": [{"name": "CASA_OK", "status": "Up"}],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    result = dashboard_data.summarize_containers()
    assert result["available"] is True
    assert result["total"] == 1


def test_summarize_stack_completeness_unavailable_after_status_mode_run(tmp_path, monkeypatch):
    # stack_completeness is full-mode-only, unlike containers (full+status).
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "status", "containers": []})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.summarize_stack_completeness()["available"] is False


def test_summarize_disk_unavailable_after_updates_mode_run(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "updates"})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.summarize_disk() == {"list": [], "available": False}


def test_summarize_system_and_backups_unavailable_outside_full_mode(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {"timestamp": "2026-07-15T14:36:53+00:00", "mode": "status"})
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    assert dashboard_data.summarize_system_and_backups()["available"] is False


# ── update history alert classification ──────────────────────────────────────────

def test_update_history_successful_update_not_flagged_as_alert(tmp_path, monkeypatch):
    path = tmp_path / "update_history.json"
    _write(path, {"entries": [
        {"ts": "2026-07-15T00:00:00+00:00", "stack": "services", "service": "dozzle",
         "old_id": "sha256:1", "new_id": "sha256:2", "status": "updated"},
    ]})
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", path)
    result = dashboard_data.summarize_update_history()
    assert result[0]["is_alert"] is False


def test_update_history_rollback_failed_flagged_as_alert(tmp_path, monkeypatch):
    path = tmp_path / "update_history.json"
    _write(path, {"entries": [
        {"ts": "2026-07-15T00:00:00+00:00", "stack": "services", "service": "planka",
         "old_id": "sha256:1", "new_id": "sha256:2", "status": "rollback_failed",
         "reason": "crash-looped after update"},
    ]})
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", path)
    result = dashboard_data.summarize_update_history()
    assert result[0]["is_alert"] is True


def test_summarize_findings_counts_and_sorts_by_severity(tmp_path, monkeypatch):
    path = tmp_path / "latest_findings.json"
    _write(path, {
        "analyzed_at": "2026-07-15T14:37:08+00:00",
        "findings": [
            {"id": "f1", "severity": "LOW", "resource": "a"},
            {"id": "f2", "severity": "CRITICAL", "resource": "b"},
            {"id": "f3", "severity": "MEDIUM", "resource": "c"},
        ],
        "has_critical": True,
        "has_high": False,
    })
    monkeypatch.setattr(config, "STATE_FINDINGS", path)
    result = dashboard_data.summarize_findings()
    assert result["counts"] == {"critical": 1, "high": 0, "medium": 1, "low": 1}
    # sorted CRITICAL -> LOW
    assert [f["id"] for f in result["list"]] == ["f2", "f3", "f1"]


def test_summarize_containers_filters_to_issues_only(tmp_path, monkeypatch):
    path = tmp_path / "latest_monitor.json"
    _write(path, {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "mode": "full",
        "containers": [
            {"name": "CASA_OK", "status": "Up"},
            {"name": "CASA_BAD", "status": "Restarting", "issue": "crash-looping"},
        ],
    })
    monkeypatch.setattr(config, "STATE_MONITOR", path)
    result = dashboard_data.summarize_containers()
    assert result["total"] == 2
    assert result["healthy"] == 1
    assert len(result["issues"]) == 1
    assert result["issues"][0]["name"] == "CASA_BAD"


def test_summarize_rollback_candidates_excludes_expired(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    path = tmp_path / "rollback_candidates.json"
    _write(path, {
        "candidates": [
            {"stack": "services", "service": "expired_one", "old_image_id": "sha256:1",
             "recorded_at": (now - timedelta(hours=2)).isoformat(),
             "expires_at": (now - timedelta(hours=1)).isoformat()},
            {"stack": "services", "service": "still_open", "old_image_id": "sha256:2",
             "recorded_at": now.isoformat(),
             "expires_at": (now + timedelta(hours=1)).isoformat()},
        ],
    })
    monkeypatch.setattr(config, "ROLLBACK_CANDIDATES_FILE", path)
    result = dashboard_data.summarize_rollback_candidates()
    assert len(result) == 1
    assert result[0]["service"] == "still_open"


def test_summarize_update_history_newest_first_and_capped(tmp_path, monkeypatch):
    path = tmp_path / "update_history.json"
    entries = [
        {"ts": f"2026-07-{d:02d}T00:00:00+00:00", "stack": "services", "service": f"svc{d}",
         "old_id": "sha256:1", "new_id": "sha256:2", "status": "updated"}
        for d in range(1, 6)
    ]
    _write(path, {"entries": entries})
    monkeypatch.setattr(config, "UPDATE_HISTORY_FILE", path)
    result = dashboard_data.summarize_update_history(limit=3)
    assert len(result) == 3
    assert result[0]["service"] == "svc5"  # newest first


def test_summarize_pending_plan_none_when_no_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_PLAN", tmp_path / "nope.json")
    assert dashboard_data.summarize_pending_plan() is None


def test_summarize_pending_plan_hides_step_commands(tmp_path, monkeypatch):
    plan_path = tmp_path / "pending_plan.json"
    _write(plan_path, {
        "planned_at": "2026-07-15T14:37:15+00:00",
        "plans": [{"id": "p1", "priority": "medium", "title": "test",
                   "steps": [{"command": "sudo systemctl restart x"}, {"command": "echo done"}],
                   "rollback": []}],
    })
    monkeypatch.setattr(config, "STATE_PLAN", plan_path)
    status_path = tmp_path / "run_status.json"
    _write(status_path, {
        "state": "awaiting_approval", "pending_plan_id": "p1", "updated_at": "2026-07-15T14:37:20+00:00",
    })
    monkeypatch.setattr(config, "STATE_STATUS", status_path)

    result = dashboard_data.summarize_pending_plan()
    assert result["plans"][0]["step_count"] == 2
    assert "command" not in json.dumps(result)  # step commands never surface here


def test_summarize_pending_plan_hidden_once_run_status_moves_on(tmp_path, monkeypatch):
    # pending_plan.json is never deleted after resolution -- RunStatus is the only
    # live signal that a plan is still genuinely pending, not just "the file still
    # has an old plan in it."
    plan_path = tmp_path / "pending_plan.json"
    _write(plan_path, {
        "planned_at": "2026-07-15T14:37:15+00:00",
        "plans": [{"id": "p1", "priority": "medium", "title": "test", "steps": [], "rollback": []}],
    })
    monkeypatch.setattr(config, "STATE_PLAN", plan_path)
    status_path = tmp_path / "run_status.json"
    _write(status_path, {"state": "idle", "pending_plan_id": None, "updated_at": "2026-07-15T15:00:00+00:00"})
    monkeypatch.setattr(config, "STATE_STATUS", status_path)

    assert dashboard_data.summarize_pending_plan() is None


# ── build_dashboard_context() ─────────────────────────────────────────────────────

def test_build_dashboard_context_never_raises_with_no_state(tmp_path, monkeypatch):
    for attr in (
        "STATE_MONITOR", "STATE_FINDINGS", "STATE_PLAN", "STATE_STATUS",
        "ROLLBACK_CANDIDATES_FILE", "UPDATE_HISTORY_FILE",
    ):
        monkeypatch.setattr(config, attr, tmp_path / f"{attr}_missing.json")

    ctx = dashboard_data.build_dashboard_context()

    assert ctx["health"]["state_available"] is False
    assert ctx["findings"]["list"] == []
    assert ctx["containers"]["total"] == 0
    assert ctx["pending_plan"] is None
    assert ctx["update_history"] == []
    assert ctx["rollback_candidates"] == []
