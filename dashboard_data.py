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

import logging
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

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

log = logging.getLogger("planetexpress.dashboard_data")
_permission_warnings = set()
_permission_warning_lock = threading.Lock()


def _load(path, model_cls):
    try:
        text = path.read_text()
    except PermissionError:
        with _permission_warning_lock:
            if path not in _permission_warnings:
                _permission_warnings.add(path)
                log.warning("Cannot read dashboard state: %s (permission denied)", path)
        return None
    except OSError:
        return None
    try:
        return model_cls.model_validate_json(text)
    except (ValidationError, ValueError):
        return None


def load_monitor() -> MonitorSnapshot | None:
    return _load(config.STATE_MONITOR, MonitorSnapshot)


def load_findings() -> Findings | None:
    return _load(config.STATE_FINDINGS, Findings)


def load_plan() -> PlanSet | None:
    return _load(config.STATE_PLAN, PlanSet)


def load_status() -> RunStatus | None:
    return _load(config.STATE_STATUS, RunStatus)


def load_rollback_candidates() -> RollbackCandidates | None:
    return _load(config.ROLLBACK_CANDIDATES_FILE, RollbackCandidates)


def load_update_history() -> UpdateHistory | None:
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
        return {
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0}, "list": [],
            "top": [], "medium_list": [], "low_list": [], "analyzed_at": None,
        }

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings.findings:
        sev = str(f.get("severity", "")).lower()
        if sev in counts:
            counts[sev] += 1

    ranked = sorted(findings.findings, key=_severity_rank)
    # Split for the template: critical/high (and anything with an unrecognized severity,
    # so it's never silently hidden) always render in full. Medium/low get their own
    # collapsed <details> -- a healthy fleet can easily rack up 20+ routine low findings
    # that would otherwise force deep scrolling past the stuff that actually matters.
    medium_list = [f for f in ranked if str(f.get("severity", "")).lower() == "medium"]
    low_list = [f for f in ranked if str(f.get("severity", "")).lower() == "low"]
    top = [f for f in ranked if str(f.get("severity", "")).lower() not in ("medium", "low")]
    return {
        "counts": counts, "list": ranked, "top": top,
        "medium_list": medium_list, "low_list": low_list,
        "analyzed_at": findings.analyzed_at,
    }


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


def _container_state(c: dict) -> str:
    """Four-way bucket for the dashboard's fleet matrix -- crash-looping is a real
    outage (down), a deliberately-stopped configured container is "paused" (not
    "online" -- an independent Codex review caught the earlier version falling
    through to online since it also has no "issue"), a running container with a
    failing/starting healthcheck is degraded-but-alive, anything else is fully
    healthy."""
    if c.get("crash_looping"):
        return "down"
    if c.get("name") in config.PAUSED_CONTAINERS and not str(c.get("status", "")).startswith("Up"):
        return "paused"
    if not str(c.get("status", "")).startswith("Up"):
        return "down"
    if c.get("issue"):
        return "degraded"
    return "online"


def summarize_containers() -> dict:
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_CONTAINERS:
        return {
            "total": 0, "healthy": 0, "issues": [], "available": False,
            "online": 0, "degraded": 0, "down": 0, "paused": 0, "cells": [],
        }
    issues = [c for c in monitor.containers if c.get("issue")]
    states = [_container_state(c) for c in monitor.containers]
    return {
        "total": len(monitor.containers),
        "healthy": len(monitor.containers) - len(issues),
        "issues": issues,
        "available": True,
        "online": states.count("online"),
        "degraded": states.count("degraded"),
        "down": states.count("down"),
        "paused": states.count("paused"),
        "cells": states,
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


def summarize_pending_plan() -> dict | None:
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
            "fix_steps": [s.get("description", "") for s in p.get("steps", []) if s.get("description")],
            "rollback_steps": [s.get("description", "") for s in p.get("rollback", []) if s.get("description")],
        }
        for p in live_plans
    ]
    return {"planned_at": plan_set.planned_at, "plans": plans}


_MEM_SIZE_RE = re.compile(r"^([\d.]+)([KMGT]?)i?B?$", re.IGNORECASE)
_MEM_FIELDS = ["total", "used", "free", "shared", "buff_cache", "available"]
# procps `uptime`'s middle segment is either "N day(s), HH:MM" or "N day(s), M min"
# (no HH:MM when uphours==0) -- both shapes seen in the wild, both handled here.
_UPTIME_RE = re.compile(
    r"^(?P<now>\d{1,2}:\d{2}:\d{2})\s+up\s+"
    r"(?:(?P<days>\d+)\s+days?,\s*)?"
    r"(?:(?P<hh>\d+):(?P<mm>\d+)|(?P<minonly>\d+)\s*min)\s*,\s*"
    r"(?P<users>\d+)\s+users?,\s*"
    r"load average:\s*(?P<load1>[\d.]+),\s*(?P<load5>[\d.]+),\s*(?P<load15>[\d.]+)"
)
# journalctl --output=short: "Jul 23 16:05:28 casamediaserver sudo[785667]: message"
_ERROR_LINE_RE = re.compile(r"^(\S+\s+\S+\s+\S+)\s+(\S+)\s+(.+)$")


def _parse_mem_size(token: str) -> float | None:
    """'15Gi' / '556Mi' (free -h's binary-unit output) -> bytes."""
    m = _MEM_SIZE_RE.match(token.strip())
    if not m:
        return None
    value, unit = m.groups()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return float(value) * mult[unit.upper()]


def _parse_memory_summary(line: str) -> dict:
    """'Mem: 15Gi 10Gi 556Mi 662Mi 5.5Gi 5.1Gi' -> labeled fields + used_pct/
    buff_cache_pct for the stacked memory bar."""
    fields = dict(zip(_MEM_FIELDS, line.split()[1:]))
    total = _parse_mem_size(fields.get("total", ""))
    used = _parse_mem_size(fields.get("used", ""))
    buff_cache = _parse_mem_size(fields.get("buff_cache", ""))
    fields["used_pct"] = round(used / total * 100) if total and used is not None else None
    if total and buff_cache is not None and used is not None:
        # Modern free derives "used" from total - available, not total - free -
        # buff/cache, so used and buff/cache commonly overlap and can sum past
        # 100%. Cap the reclaimable segment to what's actually left of the bar
        # so the two stay mutually exclusive.
        displayable_buff_cache = max(0.0, min(buff_cache, total - used))
        fields["buff_cache_pct"] = round(displayable_buff_cache / total * 100)
    else:
        fields["buff_cache_pct"] = None
    return fields


def _parse_uptime(line: str, scan_dt: datetime | None) -> dict:
    """'16:09:30 up 20 days, 23:43, 5 users, load average: 1.74, 1.31, 1.12' -> the
    stat-tile fields (up_human, since, load*, users, now). scan_dt (the monitor
    snapshot's own timestamp, not wall-clock now()) anchors "since" so a dashboard
    viewed hours after the scan still shows the boot time as of the scan, not a
    drifted one."""
    m = _UPTIME_RE.match(line.strip())
    if not m:
        return {}
    g = m.groupdict()
    days = int(g["days"] or 0)
    if g["hh"] is not None:
        hours, minutes = int(g["hh"]), int(g["mm"])
    else:
        hours, minutes = 0, int(g["minonly"] or 0)

    if days:
        up_human = f"{days}d {hours}h {minutes}m"
    elif hours:
        up_human = f"{hours}h {minutes}m"
    else:
        up_human = f"{minutes}m"

    since = None
    if scan_dt is not None:
        boot_dt = scan_dt.astimezone() - timedelta(days=days, hours=hours, minutes=minutes)
        since = boot_dt.strftime("%b %d · %H:%M")

    return {
        "up_human": up_human,
        "now": g["now"],
        "since": since,
        "users": g["users"],
        "load1": g["load1"],
        "load5": g["load5"],
        "load15": g["load15"],
    }


def _parse_error_line(line: str) -> dict:
    """journalctl short line -> {ts, host, proc, msg} for the colorized terminal-log
    block. Falls back to the raw line as msg if it doesn't match the expected shape
    (e.g. a wrapped or non-syslog-formatted journal entry)."""
    m = _ERROR_LINE_RE.match(line)
    if not m:
        return {"ts": "", "host": "", "proc": "", "msg": line}
    ts, host, rest = m.groups()
    proc, sep, msg = rest.partition(":")
    return {"ts": ts, "host": host, "proc": f"{proc}:" if sep else proc, "msg": msg.strip()}


def _diagnose_errors(errors: list[str], total: int | None = None) -> str:
    """Best-effort plain-language summary of recent_errors. Heuristic, not exhaustive
    -- an unrecognized error class falls back to a true but generic count rather than
    a guessed diagnosis. `errors` is capped at 25 by Leela (recent_errors); `total` is
    the real, uncapped recent_error_count -- pass it explicitly so a busy hour with
    more than 25 errors doesn't get diagnosed as having only 25."""
    if not errors:
        return ""
    n = total if total is not None else len(errors)
    plural = "s" if n != 1 else ""
    joined = "\n".join(errors)

    m = re.search(r"COMMAND=\S*systemctl\s+restart\s+(\S+)", joined)
    if m and ("pam_unix(sudo:auth)" in joined or "a password is required" in joined):
        return (
            f"{n} auth failure{plural} — a sudo restart of {m.group(1)} is prompting "
            "for a password it can't supply non-interactively."
        )
    if re.search(r"\boom[-_]?killer\b|out of memory", joined, re.IGNORECASE):
        return f"{n} out-of-memory event{plural} in the last hour."
    if "Failed with result" in joined or "failed to start" in joined.lower():
        return f"{n} service failure{plural} in the last hour."
    return f"{n} error{plural} in the last hour — see the log below."


def _parse_systemd_local_time(value: str | None) -> datetime | None:
    """Parse systemd's human-readable local-time timestamps (from `systemctl show`
    InactiveExitTimestamp / NextElapseUSecRealtime / LastTriggerUSec), e.g.
    "Sat 2026-07-25 03:10:09 WEST". These always render in the system's local timezone,
    and the abbreviation (WEST in summer, WET in winter here) isn't reliably parseable
    via strptime's %Z across platforms -- strip it and let time.mktime() interpret the
    naive value as local time (which is exactly what it is), then convert to a proper
    UTC-aware datetime for arithmetic against datetime.now(timezone.utc)."""
    if not value or value in ("n/a", "unknown", "never", "-"):
        return None
    parts = value.rsplit(" ", 1)
    stripped = parts[0] if len(parts) == 2 and parts[1].isalpha() else value
    try:
        struct = time.strptime(stripped, "%a %Y-%m-%d %H:%M:%S")
        return datetime.fromtimestamp(time.mktime(struct), tz=timezone.utc)
    except ValueError:
        return None


def _format_age_human(hours: float) -> str:
    hours = max(hours, 0)
    if hours < 1:
        return f"{max(round(hours * 60), 1)}m"
    days, rem_hours = divmod(int(hours), 24)
    return f"{days}d {rem_hours}h" if days else f"{rem_hours}h"


def _format_countdown_human(target: datetime, now: datetime) -> str:
    delta_minutes = int((target - now).total_seconds() // 60)
    if delta_minutes <= 0:
        return "overdue"
    days, rem_minutes = divmod(delta_minutes, 24 * 60)
    hrs, mins = divmod(rem_minutes, 60)
    if days:
        return f"in {days}d {hrs}h"
    if hrs:
        return f"in {hrs}h {mins}m"
    return f"in {mins}m"


def _format_backup_window(cadence_hours: int) -> str:
    if cadence_hours == 24:
        return "24h"
    if cadence_hours % 24 == 0:
        return f"{cadence_hours // 24}d"
    return f"{cadence_hours}h"


# Freshness tiers -- "worst job wins the tab verdict". A oneshot service's own
# ActiveState is always "inactive" between runs (see casa_hermes.py's system prompt),
# so it's deliberately not a factor here: freshness comes from age vs. cadence and the
# service's own Result, matching the design handoff's explicit "silent staleness is
# exactly how backups fail" rationale.
_FRESHNESS_STALE_MULT = 1.5
_FRESHNESS_OVERDUE_MULT = 3.0

# Mirrors casa_leela._BACKUP_CADENCE_HOURS -- only used as a fallback for snapshots taken
# before check_backups() started emitting cadence_hours itself, so a legacy weekly record
# doesn't get judged against the daily 24h window and read as immediately overdue.
_BACKUP_CADENCE_FALLBACK = {"daily": 24, "weekly": 168}


def _job_freshness(age_hours: float | None, cadence_hours: int, result: str, timer_armed: bool | None) -> str:
    """timer_armed is tri-state: True/False from a snapshot new enough to carry "next_run"
    at all, or None for a snapshot from before that field existed -- unknown legacy timer
    state degrades to "judge on age alone", not "assume disarmed", or every otherwise-healthy
    job in an existing install would read as a false DATA AGING warning until the next scan."""
    if result != "success":
        return "failed"
    if age_hours is None:
        return "stale"
    if age_hours >= cadence_hours * _FRESHNESS_OVERDUE_MULT:
        return "overdue"
    if age_hours >= cadence_hours * _FRESHNESS_STALE_MULT or timer_armed is False:
        return "stale"
    return "fresh"


_JOB_TIER_RANK = {"fresh": 0, "stale": 1, "overdue": 2, "failed": 2}
_CERT_TIER_RANK = {"valid": 0, "renew_soon": 1, "unknown": 1, "expiring": 2, "expired": 2, "error": 2}


def _backup_verdict(backups: dict, cert_list: list) -> dict:
    """Tab-level "is my data safe" verdict, structurally mirroring Hull Diagnostics'
    worst-tier alarm banner -- worst backup-job tier and worst live-cert tier both
    escalate the same verdict, since either one is "is my data safe" going wrong."""
    total = len(backups)
    if total == 0:
        return {
            "level": "unknown",
            "title": "BACKUP STATUS UNKNOWN",
            "detail": "No borg job data in this snapshot — this is a blind spot, not a clean bill of health.",
        }

    job_tiers = [b["freshness"] for b in backups.values()]
    worst_job = max(job_tiers, key=lambda t: _JOB_TIER_RANK.get(t, 0), default="fresh")
    # Rows with kind=="error" (unreadable/missing cert file) have no "status" but are
    # every bit as much a "data safety" problem as an expiring one -- treat them as the
    # worst tier rather than silently dropping them from the verdict calculation. Also
    # catches a snapshot written by the pre-redesign check_certs(), which represented the
    # same failure as a bare {"error": ...} with neither "kind" nor "status" set.
    cert_tiers = []
    for c in cert_list:
        if c.get("note"):
            continue  # "no certs declared on this host at all" placeholder, not a real cert
        if c.get("tier") or c.get("status"):
            cert_tiers.append(c.get("tier") or c["status"])
        elif c.get("kind") == "error" or c.get("error"):
            cert_tiers.append("error")
        else:
            # A snapshot written by the pre-redesign check_certs() has a healthy-looking
            # cert dict (domain/sans/expires) with no "status" at all -- the Certificate
            # Vault card already renders that as an explicit "NO DATA" badge rather than
            # implying it's valid (see the template's own comment on this), so the verdict
            # must match that honesty instead of silently defaulting to "valid" and
            # potentially hiding an already-expired legacy-shaped cert.
            cert_tiers.append("unknown")
    worst_cert = max(cert_tiers, key=lambda t: _CERT_TIER_RANK.get(t, 0), default="valid")

    fresh_count = sum(1 for t in job_tiers if t == "fresh")
    armed_count = sum(1 for b in backups.values() if b["timer_armed"])
    live_certs = [c for c in cert_list if c.get("tier") or c.get("status")]

    job_crit = worst_job in ("overdue", "failed")
    cert_crit = worst_cert in ("expiring", "expired", "error")
    if job_crit or cert_crit:
        level = "crit"
        title = "DATA AT RISK"
        if job_crit and cert_crit:
            detail = f"{fresh_count}/{total} borg job(s) healthy, and a certificate needs attention too. Check both the job and the Certificate Vault below."
        elif job_crit:
            detail = f"{fresh_count}/{total} borg job(s) healthy. Check the failing/overdue job below before trusting this backup set."
        else:
            detail = f"All {total} borg job(s) are healthy, but a certificate is expiring, expired, or unreadable. Check the Certificate Vault below."
    elif worst_job == "stale" or worst_cert in ("renew_soon", "unknown"):
        level = "warn"
        title = "DATA AGING"
        detail = f"{fresh_count}/{total} borg job(s) fresh, {armed_count}/{total} timer(s) armed. Nothing's failed outright, but a job or cert needs attention."
    else:
        level = "ok"
        title = "DATA IS SAFE"
        dated_jobs = [b for b in backups.values() if b.get("age_hours") is not None]
        newest = min(dated_jobs, key=lambda b: b["age_hours"])["age_human"] if dated_jobs else None
        clauses = [f"All {total} borg job(s) succeeded on schedule."]
        if newest:
            clauses.append(f"Newest snapshot {newest} ago.")
        known_days = [
            c.get("days_left", c.get("days_remaining"))
            for c in live_certs
            if c.get("days_left", c.get("days_remaining")) is not None
        ]
        if known_days:
            clauses.append(f"{len(live_certs)} cert(s) live, soonest expiry in {min(known_days)}d.")
        elif live_certs:
            clauses.append(f"{len(live_certs)} cert(s) live.")
        detail = " ".join(clauses)

    return {"level": level, "title": title, "detail": detail}


def summarize_system_and_backups() -> dict:
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_SYSTEM_AND_BACKUPS:
        return {"system": {}, "backups": {}, "available": False}
    system = dict(monitor.system)
    system["hostname"] = socket.gethostname()
    scan_dt = None
    if monitor.timestamp:
        try:
            scan_dt = datetime.fromisoformat(monitor.timestamp)
        except ValueError:
            scan_dt = None
    if system.get("memory_summary"):
        system["memory"] = _parse_memory_summary(system["memory_summary"])
    if system.get("uptime"):
        system["uptime_parsed"] = _parse_uptime(system["uptime"], scan_dt)
    if system.get("recent_errors"):
        system["parsed_errors"] = [_parse_error_line(e) for e in system["recent_errors"]]
        system["diagnosis"] = _diagnose_errors(system["recent_errors"], system.get("recent_error_count"))

    now = datetime.now(timezone.utc)
    backups = {}
    for name, b in monitor.backups.items():
        b = dict(b)
        # "last_run" (systemd's InactiveEnterTimestamp, see casa_leela.check_backups()) is
        # when the job last *finished* -- while a run is currently in progress
        # (state == "activating"), this still correctly holds the previous completed run's
        # time, since InactiveEnterTimestamp only updates on the *next* completion. No
        # special-casing needed here for that case.
        last_run_dt = _parse_systemd_local_time(b.get("last_run"))
        # "next_run" is absent entirely (not even "n/a") only for a snapshot from before
        # check_backups() started emitting it -- keep that as a real "unknown" distinct from
        # "we asked systemd and it reported nothing", or a legacy install would read every
        # otherwise-healthy job as a confirmed-disarmed timer until the next full scan.
        next_run_known = "next_run" in b
        next_run_dt = _parse_systemd_local_time(b.get("next_run"))
        cadence_hours = b.get("cadence_hours") or _BACKUP_CADENCE_FALLBACK.get(name, 24)
        b["cadence_hours"] = cadence_hours  # write the resolved fallback back for the template
        age_hours = (now - last_run_dt).total_seconds() / 3600 if last_run_dt else None
        b["age_hours"] = age_hours
        b["age_human"] = _format_age_human(age_hours) if age_hours is not None else "never"
        # Tri-state: True/False once a snapshot is new enough to carry "next_run" at all
        # (next_run's own absence of a value, e.g. "n/a", is a real "not armed" signal --
        # systemd's NextElapseUSecRealtime is always forward-looking *as of scan time*, so a
        # parsed value reading as "in the past" by render time just means the snapshot is a
        # few hours stale, not that the real timer disarmed -- presence alone is the right
        # signal there); None ("unknown") only for a legacy snapshot with no "next_run" key
        # at all -- never display or score that as a confirmed-disarmed timer.
        timer_armed = (next_run_dt is not None) if next_run_known else None
        b["timer_armed"] = timer_armed
        if next_run_dt:
            b["next_human"] = _format_countdown_human(next_run_dt, now)
        elif timer_armed is None:
            b["next_human"] = "unknown (pre-upgrade scan)"
        else:
            b["next_human"] = "timer not armed"
        b["window_pct"] = min(round(age_hours / cadence_hours * 100), 100) if age_hours is not None else 0
        window_label = _format_backup_window(cadence_hours)
        if age_hours is None:
            b["window_caption"] = "backup age unavailable"
        elif age_hours <= cadence_hours:
            b["window_caption"] = f"{b['window_pct']}% through the {window_label} window"
        else:
            b["window_caption"] = f"{_format_age_human(age_hours - cadence_hours)} past its {window_label} window"
        b["freshness"] = _job_freshness(age_hours, cadence_hours, b.get("result", "unknown"), timer_armed)
        backups[name] = b

    return {
        "system": system,
        "backups": backups,
        "available": True,
        "verdict": _backup_verdict(backups, monitor.certs),
    }


def summarize_certs() -> dict:
    """check_certs() already encodes its own not-available states inline (a dead
    acme.json returns [{"error": ...}], an empty one [{"note": ...}]) -- this only
    gates on scan mode, same as summarize_system_and_backups(), since certs are only
    collected on a full scan."""
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_SYSTEM_AND_BACKUPS:
        return {"list": [], "available": False}
    return {"list": monitor.certs, "available": True}


def build_professor_lines(ctx: dict) -> dict:
    """"Ship's Computer" sidebar copy -- one line per tab, plus overrides for the
    scanning and plan-approved states, all generated from the real ctx dict rather
    than the design mockup's hardcoded flavor text. Takes the fully-merged ctx
    (after casa_scruffy.py has added "traefik"/"adguard") since the Network tab's
    line needs live data build_dashboard_context() itself never fetches."""
    containers = ctx["containers"]
    findings = ctx["findings"]
    system_and_backups = ctx["system_and_backups"]
    traefik = ctx["traefik"]
    adguard = ctx["adguard"]
    health = ctx["health"]
    pipeline_status = ctx["pipeline_status"]

    scanning = pipeline_status["state"] == "running"
    last_scan_mode = health.get("last_scan_mode") or "?"

    # Overview
    if not containers["available"]:
        overview = (
            "I haven't the faintest idea how the fleet's doing — the last scan "
            f"(mode: {last_scan_mode}) didn't collect container data."
        )
    elif containers["down"]:
        overview = (
            f"Bad news, everyone. {containers['down']} of {containers['total']} "
            "containers are down. Check the Findings table before I have an aneurysm."
        )
    elif findings["counts"]["critical"] or findings["counts"]["high"]:
        n = findings["counts"]["critical"] + findings["counts"]["high"]
        overview = (
            f"{containers['healthy']} of {containers['total']} containers are up, "
            f"but {n} finding(s) need real attention. Don't make me say it twice."
        )
    elif sum(findings["counts"].values()):
        n = sum(findings["counts"].values())
        overview = (
            f"Good news, everyone! {containers['healthy']} of {containers['total']} "
            f"containers are alive and well. There's {n} nagging thing worth a look, mind you."
        )
    elif containers["degraded"]:
        # A container can be running with a failing/starting healthcheck (degraded)
        # before Hermes has ever analyzed it into a "finding" -- e.g. right after a
        # fresh scan. Findings-based branches above don't catch that case, so without
        # this the sidebar would call the fleet "alive and well" while a real issue
        # sits unreported.
        overview = (
            f"{containers['healthy']} of {containers['total']} containers are up, but "
            f"{containers['degraded']} {'is' if containers['degraded'] == 1 else 'are'} "
            "running with a shaky healthcheck. Not a crisis, but don't ignore it."
        )
    else:
        overview = (
            f"Good news, everyone! All {containers['total']} of {containers['total']} "
            "containers are alive and well — a personal best."
        )

    # Backups
    if not system_and_backups["available"]:
        backups = (
            f"Backups? Oh, my. The last scan ran in '{last_scan_mode}' mode, so "
            "I've collected precisely nothing. A full scan will fix that."
        )
    else:
        # freshness (computed in summarize_system_and_backups(), same worst-signal-wins
        # logic as the Backups tab's verdict banner) rather than raw result -- a job that
        # reported "success" but is stale/overdue/unarmed shouldn't get a clean bill of
        # health here while the tab itself is showing DATA AGING/AT RISK.
        jobs = system_and_backups["backups"]
        failed = [name for name, b in jobs.items() if b.get("freshness") == "failed"]
        aging = [name for name, b in jobs.items() if b.get("freshness") in ("stale", "overdue")]
        if failed:
            backups = (
                f"{len(failed)} of {len(jobs)} backup job(s) didn't finish cleanly "
                f"({', '.join(failed)}). Not my finest hour, but at least I noticed."
            )
        elif aging:
            backups = (
                f"{len(aging)} of {len(jobs)} backup job(s) succeeded but are running "
                f"behind schedule ({', '.join(aging)}). Not failed, but don't get comfortable."
            )
        elif jobs:
            backups = f"All {len(jobs)} backup job(s) reporting success. Borg's doing its job; I'm doing mine, which is worrying about it anyway."
        else:
            backups = "No backup jobs reported in this scan."

    # Network
    if traefik["available"]:
        not_enabled = [r for r in traefik["routers"] if r.get("status") != "enabled"]
        if not_enabled:
            network = (
                f"{len(not_enabled)} of {len(traefik['routers'])} router(s) aren't reporting "
                "enabled. Traefik's plumbing has sprung a leak somewhere."
            )
        else:
            network = (
                f"{len(traefik['routers'])} router(s), all reported enabled. Traefik is "
                "my second-favourite plumbing — right after the ship's coolant loop."
            )
    else:
        network = "Traefik's API isn't answering on :8079. Either it's down or having a moment — I can't tell which from here."
    if adguard.get("configured") and adguard.get("available"):
        network += (
            f" AdGuard's blocked {adguard.get('num_blocked_filtering', '?')} of "
            f"{adguard.get('num_dns_queries', '?')} queries, for what it's worth."
        )

    # Actions
    bot_handle = f"@{ctx['telegram_bot_username']}" if ctx.get("telegram_bot_username") else "your Telegram bot"
    actions = (
        "Everything's been updated and nothing exploded! For now I carry out fixes "
        f"over Telegram, {bot_handle}, while the good people bolt hands onto this dashboard."
    )

    lines = {"overview": overview, "backups": backups, "network": network, "actions": actions}

    if scanning:
        override = "Scanning the entire ship! Hold your hydrogen — this'll only take a moment, unless it takes several."
        lines = {k: override for k in lines}
    elif ctx["pending_plan"]:
        plan_id = pipeline_status.get("pending_plan_id") or "?"
        lines["overview"] = (
            f"Good news, everyone! Well — mostly. I've drawn up plan {plan_id} for "
            "the situation. Do have a look in the sidebar."
        )

    return lines


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
