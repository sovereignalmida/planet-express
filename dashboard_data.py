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
    behind one new route, no change needed here.

    Status blends BOTH signals, not just Hermes' findings -- an independent Codex
    review caught that the original version only looked at findings.has_critical/
    has_high, so a monitor snapshot showing real crash loops/disk-critical/incomplete
    stacks stayed "ok" for the entire window between Leela writing STATE_MONITOR and
    Hermes finishing analysis (every pipeline run has one), and indefinitely if Hermes
    ever failed outright. Monitor-derived severity is now a first-class input, not an
    afterthought only surfaced as raw counts."""
    monitor = load_monitor()
    findings = load_findings()

    open_findings = len(findings.findings) if findings else 0
    has_critical = findings.has_critical if findings else False
    has_high = findings.has_high if findings else False

    containers = monitor.containers if monitor else []
    disk = monitor.disk if monitor else []
    stacks = monitor.stack_completeness if monitor else []

    crash_looping_count = sum(1 for c in containers if c.get("crash_looping"))
    unhealthy_count = sum(1 for c in containers if c.get("issue"))
    disk_critical = any(d.get("alert") == "CRITICAL" for d in disk)
    disk_high = any(d.get("alert") == "HIGH" for d in disk)
    stack_critical_or_high = any(s.get("alert") in ("CRITICAL", "HIGH") for s in stacks)
    stack_low = any(s.get("alert") == "LOW" for s in stacks)

    if not monitor and not findings:
        status = "unknown"
    elif has_critical or crash_looping_count > 0 or disk_critical or stack_critical_or_high:
        status = "critical"
    elif has_high or unhealthy_count > 0 or disk_high or stack_low:
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "container_count": len(containers),
        "unhealthy_count": unhealthy_count,
        "crash_looping_count": crash_looping_count,
        "disk_alerts": sum(1 for d in disk if d.get("alert")),
        "open_findings": open_findings,
        "last_scan": monitor.timestamp if monitor else None,
        "last_scan_mode": monitor.mode if monitor else None,
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


# Which MonitorSnapshot fields each casa_leela.run_*() mode actually populates --
# run_status() covers containers/disk/services only, run_updates() covers only
# image_candidates, only run_full() covers everything. An independent Codex review
# caught that the dashboard was treating a partial mode's untouched fields (default
# empty lists on the pydantic model) as confirmed-zero real data -- e.g. running
# /updates overwrites STATE_MONITOR with a snapshot that has no containers/stacks/disk
# data at all, and the dashboard showed "0/0 containers healthy" as if that were a real
# observation, not "not collected this run." These sets gate what's safe to trust.
_MODES_WITH_CONTAINERS = {"full", "status"}
_MODES_WITH_DISK = {"full", "status"}
_MODES_WITH_STACK_COMPLETENESS = {"full"}
_MODES_WITH_SYSTEM_AND_BACKUPS = {"full"}


def summarize_containers() -> dict:
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_CONTAINERS:
        return {"total": 0, "healthy": 0, "issues": [], "available": False}
    issues = [c for c in monitor.containers if c.get("issue")]
    return {
        "total": len(monitor.containers),
        "healthy": len(monitor.containers) - len(issues),
        "issues": issues,
        "available": True,
    }


def summarize_stack_completeness() -> dict:
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_STACK_COMPLETENESS:
        return {"total": 0, "complete": 0, "incomplete": [], "available": False}
    incomplete = [s for s in monitor.stack_completeness if s.get("alert")]
    return {
        "total": len(monitor.stack_completeness),
        "complete": len(monitor.stack_completeness) - len(incomplete),
        "incomplete": incomplete,
        "available": True,
    }


def summarize_disk() -> dict:
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_DISK:
        return {"list": [], "available": False}
    return {"list": monitor.disk, "available": True}


# Real casa_zoidberg.py status vocabulary, not the "stable"/"success" values the
# first draft template guessed at (which don't actually occur) -- an independent Codex
# review caught that this meant every real successful update ("updated") rendered with
# the red alert-row styling. Decided here in Python, not string-compared ad hoc in the
# template, since this is a business-logic classification, not presentation.
_UPDATE_HISTORY_NON_ALERT_STATUSES = {"updated", "no_change"}


def summarize_update_history(limit: int = 20) -> list[dict]:
    history = load_update_history()
    if not history:
        return []
    entries = [e.model_dump() for e in history.entries]
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    for e in entries:
        e["is_alert"] = e.get("status") not in _UPDATE_HISTORY_NON_ALERT_STATUSES
    return entries[:limit]


def summarize_rollback_candidates() -> list[dict]:
    candidates = load_rollback_candidates()
    if not candidates:
        return []
    now = datetime.now(timezone.utc).isoformat()
    unexpired = [c for c in candidates.candidates if c.expires_at > now]
    return [c.model_dump() for c in unexpired]


def summarize_pending_plan() -> Optional[dict]:
    """pending_plan.json is never deleted after a plan is approved/executed or
    cancelled (confirmed: no unlink() of it anywhere in casa_farnsworth.py) -- an
    independent Codex review caught that checking only "does this file have plans in
    it" kept showing an already-resolved plan as pending indefinitely, until the next
    scheduled run happened to overwrite it. RunStatus.state/pending_plan_id is the one
    live signal that's actually authoritative for "is this still genuinely awaiting
    approval right now" -- PipelineState.transition() updates it the moment a plan is
    approved, cancelled, or finishes executing. Only show a plan that RunStatus still
    says is pending."""
    plan_set = load_plan()
    if not plan_set or not plan_set.plans:
        return None

    status = load_status()
    if not status or status.state != "awaiting_approval" or not status.pending_plan_id:
        return None

    live_plans = [p for p in plan_set.plans if p.get("id") == status.pending_plan_id]
    if not live_plans:
        return None

    plans = [
        {
            "id": p.get("id"),
            "priority": p.get("priority"),
            "title": p.get("title"),
            "step_count": len(p.get("steps", [])),
        }
        for p in live_plans
    ]
    return {"planned_at": plan_set.planned_at, "plans": plans}


def summarize_system_and_backups() -> dict:
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_SYSTEM_AND_BACKUPS:
        return {"system": {}, "backups": {}, "available": False}
    return {"system": monitor.system, "backups": monitor.backups, "available": True}


def summarize_certs() -> dict:
    """check_certs() already encodes its own not-available states inline (a dead
    acme.json returns [{"error": ...}], an empty one [{"note": ...}]) -- this only
    gates on scan mode, same as summarize_system_and_backups(), since certs are only
    collected on a full scan."""
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_SYSTEM_AND_BACKUPS:
        return {"list": [], "available": False}
    return {"list": monitor.certs, "available": True}


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
        "certs": summarize_certs(),
    }
