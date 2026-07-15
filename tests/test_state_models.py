"""
Fixture-based round-trip tests for state_models.py -- construct each model from a
realistic dict (matching real on-disk shapes observed on this host), dump to JSON,
reload, and confirm the result round-trips cleanly with schema_version present.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state_models import (
    Findings,
    MonitorSnapshot,
    PlanSet,
    RollbackCandidates,
    RunStatus,
    UpdateHistory,
)


def _roundtrip(model_cls, data):
    instance = model_cls(**data)
    reloaded = model_cls(**json.loads(instance.model_dump_json()))
    return instance, reloaded


def test_monitor_snapshot_full_mode():
    data = {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "agent": "casa_leela",
        "mode": "full",
        "containers": [{"name": "CASA_DOZZLE", "status": "Up", "health": "healthy", "image": "amir20/dozzle"}],
        "stack_completeness": [],
        "disk": [{"mount": "/", "source": "/dev/sdb2", "used_pct": 54}],
        "docker_disk": {},
        "mounts": {"active_count": 5, "missing": []},
        "system": {},
        "backups": {},
        "services": {},
        "image_candidates": [{"repo": "jellyfin/jellyfin", "tag": "latest", "stale_days": 35}],
        "certs": [],
    }
    instance, reloaded = _roundtrip(MonitorSnapshot, data)
    assert instance.schema_version == 1
    assert reloaded.mounts["active_count"] == 5


def test_monitor_snapshot_status_mode_partial_fields():
    # run_status()/run_updates() only populate a subset of keys -- must still validate.
    data = {
        "timestamp": "2026-07-15T14:36:53+00:00",
        "agent": "casa_leela",
        "mode": "status",
        "containers": [],
        "disk": [],
        "services": {},
    }
    instance, reloaded = _roundtrip(MonitorSnapshot, data)
    assert instance.mode == "status"
    assert instance.image_candidates == []  # defaulted, not present in input


def test_findings_tolerates_llm_parse_error_extras():
    data = {
        "analyzed_at": "2026-07-15T14:37:08+00:00",
        "findings": [{
            "id": "f1", "severity": "MEDIUM", "category": "backup",
            "resource": "backups.daily", "description": "...", "suggested_action": "...",
        }],
        "has_critical": False,
        "has_high": False,
        "update_candidates": [],
        "_parse_error": "some LLM hiccup",
    }
    instance, reloaded = _roundtrip(Findings, data)
    assert instance.schema_version == 1
    assert reloaded.model_extra.get("_parse_error") == "some LLM hiccup"


def test_plan_set_roundtrip():
    data = {
        "planned_at": "2026-07-15T14:37:15+00:00",
        "plans": [{"id": "p1", "priority": "medium", "title": "test", "steps": [], "rollback": []}],
        "expires_at": "2026-07-16T14:37:15+00:00",
    }
    instance, reloaded = _roundtrip(PlanSet, data)
    assert reloaded.plans[0]["id"] == "p1"


def test_run_status_roundtrip():
    data = {
        "state": "awaiting_approval",
        "pending_plan_id": "p1",
        "pending_msg_id": 397,
        "updated_at": "2026-07-15T14:37:17+00:00",
    }
    instance, reloaded = _roundtrip(RunStatus, data)
    assert reloaded.pending_msg_id == 397


def test_rollback_candidates_roundtrip():
    data = {
        "candidates": [{
            "stack": "services", "service": "dozzle", "old_image_id": "sha256:abc",
            "recorded_at": "2026-07-15T00:00:00+00:00",
            "expires_at": "2026-07-15T00:15:00+00:00",
        }],
    }
    instance, reloaded = _roundtrip(RollbackCandidates, data)
    assert reloaded.candidates[0].stack == "services"


def test_update_history_roundtrip_and_reason_optional():
    data = {
        "entries": [
            {"ts": "2026-07-15T00:00:00+00:00", "stack": "services", "service": "dozzle",
             "old_id": "sha256:abc", "new_id": "sha256:def", "status": "updated"},
            {"ts": "2026-07-15T00:05:00+00:00", "stack": "services", "service": "planka",
             "old_id": "sha256:111", "new_id": "sha256:222", "status": "rolled_back",
             "reason": "crash-looped after update"},
        ],
    }
    instance, reloaded = _roundtrip(UpdateHistory, data)
    assert reloaded.entries[0].reason is None
    assert reloaded.entries[1].reason == "crash-looped after update"
