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


def load_status() -> RunStatus | None:
    return _load(config.STATE_STATUS, RunStatus)


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
    stack_warning = any(s.get("alert") in ("LOW", "MEDIUM") for s in stacks)

    if not monitor and not findings:
        status = "unknown"
    elif has_critical or crash_looping_count > 0 or disk_critical or stack_critical_or_high:
        status = "critical"
    elif has_high or unhealthy_count > 0 or disk_high or stack_warning:
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
            "available": False,
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
        "analyzed_at": findings.analyzed_at, "available": True,
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


def _disk_level(used_pct) -> str:
    """The disk row of DATA-CONTRACT.md's status table: < 75% ok, 75-89 warn, 90-94 high,
    >= 95 crit, unreadable none. The first Overview pass carried the old bar's 60/80
    thresholds forward, which painted a 63%-full mount amber -- eleven mounts of routine
    amber is how a real 91% stops being noticed."""
    if not isinstance(used_pct, (int, float)):
        return "none"
    if used_pct >= 95:
        return "crit"
    if used_pct >= 90:
        return "high"
    if used_pct >= 75:
        return "warn"
    return "ok"


def summarize_disk() -> dict:
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_DISK:
        return {"list": [], "available": False}
    return {
        "list": sorted(
            (mount | {"level": _disk_level(mount.get("used_pct"))} for mount in monitor.disk),
            key=lambda mount: mount.get("used_pct") if isinstance(mount.get("used_pct"), (int, float)) else -1,
            reverse=True,
        ),
        "available": True,
    }


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
    """Nothing here: the open canary rollback windows live in the core's database, which the
    dashboard's user is denied (`scripts/web_access.py`). `casa_scruffy.index()` fetches them over
    the `canary.candidates` RPC, the same way it fetches Traefik and AdGuard (Codex, T42)."""
    return []


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
        return {"system": {}, "backups": {}, "available": False, "enabled_jobs": config.BACKUP_JOBS}
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
        "enabled_jobs": config.BACKUP_JOBS,
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


_OVERVIEW_LEVEL_RANK = {"none": -1, "ok": 0, "warn": 1, "high": 2, "crit": 3}
# Both tables are DATA-CONTRACT.md's status mapping, not a second opinion on it. "expiring"
# is crit there (the LED even pulses); calling it "high" on the Overview would have made the
# tile read less severe than the Backups tab it summarises.
_BACKUP_FRESHNESS_LEVEL = {"fresh": "ok", "stale": "warn", "overdue": "crit", "failed": "crit"}
_CERT_TIER_LEVEL = {"valid": "ok", "renew_soon": "warn", "expiring": "crit", "expired": "crit"}


def summarize_adguard(adguard: dict) -> dict:
    """One shaping of AdGuard's three raw counters, used by both the Network strip and the
    Overview tile. A fresh AdGuard reports zero queries, so the percentage is guarded here
    rather than in two templates that would each have to remember to."""
    if not adguard.get("available"):
        return {"available": False, "configured": bool(adguard.get("configured")),
                "queries": "—", "blocked": "—", "blocked_pct": "—",
                "allowed_pct": 0, "avg_ms": "—"}
    queries = adguard.get("num_dns_queries") or 0
    blocked = adguard.get("num_blocked_filtering") or 0
    blocked_pct = round(blocked / queries * 100, 1) if queries else 0
    avg = adguard.get("avg_processing_time")
    return {
        "available": True, "configured": True,
        "queries": f"{queries:,}", "blocked": f"{blocked:,}",
        "blocked_pct": blocked_pct, "allowed_pct": round(100 - blocked_pct, 1),
        "avg_ms": round(avg * 1000) if isinstance(avg, (int, float)) else "—",
    }


def _hull_hero(total_findings: int, pending: int, plans_known: bool) -> str:
    if total_findings:
        return f"{total_findings} FINDING{'' if total_findings == 1 else 'S'}"
    if pending:
        return f"{pending} PLAN{'' if pending == 1 else 'S'} WAITING"
    return "ALL NOMINAL" if plans_known else "NO FINDINGS"


def _cert_tier_level(cert: dict) -> str:
    """An error row from check_certs() carries kind="error" and no tier at all; it is a
    certificate we could not read, which is a crit, not a shrug."""
    if cert.get("kind") == "error" or cert.get("error"):
        return "crit"
    return _CERT_TIER_LEVEL.get(cert.get("tier") or cert.get("status"), "warn")


def _overview_worst(*levels: str) -> str:
    return max(levels, key=lambda level: _OVERVIEW_LEVEL_RANK.get(level, -1), default="none")


def summarize_overview_tiles(ctx: dict, routers: dict, adguard: dict) -> dict:
    """Collapse existing dashboard summaries into the five above-the-fold readouts.

    ``routers`` and ``adguard`` are explicit arguments because they are live route
    inputs.  This function only shapes values already collected by its caller.
    """
    mode = ctx.get("health", {}).get("last_scan_mode") or "?"
    tiles = {}

    containers = ctx.get("containers", {})
    services = ctx.get("services", {})
    if not containers.get("available"):
        tiles["fleet"] = {
            "level": "none", "hero": "—", "sub": f"mode {mode} did not collect containers",
            "note": "containers ›", "total": 0, "cells": [],
        }
    else:
        # Two things this tile must not do. It must not vanish after a "status" scan, which
        # collects containers but not stack completeness -- only the stack count in the corner
        # depends on services. And it must not go amber for PAUSED_CONTAINERS, which are
        # stopped on purpose and are not counted as unhealthy, or "85 of 85 healthy" renders
        # as a warning about itself.
        level = "crit" if containers.get("down") else "warn" if containers.get("degraded") else "ok"
        tiles["fleet"] = {
            "level": level, "hero": str(containers.get("healthy", 0)),
            "sub": f"/ {containers.get('total', 0)} healthy",
            "note": f"{services.get('total_stacks', 0)} stacks" if services.get("available") else "stacks —",
            "total": containers.get("total", 0), "cells": containers.get("cells", []),
        }

    findings = ctx.get("findings", {})
    # Approvals live in core's store, not in the run-status file: pending_plan_id was the
    # retired shell planner's signal and is now always null. The route supplies the real count
    # from proposal.list_pending and leaves the key absent when core could not be reached --
    # which makes the plan count unknown, never zero, and never takes the findings with it.
    pending = ctx.get("pending_approvals")
    plans_known = isinstance(pending, int)
    pending = pending if plans_known else 0
    if not findings.get("available"):
        # Findings and approvals come from different places. An approval genuinely waiting for
        # the operator is the most actionable thing this tile can carry, so a missing findings
        # snapshot must not swallow it -- the CRIT/HIGH pills go to "?" instead of pretending
        # to be zero.
        tiles["hull"] = {
            "level": "warn" if pending else "none",
            "hero": _hull_hero(0, pending, plans_known) if pending else "—",
            "sub": f"mode {mode} has no findings analysis",
            "note": "actions ›", "critical": "?", "high": "?",
            "plans": pending if plans_known else "?",
            "show_pills": bool(pending),
        }
    else:
        counts = findings.get("counts", {})
        # Not sum(counts.values()): summarize_findings() deliberately keeps a finding with an
        # unrecognised severity in "top" rather than hiding it, and it lands in none of the four
        # buckets. Counting only the buckets turned such a finding into ALL NOMINAL while Hull
        # Diagnostics was displaying it.
        counted = sum(counts.values())
        total_findings = max(len(findings.get("list", [])), counted)
        unknown_severity = total_findings - counted
        if counts.get("critical"):
            level = "crit"
        elif counts.get("high") or unknown_severity > 0:
            level = "high"
        elif counts.get("medium") or counts.get("low") or pending:
            level = "warn"
        else:
            level = "ok"
        tiles["hull"] = {
            "level": level,
            # "ALL NOMINAL" is a claim about the whole hull. With core unreachable we only know
            # the findings are clean, so say exactly that and let the PLANS pill show "?" --
            # overclaiming here is how an operator stops reading the tile. And with no findings
            # but approvals waiting, the thing that wants you is the approval, not a "0".
            "hero": _hull_hero(total_findings, pending, plans_known),
            "sub": f"{counts.get('critical', 0)} CRIT · {counts.get('high', 0)} HIGH · {pending} PLANS",
            "note": "actions ›", "critical": counts.get("critical", 0),
            "high": counts.get("high", 0), "plans": pending if plans_known else "?",
            "show_pills": True,
        }

    system_and_backups = ctx.get("system_and_backups", {})
    certs = ctx.get("certs", {})
    backups = system_and_backups.get("backups", {}) if system_and_backups.get("available") else {}
    # The hero is the newest snapshot, but the LEVEL is the worst job on the host. Reading
    # severity off one job is how a failed daily hides behind a fresh weekly -- the Backups
    # tab's own verdict already takes the worst, and the tile has to agree with it.
    newest = min(
        (job for job in backups.values() if isinstance(job, dict)),
        key=lambda job: job.get("age_hours") if isinstance(job.get("age_hours"), (int, float)) else float("inf"),
        default=None,
    )
    # Jobs and certificates are separate sensors and fail separately -- the same shape as
    # traefik/adguard above and uptime/memory below. Requiring both meant an unreadable cert
    # list could hide a failed backup, which is the one thing this tile exists to show.
    live_certs = [c for c in certs.get("list", []) if not c.get("note")] if certs.get("available") else []
    cert_levels = [_cert_tier_level(cert) for cert in live_certs]
    known_days = [
        c.get("days_left", c.get("days_remaining")) for c in live_certs
        if c.get("days_left", c.get("days_remaining")) is not None
    ]
    soonest = min(known_days) if known_days else None
    if newest is None:
        # No borg job observed at all. _backup_verdict() calls that unknown, and a valid
        # certificate must not promote a backup blind spot to green -- the certs are real
        # information, so they still render, but they are not evidence about backups.
        tiles["backups"] = {
            "level": "none", "hero": "—",
            "sub": f"mode {mode} did not collect backups",
            "note": "backups ›", "next": "—",
            "cert_count": len(live_certs) if certs.get("available") else "—",
            "soonest": f"{soonest}d" if soonest is not None else "—",
        }
    else:
        job_levels = [
            _BACKUP_FRESHNESS_LEVEL.get(job.get("freshness"), "warn")
            for job in backups.values() if isinstance(job, dict)
        ]
        level = _overview_worst(*job_levels, *cert_levels)
        # systemd stamps the completion time even when the run produced nothing, so for a
        # failed job age_human is "since we last tried", not "since we last had a backup".
        # The Backups tab already makes that distinction; the tile has to as well.
        tiles["backups"] = {
            "level": level, "hero": newest.get("age_human") or "—",
            "sub": "since last attempt" if newest.get("freshness") == "failed" else "since snapshot",
            "note": "backups ›", "next": newest.get("next_human") or "—",
            "cert_count": len(live_certs) if certs.get("available") else "—",
            "soonest": f"{soonest}d" if soonest is not None else "—",
        }

    # AdGuard is an optional component: plenty of installs never run it, and it can be down
    # while Traefik is fine. Its absence may grey out its own two numbers, never the router
    # count -- a disabled router must not be replaced by a neutral "unavailable" tile.
    router_list = routers.get("routers", []) if routers.get("available") else []
    # "0 of 0 routers up" is not a healthy fleet, it is no observation. Traefik answering with
    # an empty list is the Network tab's "No routers reported", not an all-clear.
    if not routers.get("available") or not router_list:
        tiles["network"] = {
            "level": "none", "hero": "—",
            "sub": "no routers reported" if routers.get("available") else "traefik api unreachable",
            "note": "network ›", "detail": "", "blocked_pct": "—", "avg_ms": "—",
        }
    else:
        routers_up = sum(router.get("status") == "enabled" for router in router_list)
        router_level = "ok" if routers_up == len(router_list) else "crit"
        stats = summarize_adguard(adguard)
        if stats["available"]:
            detail = f'adguard {stats["blocked_pct"]}% blocked · {stats["avg_ms"]}ms'
        else:
            detail = "adguard not configured" if not stats["configured"] else "adguard unavailable"
        tiles["network"] = {
            "level": router_level, "hero": str(routers_up), "sub": "routers up",
            "note": "network ›", "detail": detail,
            "blocked_pct": stats["blocked_pct"], "avg_ms": stats["avg_ms"],
        }

    if not system_and_backups.get("available"):
        tiles["system"] = {
            "level": "none", "hero": "—", "sub": f"mode {mode} did not collect system data",
            "note": "up —", "load5": "—", "load15": "—", "memory": {}, "show_memory": False,
        }
    else:
        # `uptime` and `free -h` are separately parsed outputs and fail separately. Gating the
        # tile's level on both meant an unparseable uptime line quietly hid a 94%-full memory
        # bar behind "no system metrics" -- the one number on this tile worth an alarm.
        system = system_and_backups.get("system", {})
        uptime = system.get("uptime_parsed", {})
        memory = system.get("memory", {})
        memory_pct = memory.get("used_pct")
        show_memory = isinstance(memory_pct, (int, float))
        if not show_memory:
            level = "none"
        elif memory_pct >= 90:
            level = "crit"
        elif memory_pct >= 80:
            level = "high"
        elif memory_pct >= 70:
            level = "warn"
        else:
            level = "ok"
        tiles["system"] = {
            "level": level, "hero": uptime.get("load1") or "—",
            "sub": "load" if uptime else "load unreadable",
            "note": f"up {uptime.get('up_human') or '—'}",
            "load5": uptime.get("load5") or "—", "load15": uptime.get("load15") or "—",
            "memory": memory, "show_memory": show_memory,
        }

    return tiles


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
    return lines


_SERVICE_LEVEL_RANK = {"crit": 0, "warn": 1, "idle": 2, "ok": 3}
_SERVICE_LEVEL_WORD = {"crit": "down", "warn": "degraded", "idle": "paused", "ok": ""}


def _service_component_level(status: str, state: str, *, multi: bool) -> str:
    """Collapse one observed container state into the Overview card vocabulary.

    A service can have several comma-joined container observations.  Explicit state
    suffixes let each component retain its own meaning even though Leela stores only
    the service's aggregate (worst) status alongside them.
    """
    normalized = state.strip().lower()
    if normalized.startswith("running"):
        # With a comma-joined observation, the aggregate status belongs to the
        # service, so use each component's explicit suffix to avoid painting a
        # healthy replica with its failed sibling's status.
        if multi:
            if "(healthy)" in normalized or normalized == "running":
                return "ok"
            if "(starting)" in normalized or "(unhealthy)" in normalized:
                return "warn"
        return "ok" if status == "healthy" else "warn"
    if status == "healthy":
        return "idle"
    if status == "unknown":
        return "warn"
    return "crit"


def _service_member(service, detail) -> dict | None:
    if not isinstance(detail, dict):
        return None
    service_name = str(service)
    status = str(detail.get("status", "unknown")).lower()
    state = str(detail.get("state", ""))
    components = state.split(",") if state else [""]
    levels = [
        _service_component_level(status, component, multi=len(components) > 1)
        for component in components
    ]
    level = min(levels, key=_SERVICE_LEVEL_RANK.get)
    word = "starting" if level == "warn" and status == "unknown" else _SERVICE_LEVEL_WORD[level]
    return {
        "service": service_name,
        "level": level,
        "word": word,
        "state": state,
    }


def summarize_services() -> dict:
    monitor = load_monitor()
    if not monitor or monitor.mode not in _MODES_WITH_STACK_COMPLETENESS:
        return {
            "available": False, "stacks": [], "total_stacks": 0,
            "up": 0, "total": 0, "attention": 0,
        }

    stacks = []
    for raw_stack in monitor.stack_completeness:
        if not isinstance(raw_stack, dict):
            continue
        name = str(raw_stack.get("stack", "?"))
        raw_services = raw_stack.get("services", {})
        services = raw_services if isinstance(raw_services, dict) else {}
        members = [
            member
            for service, detail in services.items()
            if (member := _service_member(service, detail)) is not None
        ]
        members.sort(key=lambda member: (
            _SERVICE_LEVEL_RANK[member["level"]], member["service"]
        ))

        if raw_stack.get("status") == "unknown" and not members:
            level = "warn"
            note = "state unreadable"
        else:
            level = min(
                (member["level"] for member in members),
                key=_SERVICE_LEVEL_RANK.get,
                default="ok",
            )
            note = " · ".join(
                f'{member["service"]} {member["word"]}'
                for member in members if member["level"] != "ok"
            )
        stacks.append({
            "name": name,
            "up": sum(member["level"] == "ok" for member in members),
            "total": len(members),
            "level": level,
            "note": note,
            "members": members,
        })

    stacks.sort(key=lambda stack: (
        _SERVICE_LEVEL_RANK[stack["level"]], -stack["total"], stack["name"]
    ))
    return {
        "available": True,
        "stacks": stacks,
        "total_stacks": len(stacks),
        "up": sum(stack["up"] for stack in stacks),
        "total": sum(stack["total"] for stack in stacks),
        "attention": sum(stack["level"] != "ok" for stack in stacks),
    }


def build_dashboard_context() -> dict:
    """Single entry point the Flask route calls."""
    ctx = {
        "health": summarize_health(),
        "findings": summarize_findings(),
        "pipeline_status": summarize_pipeline_status(),
        "containers": summarize_containers(),
        "stack_completeness": summarize_stack_completeness(),
        "services": summarize_services(),
        "disk": summarize_disk(),
        "update_history": summarize_update_history(),
        "rollback_candidates": summarize_rollback_candidates(),
        "system_and_backups": summarize_system_and_backups(),
        "certs": summarize_certs(),
    }
    # Live network values are intentionally unavailable here; the route replaces
    # these placeholders after its explicit Traefik/AdGuard fetches.
    ctx["overview_tiles"] = summarize_overview_tiles(
        ctx, {"available": False, "routers": []}, {"available": False}
    )
    return ctx
