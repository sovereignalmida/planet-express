"""
dashboard_data.py — read-only summarization layer for the web dashboard (casa_scruffy.py).

Pure logic, zero Flask import. Every load_*() reads its config.STATE_* path at call
time (matching how every other module in this repo reads these paths inline) and
returns Optional[Model] -- None on a missing or malformed file, never a raised
exception. A passive glance dashboard must never fail to render because one state
file doesn't exist yet (fresh install, no pipeline run) or is mid-write-torn.

Every summarize_*() returns a plain JSON-primitive dict -- no pydantic objects escape
this module -- so they're already jsonify()-safe for a future JSON route (Spec 7's
Homepage-widget endpoint) with no serialization pass to invent later.
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError

import config
from state_models import (
    Findings,
    MonitorSnapshot,
    PlanSet,
    RollbackCandidates,
    RunStatus,
    UpdateHistory,
)


def _load(path, model_cls):
    try:
        text = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        return model_cls.model_validate_json(text)
    except (ValidationError, ValueError):
        return None


def load_monitor() -> Optional[MonitorSnapshot]:
    return _load(config.STATE_MONITOR, MonitorSnapshot)


def load_findings() -> Optional[Findings]:
    return _load(config.STATE_FINDINGS, Findings)


def load_plan() -> Optional[PlanSet]:
    return _load(config.STATE_PLAN, PlanSet)


def load_status() -> Optional[RunStatus]:
    return _load(config.STATE_STATUS, RunStatus)


def load_rollback_candidates() -> Optional[RollbackCandidates]:
    return _load(config.ROLLBACK_CANDIDATES_FILE, RollbackCandidates)


def load_update_history() -> Optional[UpdateHistory]:
    return _load(config.UPDATE_HISTORY_FILE, UpdateHistory)


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _severity_rank(finding: dict) -> int:
    return _SEVERITY_ORDER.get(str(finding.get("severity", "")).lower(), len(_SEVERITY_ORDER))


def summarize_health() -> dict:
    """Shaped to already match Spec 7's future Homepage-widget contract (status,
    last scan time, open findings count) -- Spec 7 becomes jsonify(summarize_health())
    behind one new route, no change needed here."""
    monitor = load_monitor()
    findings = load_findings()

    open_findings = len(findings.findings) if findings else 0
    has_critical = findings.has_critical if findings else False
    has_high = findings.has_high if findings else False

    if not monitor and not findings:
        status = "unknown"
    elif has_critical:
        status = "critical"
    elif has_high:
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "container_count": len(monitor.containers) if monitor else 0,
        "unhealthy_count": sum(1 for c in (monitor.containers if monitor else []) if c.get("issue")),
        "crash_looping_count": sum(
            1 for c in (monitor.containers if monitor else []) if c.get("crash_looping")
        ),
        "disk_alerts": sum(1 for d in (monitor.disk if monitor else []) if d.get("alert")),
        "open_findings": open_findings,
        "last_scan": monitor.timestamp if monitor else None,
        "state_available": monitor is not None or findings is not None,
    }


def summarize_findings() -> dict:
    # "list" not "items" -- a dict key literally named "items"/"keys"/"values"/etc.
    # collides with Jinja2's dot-attribute-access shorthand (it tries getattr()
    # before __getitem__, and every dict has a real builtin .items() method), which
    # would silently return the bound method instead of the value in the template.
    findings = load_findings()
    if not findings:
        return {"counts": {"critical": 0, "high": 0, "medium": 0, "low": 0}, "list": [], "analyzed_at": None}

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings.findings:
        sev = str(f.get("severity", "")).lower()
        if sev in counts:
            counts[sev] += 1

    ranked = sorted(findings.findings, key=_severity_rank)
    return {"counts": counts, "list": ranked, "analyzed_at": findings.analyzed_at}


def summarize_pipeline_status() -> dict:
    status = load_status()
    if not status:
        return {"state": "unknown", "pending_plan_id": None, "updated_at": None}
    return {
        "state": status.state,
        "pending_plan_id": status.pending_plan_id,
        "updated_at": status.updated_at,
    }


def summarize_containers() -> dict:
    monitor = load_monitor()
    if not monitor:
        return {"total": 0, "healthy": 0, "issues": []}
    issues = [c for c in monitor.containers if c.get("issue")]
    return {"total": len(monitor.containers), "healthy": len(monitor.containers) - len(issues), "issues": issues}


def summarize_stack_completeness() -> dict:
    monitor = load_monitor()
    if not monitor:
        return {"total": 0, "complete": 0, "incomplete": []}
    incomplete = [s for s in monitor.stack_completeness if s.get("alert")]
    return {
        "total": len(monitor.stack_completeness),
        "complete": len(monitor.stack_completeness) - len(incomplete),
        "incomplete": incomplete,
    }


def summarize_disk() -> list[dict]:
    monitor = load_monitor()
    return monitor.disk if monitor else []


def summarize_update_history(limit: int = 20) -> list[dict]:
    history = load_update_history()
    if not history:
        return []
    entries = [e.model_dump() for e in history.entries]
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:limit]


def summarize_rollback_candidates() -> list[dict]:
    candidates = load_rollback_candidates()
    if not candidates:
        return []
    now = datetime.now(timezone.utc).isoformat()
    unexpired = [c for c in candidates.candidates if c.expires_at > now]
    return [c.model_dump() for c in unexpired]


def summarize_pending_plan() -> Optional[dict]:
    plan_set = load_plan()
    if not plan_set or not plan_set.plans:
        return None
    plans = [
        {
            "id": p.get("id"),
            "priority": p.get("priority"),
            "title": p.get("title"),
            "step_count": len(p.get("steps", [])),
        }
        for p in plan_set.plans
    ]
    return {"planned_at": plan_set.planned_at, "plans": plans}


def summarize_system_and_backups() -> dict:
    monitor = load_monitor()
    if not monitor:
        return {"system": {}, "backups": {}}
    return {"system": monitor.system, "backups": monitor.backups}


def build_dashboard_context() -> dict:
    """Single entry point the Flask route calls."""
    return {
        "health": summarize_health(),
        "findings": summarize_findings(),
        "pipeline_status": summarize_pipeline_status(),
        "containers": summarize_containers(),
        "stack_completeness": summarize_stack_completeness(),
        "disk": summarize_disk(),
        "update_history": summarize_update_history(),
        "rollback_candidates": summarize_rollback_candidates(),
        "pending_plan": summarize_pending_plan(),
        "system_and_backups": summarize_system_and_backups(),
    }
