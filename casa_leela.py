"""
casa_leela.py — Leela: System Monitor
"I'm the only one around here with the training, the qualifications,
 and the hair to keep this ship in one piece."

Collects raw system state as structured JSON. No analysis, no prose.
Runs fast, batches all checks in one pass, returns clean JSON for Hermes.

Usage:
    python casa_leela.py              # prints JSON to stdout
    python casa_leela.py --updates    # image update check only
    python casa_leela.py --status     # quick disk + container health only
"""

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
import casa_stackctl as stackctl

log = logging.getLogger("planetexpress.leela")


# ── Shell helper ──────────────────────────────────────────────────────────────
def _run(cmd: str | list, timeout: int = 30) -> tuple[int, str, str]:
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"command timed out after {timeout}s"
    except FileNotFoundError as e:
        return 1, "", str(e)


# ── Check functions ───────────────────────────────────────────────────────────
# A container that has auto-restarted this many times (Docker's own RestartCount,
# tracked under a restart policy) is crash-looping regardless of its current status.
CRASH_LOOP_RESTART_THRESHOLD = 3
# A container currently "Up" but younger than this AND with at least one restart is
# also treated as crash-looping — catches loops on containers with no restart policy,
# where something external (Bender, a human, docker compose) keeps re-starting it.
CRASH_LOOP_MIN_UPTIME_SECONDS = 60


def _inspect_restart_info(names: list[str]) -> dict[str, dict]:
    """Batch docker inspect for RestartCount + current uptime, keyed by container name.
    One inspect call for every container is far cheaper than one call per container."""
    if not names:
        return {}
    fmt = (
        "{{.Name}}\t{{.RestartCount}}\t{{.State.Status}}\t"
        "{{.State.StartedAt}}"
    )
    _, out, err = _run(["docker", "inspect", "--format", fmt, *names], timeout=30)
    if not out:
        log.warning(f"docker inspect returned nothing: {err}")
        return {}

    info = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        name, restart_count_str, state, started_at = parts
        name = name.lstrip("/")
        uptime_seconds = None
        if state == "running" and started_at and not started_at.startswith("0001-01-01"):
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                uptime_seconds = (datetime.now(timezone.utc) - started).total_seconds()
            except ValueError:
                pass
        info[name] = {
            "restart_count": int(restart_count_str) if restart_count_str.isdigit() else 0,
            "uptime_seconds": uptime_seconds,
        }
    return info


def check_containers() -> list[dict]:
    """All containers — status, health, image. Flag anything not running or crash-looping."""
    fmt = (
        '{"name":"{{.Names}}",'
        '"status":"{{.Status}}",'
        '"image":"{{.Image}}"}'
    )
    _, out, err = _run(f"docker ps -a --format {shlex.quote(fmt)}")
    if not out:
        log.warning(f"docker ps returned nothing: {err}")
        return []

    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
            # Parse health from status string — .Health is not a valid --format
            # field in all Docker versions. Status already contains it, e.g.:
            # "Up 2 weeks (healthy)", "Up 3 days (unhealthy)", "Exited (1) ..."
            status = c["status"]
            if "(unhealthy)" in status:
                health = "unhealthy"
            elif "(health: starting)" in status or "(starting)" in status:
                health = "starting"
            elif "(healthy)" in status:
                health = "healthy"
            else:
                health = ""
            c["health"] = health
            containers.append(c)
        except json.JSONDecodeError:
            log.warning(f"Skipping unparseable container line: {line!r}")

    restart_info = _inspect_restart_info([c["name"] for c in containers])
    for c in containers:
        info = restart_info.get(c["name"], {})
        restart_count = info.get("restart_count", 0)
        uptime_seconds = info.get("uptime_seconds")
        status = c["status"]
        health = c["health"]
        c["restart_count"] = restart_count

        crash_looping = restart_count >= CRASH_LOOP_RESTART_THRESHOLD or (
            status.startswith("Up")
            and restart_count >= 1
            and uptime_seconds is not None
            and uptime_seconds < CRASH_LOOP_MIN_UPTIME_SECONDS
        )
        if crash_looping:
            c["crash_looping"] = True

        issue = None
        if not status.startswith("Up") and c["name"] not in config.PAUSED_CONTAINERS:
            issue = f"not running ({status})"
        elif health == "unhealthy":
            issue = "healthcheck failing"
        elif health == "starting":
            issue = "healthcheck still initialising"
        if crash_looping:
            issue = f"crash-looping (restarted {restart_count}x)" + (
                f", {issue}" if issue else ""
            )
        if issue:
            c["issue"] = issue

    return containers


def _previous_stack_completeness() -> dict[str, dict]:
    """Load the last saved snapshot's stack_completeness, keyed by stack name, so this run
    can tell 'was fine last time, now missing' (an actual incident) apart from 'has never
    had containers' (e.g. a stack that's defined but deliberately never started — reported
    once, then not re-alarmed on every cycle). Returns {} if there's no history yet."""
    if not config.STATE_MONITOR.exists():
        return {}
    try:
        prev = json.loads(config.STATE_MONITOR.read_text())
        return {s["stack"]: s for s in prev.get("stack_completeness", [])}
    except Exception as e:
        log.warning(f"Could not load previous snapshot for stack-completeness comparison: {e}")
        return {}


def check_stack_completeness() -> list[dict]:
    """For every active (non-forbidden) stack, compare how many services its compose file
    defines against how many actually have a container — running OR stopped — right now.

    A stack that should have containers but has ZERO is the signature of the whole stack
    having been torn down (e.g. an interrupted `docker compose down`, or images pruned out
    from under containers that were already gone) — a distinct and more severe failure than
    any single container being unhealthy, and one that "is everything currently running
    healthy" can never catch, because there's nothing there to BE unhealthy. This exact gap
    let a fully-missing stack go unnoticed on 2026-07-03.

    Severity depends on history, not just the current count: a stack that WAS complete last
    run and isn't now is an active incident (CRITICAL/HIGH). A stack that was ALREADY
    incomplete last run too (e.g. pinepods, defined but deliberately never started) is
    downgraded to LOW — still reported, so it isn't lost, but not re-screamed every cycle.
    First-ever run (no history) is treated as unknown and conservatively flagged urgent."""
    previous = _previous_stack_completeness()
    results = []
    for stack_dir in config.active_stack_dirs():
        stack_name = stack_dir.name
        compose_file = stack_dir / "docker-compose.yml"
        _, services_out, err = _run(f"docker compose -f {compose_file} config --services")
        expected_services = [s for s in services_out.splitlines() if s.strip()]
        if not expected_services:
            log.warning(f"Could not determine services for stack {stack_name}: {err}")
            continue

        _, ps_out, _ = _run(
            f"docker compose -f {compose_file} ps -a --format {shlex.quote('{{.Service}}')}"
        )
        present_services = {s for s in ps_out.splitlines() if s.strip()}
        missing = [s for s in expected_services if s not in present_services]

        entry = {
            "stack": stack_name,
            "expected_count": len(expected_services),
            "present_count": len(expected_services) - len(missing),
            "missing_services": missing,
        }
        if missing:
            prev_entry = previous.get(stack_name)
            was_already_incomplete = prev_entry is not None and prev_entry.get("missing_services")
            if was_already_incomplete:
                entry["alert"] = "LOW"
            else:
                entry["alert"] = "CRITICAL" if len(missing) == len(expected_services) else "HIGH"
        results.append(entry)
    return results


def check_disk() -> list[dict]:
    """Disk usage for all relevant mounts. Alert at 80/90%.

    Includes the root filesystem (/, /dev/sdb2) — this is where /var/lib/docker
    lives, and it's what actually fills up from image/container/log growth.
    Previously unmonitored: nothing here watched root until 2026-07-03."""
    _, out, _ = _run("df -h --output=source,target,pcent")
    patterns = [
        "casamedia", "immich", "erugo", "urphoto",
        "/dev/sda", "/dev/sdc", "/dev/sdb2", "/home",
        "casafast", "casabu",
    ]
    disks = []
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 3:
            continue
        source, target, pcent_str = parts[0], parts[1], parts[2]
        # Root filesystem: match target "/" exactly (substring match would also
        # catch "/home", "/boot/efi", etc. which are handled by their own patterns).
        is_root = target == "/"
        if not is_root and not any(p in source or p in target for p in patterns):
            continue
        try:
            pct = int(pcent_str.rstrip("%"))
        except ValueError:
            continue
        entry: dict = {"mount": target, "source": source, "used_pct": pct}
        if pct >= 90:
            entry["alert"] = "CRITICAL"
        elif pct >= 80:
            entry["alert"] = "HIGH"
        disks.append(entry)
    return disks


def check_docker_disk() -> dict:
    """docker system df — how much space images/containers/volumes/build cache are
    using and how much is reclaimable. Feeds Farnsworth's safe-prune decision; Leela
    only reports facts, it does not decide whether pruning is safe."""
    fmt = "{{.Type}}\t{{.TotalCount}}\t{{.Active}}\t{{.Size}}\t{{.Reclaimable}}"
    _, out, err = _run(f"docker system df --format {shlex.quote(fmt)}")
    if not out:
        log.warning(f"docker system df returned nothing: {err}")
        return {"rows": []}
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        rows.append({
            "type": parts[0], "total_count": parts[1], "active": parts[2],
            "size": parts[3], "reclaimable": parts[4],
        })
    return {"rows": rows}


def check_mounts() -> dict:
    """Verify all expected SMB mounts are actually reachable.

    Delegates to casa_stackctl.check_mounts() (config.MOUNT_UNITS is the single source of
    truth for the unit->path list) rather than keeping a second implementation here. This
    used to check `systemctl list-units --state=active` instead of real reachability --
    but a persistent CIFS mount (casamedia) can stay "active (mounted)" in systemd's eyes
    even after its session goes stale (e.g. surviving an Unraid reboot), which silently hid
    exactly that failure from this monitor. Testing real listability (with retry, since a
    NAS recovery window can transiently fail a single attempt) catches it instead.
    casamediafast2tb (4K movies) was decommissioned 2026-07-07 and isn't in the shared list.
    """
    results = stackctl.check_mounts()
    missing = [path for _, path, ok in results if not ok]
    return {
        "active_count": len(results) - len(missing),
        "missing": missing,
    }


def check_system() -> dict:
    """RAM, uptime, and recent journal errors."""
    _, mem_out, _ = _run("free -h")
    _, uptime_out, _ = _run("uptime")
    _, journal_out, _ = _run(
        "journalctl -p err -S '1 hour ago' --no-pager -q --output=short"
    )
    errors = [l for l in journal_out.splitlines() if l.strip()]
    mem_lines = mem_out.splitlines()
    return {
        "memory_summary": mem_lines[1] if len(mem_lines) > 1 else mem_out,
        "uptime": uptime_out,
        "recent_errors": errors[:25],
        "recent_error_count": len(errors),
    }


def check_backups() -> dict:
    """Borg backup service status and last run."""
    result = {}
    for unit in ["daily-borg-backup", "weekly-borg-backup"]:
        _, out, _ = _run(
            f"systemctl show {unit}.service "
            "--property=ActiveState,Result,ExecMainStatus,InactiveExitTimestamp"
        )
        props = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        key = "daily" if "daily" in unit else "weekly"
        result[key] = {
            "state": props.get("ActiveState", "unknown"),
            "result": props.get("Result", "unknown"),
            "exit_code": props.get("ExecMainStatus", "?"),
            "last_run": props.get("InactiveExitTimestamp", "n/a"),
        }
    return result


def check_services() -> dict:
    """Status of critical systemd services (startup).
    Note: nebula.service and dnclient.service are both intentionally decommissioned —
    remote access is now via Tailscale on OPNsense (outside this host, not monitored here).
    dnclient retired 2026-07-04, see project_casaserver_reip_plan memory for context."""
    units = ["casa-startup"]
    status = {}
    for unit in units:
        _, out, _ = _run(f"systemctl is-active {unit}")
        status[unit] = out.strip() or "unknown"
    return status


def check_images() -> list[dict]:
    """
    List :latest images older than 30 days as update candidates.
    Actual digest comparison requires registry API calls — flagged for Hermes.
    """
    fmt = (
        '{"repo":"{{.Repository}}",'
        '"tag":"{{.Tag}}",'
        '"created":"{{.CreatedSince}}",'
        '"id":"{{.ID}}"}'
    )
    _, out, _ = _run(f"docker images --format {shlex.quote(fmt)}")
    candidates = []
    stale_pattern = re.compile(r"(\d+)\s+(months?|weeks?)", re.IGNORECASE)
    for line in out.splitlines():
        try:
            img = json.loads(line.strip())
            if img["tag"] not in ("latest", ""):
                continue
            m = stale_pattern.search(img["created"])
            if m:
                n, unit = int(m.group(1)), m.group(2).lower()
                days = n * (30 if "month" in unit else 7)
                if days >= 30:
                    img["stale_days"] = days
                    candidates.append(img)
        except (json.JSONDecodeError, KeyError):
            continue
    return candidates


def check_certs() -> list[dict]:
    """Read Traefik ACME JSON for certificate domain list (expiry via openssl if needed).

    acme.json is confirmed-dead legacy data (2026-07-03): the entire certificatesResolvers
    block in ~/apps/network/proxy/traefik.yml is commented out, so nothing reads or writes
    this file — a permission-denied here is not a real problem, just noise. Suppressed at
    the source rather than relying on Hermes to correctly downgrade it every single cycle
    (it kept generating a LOW finding — and once, a whole diagnostic plan — every run)."""
    acme_path = Path("/home/casaroot/apps/network/proxy/letsencrypt/acme.json")
    if not acme_path.exists():
        return [{"error": "acme.json not found — Traefik may not have issued certs yet"}]
    if not os.access(acme_path, os.R_OK):
        return []  # known-dead legacy file, root-only 0600, no active resolver reads it
    try:
        data = json.loads(acme_path.read_text())
        certs = []
        for resolver, resolver_data in data.items():
            for cert in resolver_data.get("Certificates", []):
                domain = cert.get("domain", {}).get("main", "unknown")
                sans = cert.get("domain", {}).get("sans", [])
                certs.append({"domain": domain, "sans": sans, "resolver": resolver})
        return certs if certs else [{"note": "acme.json parsed but no certs found"}]
    except Exception as e:
        return [{"error": str(e)}]


# ── Entry points ──────────────────────────────────────────────────────────────
def run_full() -> dict:
    """Captain's full scan — all checks, returns complete snapshot."""
    log.info("Leela starting full system scan...")
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "casa_leela",
        "mode": "full",
        "containers": check_containers(),
        "stack_completeness": check_stack_completeness(),
        "disk": check_disk(),
        "docker_disk": check_docker_disk(),
        "mounts": check_mounts(),
        "system": check_system(),
        "backups": check_backups(),
        "services": check_services(),
        "image_candidates": check_images(),
        "certs": check_certs(),
    }
    n_issues = sum(1 for c in snapshot["containers"] if c.get("issue"))
    n_crash  = sum(1 for c in snapshot["containers"] if c.get("crash_looping"))
    n_disk   = sum(1 for d in snapshot["disk"] if d.get("alert"))
    n_missing_stacks = sum(1 for s in snapshot["stack_completeness"] if s.get("alert"))
    log.info(
        f"Leela scan complete — "
        f"{n_issues} container issue(s) ({n_crash} crash-looping), {n_disk} disk alert(s), "
        f"{n_missing_stacks} stack(s) with missing containers, "
        f"{len(snapshot['mounts']['missing'])} missing mount(s)"
    )
    return snapshot


def run_status() -> dict:
    """Quick status — containers + disk only. For /status command."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "casa_leela",
        "mode": "status",
        "containers": check_containers(),
        "disk": check_disk(),
        "services": check_services(),
    }


def run_updates() -> dict:
    """Image update candidates only. For /updates command."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "casa_leela",
        "mode": "updates",
        "image_candidates": check_images(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    parser = argparse.ArgumentParser(description="Leela — CasaMediaServer monitor")
    parser.add_argument("--status",  action="store_true", help="Quick status only")
    parser.add_argument("--updates", action="store_true", help="Image update check only")
    args = parser.parse_args()

    if args.status:
        result = run_status()
    elif args.updates:
        result = run_updates()
    else:
        result = run_full()

    print(json.dumps(result, indent=2))
