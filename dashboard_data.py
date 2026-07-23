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

import re
import socket
from datetime import datetime, timedelta, timezone
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


def _parse_mem_size(token: str) -> Optional[float]:
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


def _parse_uptime(line: str, scan_dt: Optional[datetime]) -> dict:
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
    return {"system": system, "backups": monitor.backups, "available": True}


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
        jobs = system_and_backups["backups"]
        failed = [name for name, b in jobs.items() if b.get("result") != "success"]
        if failed:
            backups = (
                f"{len(failed)} of {len(jobs)} backup job(s) didn't finish cleanly "
                f"({', '.join(failed)}). Not my finest hour, but at least I noticed."
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
