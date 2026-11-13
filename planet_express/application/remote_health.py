"""The other Docker hosts' health, from Core's SSH reading (docs/designs/multi-host.md, release B).

Pure over `state/hosts.json`: no SSH here. Leela puts the answer in its snapshot as `other_hosts`; incidents and the Telegram report
read it from there. It is never given to Hermes or the planner, by construction (Hermes's input is built from named fields only):
a problem on another host must never become a plan for this one. PE does not change other hosts (that is level 3).

The rules are Leela's local ones, said in Docker's own state words:
  * restarting, or dead: a problem; created and never started: a problem;
  * exited with a non-zero code: not running; exited 0 is a finished one-shot, not a problem;
  * healthcheck unhealthy: a problem; healthcheck still starting past STARTING_GRACE_S of uptime: a problem;
  * a restart count at or above the crash-loop threshold: crash-looping (HIGH);
  * a host with no reading for 15 minutes (the Hosts page's "unknown"): the host itself is the problem, and its containers are
    not judged. A reading a few minutes old (one missed read) is still judged on its last values, and raises no host alert.
"""

from __future__ import annotations

import time
from datetime import datetime

CRASH_LOOP_RESTARTS = 3          # casa_leela.CRASH_LOOP_RESTART_THRESHOLD; a test pins the two together
# Leela's local default (DEFAULT_STARTING_GRACE_SECONDS) reads each container's own start period; the remote read does not carry
# it, so the remote rule waits longer before calling a starting healthcheck stuck.
STARTING_GRACE_S = 300


def _uptime(started, now: float) -> float | None:
    if not isinstance(started, str) or not started or started.startswith("0001-"):
        return None
    try:
        return now - datetime.fromisoformat(started.replace("Z", "+00:00")[:32]).timestamp()
    except ValueError:
        return None


def container_issue(c: dict, now: float | None = None) -> tuple[str | None, bool]:
    """(issue words or None, crash_looping) for one container record from hosts.json."""
    state = c.get("state")
    restarts = c.get("restarts") if isinstance(c.get("restarts"), int) and not isinstance(c.get("restarts"), bool) else 0
    crash = restarts >= CRASH_LOOP_RESTARTS and state in ("running", "restarting")
    issue = None
    if state == "restarting":
        issue = "restarting"
    elif state == "dead":
        issue = "dead"
    elif state == "created":
        issue = "created, never started"
    elif state == "exited":
        code = c.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            issue = f"not running (exited {code})"
    elif c.get("health") == "unhealthy":
        issue = "healthcheck failing"
    elif c.get("health") == "starting" and now is not None:
        up = _uptime(c.get("started"), now)
        if up is not None and up > STARTING_GRACE_S:
            issue = f"healthcheck still starting after {int(up)}s"
    if crash:
        issue = f"crash-looping (restarted {restarts}x)" + (f", {issue}" if issue and issue != "restarting" else "")
    return issue, crash


def assess(inventory, hosts_file, *, now: float | None = None) -> list[dict]:
    """One entry per host with an `ssh:` alias: {"host", "ssh", "link", "reading": bool, "reason", "total", "problems": [...]}."""
    from planet_express.application.multi_host import _safe_card, load_hosts_file   # the Hosts page's own reading of the file

    now = time.time() if now is None else now
    records = load_hosts_file(hosts_file)
    out = []
    for entry in inventory.hosts:
        if not entry.ssh:
            continue
        record = records.get(entry.ssh)
        card = _safe_card(entry, record, now)
        # Current or stale (under 15 minutes) is a reading: one missed SSH read must not alert (review, release B).
        # A metrics-only host (`docker: false`) is reading when its metrics are; it has no containers to judge.
        reading = card.liveness.state in ("current", "stale") and (
            not entry.docker or isinstance((record or {}).get("containers"), list))
        problems = []
        if reading and entry.docker:
            for c in record["containers"]:
                if not isinstance(c, dict) or not isinstance(c.get("name"), str):
                    continue
                issue, crash = container_issue(c, now)
                if issue:
                    problems.append({"name": c["name"], "image": c.get("image") if isinstance(c.get("image"), str) else "",
                                     "state": c.get("state") if isinstance(c.get("state"), str) else "",
                                     "health": c.get("health") if isinstance(c.get("health"), str) else "",
                                     "restart_count": c.get("restarts") if isinstance(c.get("restarts"), int) else 0,
                                     "issue": issue, "crash_looping": crash})
        out.append({"host": entry.name, "ssh": entry.ssh, "link": entry.link, "reading": reading,
                    "reason": None if reading else (card.liveness.reason or "no fresh reading"),
                    "total": len(record["containers"]) if reading and entry.docker else None,
                    "names": sorted(c["name"] for c in record["containers"] if isinstance(c, dict) and isinstance(c.get("name"), str))
                    if reading and entry.docker else [],
                    # A healthcheck still starting is not yet healthy: its incident must not close on it (Codex, release B).
                    "starting": sorted(c["name"] for c in record["containers"] if isinstance(c, dict) and isinstance(c.get("name"), str)
                                       and c.get("health") == "starting" and container_issue(c, now)[0] is None)
                    if reading and entry.docker else [],
                    "problems": problems})
    return out


def report_lines(other_hosts: list[dict]) -> list[str]:
    """Plain lines for Telegram: one per problem, one per host that is not reading. Empty when all is well."""
    lines = []
    for h in other_hosts or []:
        if not h.get("reading"):
            lines.append(f"{h['host']}: not reading ({h.get('reason') or 'no fresh reading'})")
            continue
        for p in h.get("problems", []):
            lines.append(f"{h['host']} · {p['name']}: {p['issue']}")
    return lines
