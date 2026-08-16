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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

import casa_stackctl as stackctl
import config

log = logging.getLogger("planetexpress.leela")


# ── Shell helper ──────────────────────────────────────────────────────────────
def _run(cmd: str | list, timeout: int = 30) -> tuple[int, str, str]:
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
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
# Docker's own "starting" health state is expected to last through the container's
# configured start_period plus enough failing checks to hit its retry count (e.g.
# TubeArchivist's 30s start_period + up to 3 retries at a 2m interval — ~6.5 minutes
# end to end). Flagging "starting" as an issue with no grace period means every
# routine restart/reboot generates a false-positive finding (and a needless
# Farnsworth restart plan) for any container mid-startup. Used as a fallback when a
# container has no healthcheck configured at all (Docker's own defaults are also 0).
DEFAULT_STARTING_GRACE_SECONDS = 60
# Extra buffer on top of start_period + ((interval + timeout) * retries) for
# scheduling jitter.
STARTING_GRACE_BUFFER_SECONDS = 30
# `docker inspect` only reflects fields explicitly set on the healthcheck — any field
# left unspecified reports as its Go zero-value (0/"0s"), NOT the value Docker actually
# uses at runtime. Applying Docker's own runtime defaults here (interval/timeout 30s,
# retries 3) avoids under-counting the grace period for a healthcheck that e.g. only
# sets `interval:` and leaves retries/timeout implicit.
DOCKER_DEFAULT_INTERVAL_SECONDS = 30
DOCKER_DEFAULT_TIMEOUT_SECONDS = 30
DOCKER_DEFAULT_RETRIES = 3
DOCKER_DEFAULT_START_INTERVAL_SECONDS = 5

# Longer/multi-character units must be tried before their single-character prefixes
# (e.g. "ms" before "m") or the regex greedily matches the wrong unit — "500ms" would
# otherwise parse as "500m" (30000s) with a dangling unmatched "s".
_GO_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)")
_GO_DURATION_UNIT_SECONDS = {
    "h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9,
}


def _parse_go_duration_seconds(value: str) -> float:
    """Parse Docker's Go-template duration strings (e.g. "30s", "2m0s", "1h30m0s",
    "0s") into seconds. Returns 0.0 for anything unparseable/empty."""
    if not value:
        return 0.0
    total = 0.0
    for amount, unit in _GO_DURATION_RE.findall(value):
        total += float(amount) * _GO_DURATION_UNIT_SECONDS[unit]
    return total


def _inspect_restart_info(names: list[str]) -> dict[str, dict]:
    """Batch docker inspect for RestartCount + current uptime, keyed by container name.
    One inspect call for every container is far cheaper than one call per container."""
    if not names:
        return {}
    base_fields = (
        "{{.Name}}\t{{.RestartCount}}\t{{.State.Status}}\t"
        "{{.State.StartedAt}}\t"
        "{{if .Config.Healthcheck}}1{{else}}0{{end}}\t"
        "{{if .Config.Healthcheck}}{{.Config.Healthcheck.StartPeriod}}{{else}}{{end}}\t"
    )
    start_interval_field = "{{if .Config.Healthcheck}}{{.Config.Healthcheck.StartInterval}}{{else}}{{end}}\t"
    remaining_fields = (
        "{{if .Config.Healthcheck}}{{.Config.Healthcheck.Interval}}{{else}}{{end}}\t"
        "{{if .Config.Healthcheck}}{{.Config.Healthcheck.Timeout}}{{else}}{{end}}\t"
        "{{if .Config.Healthcheck}}{{.Config.Healthcheck.Retries}}{{else}}0{{end}}"
    )
    fmt = base_fields + start_interval_field + remaining_fields
    field_count = 10
    rc, out, err = _run(["docker", "inspect", "--format", fmt, *names], timeout=30)
    if rc != 0 and "StartInterval" in err:
        # Pre-25.0 Docker CLI: HealthConfig has no StartInterval field at all, so
        # referencing it fails the whole template (unlike a Go map lookup, which
        # would just return a zero value) — retry without that field.
        fmt = base_fields + remaining_fields
        field_count = 9
        rc, out, err = _run(["docker", "inspect", "--format", fmt, *names], timeout=30)
    if not out:
        log.warning(f"docker inspect returned nothing: {err}")
        return {}

    info = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != field_count:
            continue
        if field_count == 10:
            (
                name, restart_count_str, state, started_at,
                has_healthcheck_str, start_period_str, start_interval_str, interval_str, timeout_str, retries_str,
            ) = parts
        else:
            (
                name, restart_count_str, state, started_at,
                has_healthcheck_str, start_period_str, interval_str, timeout_str, retries_str,
            ) = parts
            start_interval_str = ""
        name = name.lstrip("/")
        uptime_seconds = None
        if state == "running" and started_at and not started_at.startswith("0001-01-01"):
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                uptime_seconds = (datetime.now(timezone.utc) - started).total_seconds()
            except ValueError:
                pass
        # StartPeriod/StartInterval/Interval/Timeout render as Go duration strings
        # ("30s", "2m0s"), not raw nanoseconds. A container is expected to stay
        # "starting" through its start_period, then up to `retries` more failing
        # checks — each cycle taking up to interval + timeout, since the next interval
        # doesn't start until the current probe finishes — before Docker would even
        # mark it "unhealthy". Any field left unset in the container's own healthcheck
        # config inspects as its Go zero-value, not Docker's actual runtime default,
        # so those get filled in below rather than treated as 0. During start_period,
        # Docker probes at start_interval cadence (default 5s) rather than the regular
        # interval — if start_interval is configured larger than start_period, the
        # first probe may not even fire until start_interval elapses, so the effective
        # start_period floor has to account for that too.
        if has_healthcheck_str == "1":
            start_period_seconds = _parse_go_duration_seconds(start_period_str)
            start_interval_seconds = (
                _parse_go_duration_seconds(start_interval_str) or DOCKER_DEFAULT_START_INTERVAL_SECONDS
            )
            effective_start_period_seconds = max(start_period_seconds, start_interval_seconds)
            interval_seconds = _parse_go_duration_seconds(interval_str) or DOCKER_DEFAULT_INTERVAL_SECONDS
            timeout_seconds = _parse_go_duration_seconds(timeout_str) or DOCKER_DEFAULT_TIMEOUT_SECONDS
            retries = int(retries_str) if retries_str.isdigit() and int(retries_str) > 0 else DOCKER_DEFAULT_RETRIES
            grace = (
                effective_start_period_seconds
                + (interval_seconds + timeout_seconds) * retries
                + STARTING_GRACE_BUFFER_SECONDS
            )
        else:
            grace = DEFAULT_STARTING_GRACE_SECONDS
        info[name] = {
            "restart_count": int(restart_count_str) if restart_count_str.isdigit() else 0,
            "uptime_seconds": uptime_seconds,
            "starting_grace_seconds": grace,
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
            grace = info.get("starting_grace_seconds", DEFAULT_STARTING_GRACE_SECONDS)
            if uptime_seconds is not None and uptime_seconds > grace:
                issue = (
                    f"healthcheck still initialising after {int(uptime_seconds)}s "
                    f"(expected within ~{int(grace)}s)"
                )
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
    except Exception as e:  # noqa: BLE001
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


def check_unraid_exports() -> dict:
    """Check Unraid's /etc/exports for duplicate NFS fsid values.

    A collision (two shares sharing an fsid) makes one of them resolve empty/wrong to NFS
    clients while looking completely healthy at the mount level -- this exact bug silently
    broke urphotos_nfs (see project_casamedia_nfs_migration memory, fixed 2026-08-13).
    Cheap to catch here before it recurs as an "Immich shows empty photos"-style symptom.
    Requires the `unraid` SSH alias (root@192.168.1.171, key-based) to be reachable from
    this host; a connection failure is reported as its own alert rather than raising, since
    Unraid being briefly unreachable shouldn't crash the whole Leela scan.
    """
    rc, out, err = _run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "unraid",
         "grep -o 'fsid=[0-9]*' /etc/exports | sort | uniq -d"],
        timeout=15,
    )
    if rc != 0 and not out:
        return {"reachable": False, "error": err.strip() or f"ssh exited {rc}", "duplicate_fsids": []}
    dupes = [line.strip() for line in out.splitlines() if line.strip()]
    result: dict = {"reachable": True, "duplicate_fsids": dupes}
    if dupes:
        result["alert"] = "HIGH"
    return result


# Containers whose bind-mounted NFS paths are worth probing for a stale file handle.
# An NFS export/inode change on the server side (Unraid share remount, fsid churn --
# see check_unraid_exports() above) can leave an already-running container's cached
# mount broken while the container itself stays "Up"/"healthy": Docker's health
# machinery only sees what the container's own healthcheck checks, and most
# healthchecks (WeKnora's included) are a bare liveness ping that never touches the
# filesystem. Found 2026-08-15 on CASA_WEKNORA_APP/data/files: 20+ real file uploads
# 500'd for several minutes while `docker ps` and /health both kept reporting fine.
NFS_MOUNT_WATCHLIST = [
    ("CASA_WEKNORA_APP", "/data/files"),
]


def check_nfs_mount_health() -> list[dict]:
    """Probe each watched container's NFS-backed bind mount for a stale file handle
    (`stat` returns ESTALE/"Stale file handle" when the container's cached NFS
    dentry/inode is invalidated but the mount stays bound). The only fix is a
    container restart to force a fresh bind mount -- this exists to give Farnsworth
    a finding to act on, since check_containers() never notices (see comment above)."""
    results = []
    for container, path in NFS_MOUNT_WATCHLIST:
        # Wrap `stat` in the container's own `timeout` so a hard-mounted NFS lookup
        # that blocks (server unreachable, not just ESTALE) gets killed *inside* the
        # container. Without this, `docker exec`'s client-side timeout below only
        # kills the local client -- the daemon-started `stat` stays wedged in the
        # container's PID namespace and a prolonged outage leaks one per scan.
        rc, out, err = _run(
            ["docker", "exec", container, "timeout", "8", "stat", path], timeout=10
        )
        entry = {"container": container, "path": path}
        if rc != 0:
            message = err.strip() or out.strip()
            entry["error"] = message
            entry["alert"] = "HIGH" if "stale file handle" in message.lower() else "MEDIUM"
        results.append(entry)
    return results


# Chasing Portugal DAM ingest pipeline (ingest.py) -- same NAS mount/state paths it uses
# itself, kept in sync manually since it's a separate app, not a Planet Express stack.
CHASINGPT_INGEST_ROOT = Path("/chasingportugal_nfs/01_INGEST")
CHASINGPT_QUARANTINED = CHASINGPT_INGEST_ROOT / ".quarantined"
CHASINGPT_STATE_DIR = Path("/home/casaroot/apps/chasingpt/processing")
CHASINGPT_IGNORE_DIR_NAMES = {".completed", ".quarantined"}
CHASINGPT_STUCK_LOCK_AGE_SECONDS = 2 * 60 * 60  # a lock older than this means ingest.py died mid-run
CHASINGPT_SCAN_TIMEOUT_SECONDS = 10


def _scan_chasingpt_ingest() -> dict:
    """Actual traversal for check_chasingpt_ingest() -- kept separate so it can be run
    under a hard timeout (see caller)."""
    result: dict = {"queue_depth": 0, "quarantined_count": 0, "stuck_locks": []}

    # os.path.ismount(), not just is_dir(): mirrors ingest.py's own check (NAS_ROOT in
    # chasingpt/ingest/ingest.py) -- if the NFS mount is detached, 01_INGEST can still
    # resolve as a plain empty local directory and report a false "healthy, empty queue".
    if not os.path.ismount(CHASINGPT_INGEST_ROOT.parent) or not CHASINGPT_INGEST_ROOT.is_dir():
        result["error"] = f"{CHASINGPT_INGEST_ROOT} missing or unmounted -- mount may be stale"
        result["alert"] = "HIGH"
        return result

    result["queue_depth"] = sum(
        1 for entry in CHASINGPT_INGEST_ROOT.iterdir()
        if entry.is_dir() and entry.name not in CHASINGPT_IGNORE_DIR_NAMES and not entry.name.startswith(".")
    )

    if CHASINGPT_QUARANTINED.is_dir():
        result["quarantined_count"] = sum(1 for entry in CHASINGPT_QUARANTINED.iterdir() if entry.is_dir())
    if result["quarantined_count"] > 0:
        result["alert"] = "MEDIUM"

    if CHASINGPT_STATE_DIR.is_dir():
        now = time.time()
        for lock_file in CHASINGPT_STATE_DIR.glob("*.lock"):
            # ingest.py can remove this lock between glob() and stat() as it finishes a
            # project -- that's a normal race, not a scan failure, so skip it rather than
            # letting the FileNotFoundError abort the whole run_full() pipeline.
            try:
                age = now - lock_file.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > CHASINGPT_STUCK_LOCK_AGE_SECONDS:
                result["stuck_locks"].append({"project": lock_file.stem, "age_hours": round(age / 3600, 1)})
        if result["stuck_locks"]:
            result["alert"] = "HIGH"

    return result


def check_chasingpt_ingest() -> dict:
    """Chasing Portugal DAM ingest queue health: pending/ready projects, quarantined
    count, and any stuck lock file left behind by a crashed ingest.py run.

    Not wired into NFS_MOUNT_WATCHLIST/check_mounts() -- those check host-level
    reachability, this checks the app's own queue state on top of that mount.

    /chasingportugal_nfs is hard-mounted, so if the NAS goes unreachable mid-scan,
    Path.is_dir()/iterdir() can block indefinitely instead of failing fast -- unlike
    check_unraid_exports()/check_nfs_mount_health() above, there's no subprocess
    `timeout` to wrap here since this walks the local filesystem directly, not a
    remote command. The scan runs in a daemon thread with a hard deadline instead;
    same leak-on-hang tradeoff as check_nfs_mount_health()'s docker-exec comment --
    a wedged thread is left behind rather than letting one hung scan take down the
    whole run_full() pipeline before it can write a snapshot or raise this alert.
    daemon=True (rather than ThreadPoolExecutor, whose worker threads are non-daemon)
    so a genuinely wedged scan can't also block interpreter/process shutdown."""
    scan_result: dict = {}

    def _run_scan() -> None:
        try:
            scan_result["value"] = _scan_chasingpt_ingest()
        except OSError as e:
            # e.g. an NFS I/O error mid-iterdir() (not just a hang) -- report it as the
            # same kind of alert a missing/unmounted root gets, rather than letting a
            # KeyError on scan_result below abort the whole run_full() pipeline.
            scan_result["value"] = {
                "queue_depth": 0,
                "quarantined_count": 0,
                "stuck_locks": [],
                "error": f"scan failed: {e}",
                "alert": "HIGH",
            }

    thread = threading.Thread(target=_run_scan, daemon=True)
    thread.start()
    thread.join(timeout=CHASINGPT_SCAN_TIMEOUT_SECONDS)
    if thread.is_alive():
        return {
            "queue_depth": 0,
            "quarantined_count": 0,
            "stuck_locks": [],
            "error": f"scan timed out after {CHASINGPT_SCAN_TIMEOUT_SECONDS}s -- mount may be unreachable",
            "alert": "HIGH",
        }
    return scan_result["value"]


def check_system() -> dict:
    """RAM, uptime, and recent journal errors."""
    _, mem_out, _ = _run("free -h")
    _, uptime_out, _ = _run("uptime")
    _, journal_out, _ = _run(
        "journalctl -p err -S '1 hour ago' --no-pager -q --output=short"
    )
    errors = [line for line in journal_out.splitlines() if line.strip()]
    mem_lines = mem_out.splitlines()
    return {
        "memory_summary": mem_lines[1] if len(mem_lines) > 1 else mem_out,
        "uptime": uptime_out,
        "recent_errors": errors[:25],
        "recent_error_count": len(errors),
    }


_BACKUP_CADENCE_HOURS = {"daily": 24, "weekly": 168}


def _last_journal_completion(unit: str) -> tuple[str, str]:
    """Fall back to the journal's persisted last terminal-event entry when systemd's own
    InactiveEnterTimestamp comes back empty. That property is transient, in-memory systemd
    state -- it resets to '' on any daemon-reload/reboot and only repopulates the next time
    the unit runs, which falsely reads as "NEVER" on the dashboard for an infrequent job
    (e.g. weekly) that actually completed fine before the reset. journalctl already reads
    passwordlessly for casaroot elsewhere in this file (see check_system() above); scoped to
    one unit here the same way. Matches both "Finished <unit>" (success) and "Failed to
    start <unit>" (failure) -- matching only success would let a reset InactiveEnterTimestamp
    silently pick an older successful run over a more recent failure, misreporting a broken
    backup as healthy. journalctl's default (non-reversed) order is oldest-first, so the last
    match seen while iterating forward is always the most recent terminal event regardless of
    which of the two it is.

    Returns (last_run, result) rather than just the timestamp -- an independent Codex review
    caught that returning only the timestamp left the caller's separate `Result` property
    (also reset to its default "success" by the same daemon-reload/reboot that wiped
    InactiveEnterTimestamp) unexamined, so a unit whose last real run actually failed could
    still surface with a fresh-looking last_run *and* result="success", i.e. exactly the
    "misreport a broken backup as healthy" failure mode this function exists to prevent, just
    moved from the timestamp into the result field instead. last_run is returned in the same
    "%a %Y-%m-%d %H:%M:%S %Z" shape `systemctl show` emits, since
    dashboard_data._parse_systemd_local_time() expects exactly that format regardless of
    source; result is "success" or "failed" (empty string for either if no matching journal
    entry was found at all)."""
    _, out, _ = _run(f"journalctl -u {unit} -o json --no-pager -n 200")
    last_ts = None
    last_success = None
    for line in out.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("MESSAGE", "")
        if f"Finished {unit}" in message:
            last_ts = entry.get("__REALTIME_TIMESTAMP")
            last_success = True
        elif f"Failed to start {unit}" in message:
            last_ts = entry.get("__REALTIME_TIMESTAMP")
            last_success = False
    if last_ts is None:
        return "", ""
    try:
        dt = datetime.fromtimestamp(int(last_ts) / 1_000_000).astimezone()
    except (ValueError, OSError):
        return "", ""
    return dt.strftime("%a %Y-%m-%d %H:%M:%S %Z"), ("success" if last_success else "failed")


def check_backups() -> dict:
    """Borg backup service + timer status.

    Delegates the timer half (last/next trigger) to stackctl.check_backups() --
    stackctl.BORG_JOBS is already the single source of truth for the service/timer unit
    pairs (powers Farnsworth's /backups command), same reuse pattern as check_mounts()
    delegating to stackctl.check_mounts() above. Reshaped into the dict-keyed-by-job-name
    shape the dashboard already expects, plus a static cadence_hours the dashboard uses to
    judge staleness (a oneshot service's own ActiveState is always "inactive" between
    runs -- see casa_hermes.py's system prompt -- so "state" stays in the payload for
    completeness but the dashboard doesn't lead with it)."""
    timers = {t["label"]: t for t in stackctl.check_backups()}
    result = {}
    for unit in ["daily-borg-backup", "weekly-borg-backup"]:
        rc, out, _ = _run(
            f"systemctl show {unit}.service "
            "--property=ActiveState,Result,ExecMainStatus,InactiveEnterTimestamp"
        )
        props = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        key = "daily" if "daily" in unit else "weekly"
        tmr = timers.get(key, {})
        # InactiveEnterTimestamp, not InactiveExitTimestamp -- the latter is when the
        # oneshot last *left* inactive (i.e. started running), the former is when it
        # last *entered* inactive (i.e. finished). The dashboard reads this as "when did
        # the last snapshot complete", and on this host those two differ by ~2h45m for
        # the daily job -- using the start time understated every displayed age by the
        # job's full runtime and could misclassify a still-running backup as fresh.
        last_run = props.get("InactiveEnterTimestamp", "")
        job_result = props.get("Result", "unknown")
        if rc == 0 and not last_run:
            # Empty (rc == 0, so the query itself succeeded), not missing -- a
            # daemon-reload/reboot since this unit last ran wiped the transient property.
            # Fall back to the journal so an infrequent job (e.g. weekly) doesn't falsely
            # read as "NEVER" until it next happens to fire. Also take the journal's
            # success/failure verdict, not just its timestamp -- the same reset that wiped
            # InactiveEnterTimestamp resets `Result` to its default "success" too, which
            # would otherwise report a job whose last real run actually failed as both
            # fresh AND successful.
            journal_run, journal_result = _last_journal_completion(f"{unit}.service")
            last_run = journal_run or "n/a"
            if journal_result:
                job_result = journal_result
        elif not last_run:
            # rc != 0 -- the systemctl query itself failed (bad unit name, systemd
            # unreachable, etc.), not just an empty transient property. An independent
            # Codex review caught that falling back to the journal in this case too would
            # silently paper over a real query failure with a stale-but-real historical
            # result/timestamp; "unknown"/"n/a" here correctly surfaces the query failure
            # instead of masking it.
            last_run = "n/a"
        result[key] = {
            "state": props.get("ActiveState", "unknown"),
            "result": job_result,
            "exit_code": props.get("ExecMainStatus", "?"),
            "last_run": last_run,
            "next_run": tmr.get("next_run_at", "n/a"),
            "last_trigger": tmr.get("last_run_at", "n/a"),
            "cadence_hours": _BACKUP_CADENCE_HOURS[key],
        }
    return result


def check_services() -> dict:
    """Status of critical systemd services (startup).
    Note: nebula.service and dnclient.service are both intentionally decommissioned —
    remote access is now via Tailscale on OPNsense (outside this host, not monitored here).
    dnclient retired 2026-07-04, see project_casaserver_reip_plan memory for context."""
    units = ["casa-stacks"]
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


_TRAEFIK_DYNAMIC_DIR = Path("/home/casaroot/apps/network/proxy/dynamic")
_TRAEFIK_CERTS_CONTAINER_PREFIX = "/etc/traefik/certs/"
_TRAEFIK_CERTS_HOST_DIR = Path("/home/casaroot/apps/network/proxy/certs")

# openssl quotes a DN component's value (RFC2253-style) when it contains a comma -- e.g.
# `O = "CloudFlare, Inc."` -- and whether that happens at all depends on which openssl
# binary actually runs (this host has two: a Homebrew one on interactive PATHs that never
# quotes, and /usr/bin/openssl, what casa-planetexpress.service's minimal systemd PATH
# resolves to, which does). Match the quoted form first or an unquoted capture group
# swallows everything up to the comma *inside* the quotes, e.g. `"CloudFlare` for
# `"CloudFlare, Inc."`.
_CERT_CN_RE = re.compile(r'CN\s*=\s*(?:"([^"]+)"|([^,/\n]+))')
_CERT_O_RE = re.compile(r'O\s*=\s*(?:"([^"]+)"|([^,/\n]+))')


def _dn_value(m: "re.Match | None") -> str | None:
    """Pull whichever alternative (quoted or bare) matched _CERT_CN_RE/_CERT_O_RE."""
    if not m:
        return None
    return (m.group(1) or m.group(2)).strip()
_CERT_SAN_RE = re.compile(r"DNS:([^,\s]+)")
_CERT_ENDDATE_RE = re.compile(r"notAfter=(.+)")
_CERT_EXPIRING_SOON_DAYS = 30  # dashboard "RENEW SOON" amber tier
_CERT_EXPIRING_CRITICAL_DAYS = 7  # dashboard "EXPIRING" red tier


def _discover_cert_files() -> list[Path]:
    """Traefik's certificatesResolvers/ACME are disabled here (see check_certs()) --
    the certs it actually serves are static files declared via the file provider's
    tls.certificates[] blocks in ~/apps/network/proxy/dynamic/*.yml (that directory is
    exactly what providers.file.directory watches in traefik.yml). Read those
    declarations rather than globbing the certs directory directly: that directory also
    holds the internal CA's cert/key and a stray .csr, which aren't certs Traefik serves."""
    paths: list[Path] = []
    if not _TRAEFIK_DYNAMIC_DIR.is_dir():
        return paths
    for yml_path in sorted(_TRAEFIK_DYNAMIC_DIR.glob("*.yml")):
        try:
            data = yaml.safe_load(yml_path.read_text()) or {}
        except Exception as e:  # noqa: BLE001
            # A malformed dynamic config is exactly the kind of thing this check exists
            # to catch -- don't swallow it without a trace, even though it's rare enough
            # that surfacing it as its own dashboard row isn't worth the complexity here.
            log.warning(f"Skipping unparseable Traefik dynamic config {yml_path.name}: {e}")
            continue
        if not isinstance(data, dict):
            continue
        for entry in (data.get("tls") or {}).get("certificates") or []:
            cert_file = entry.get("certFile", "")
            if cert_file.startswith(_TRAEFIK_CERTS_CONTAINER_PREFIX):
                host_path = _TRAEFIK_CERTS_HOST_DIR / cert_file[len(_TRAEFIK_CERTS_CONTAINER_PREFIX):]
                if host_path not in paths:
                    paths.append(host_path)
    return paths


def _cert_expiry_status(days_remaining: int | None) -> str:
    if days_remaining is None:
        return "valid"
    if days_remaining < 0:
        return "expired"
    if days_remaining <= _CERT_EXPIRING_CRITICAL_DAYS:
        return "expiring"
    if days_remaining <= _CERT_EXPIRING_SOON_DAYS:
        return "renew_soon"
    return "valid"


def _parse_cert_file(path: Path) -> dict:
    if not path.exists():
        return {"error": f"{path.name}: declared in Traefik's dynamic config but missing on disk", "resolver": path.stem}
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-subject", "-issuer", "-ext", "subjectAltName", "-enddate"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception as e:  # noqa: BLE001
        return {"error": f"{path.name}: openssl failed to read cert ({e})", "resolver": path.stem}
    out = result.stdout
    # -subject and -issuer both print a "...CN = ..." line -- search each field's own
    # line rather than the whole blob, or a blind CN regex could grab the issuer's CN
    # for a self-signed-style cert whose issuer line happens to come first.
    subject_line = next((ln for ln in out.splitlines() if ln.startswith("subject=")), "")
    issuer_line = next((ln for ln in out.splitlines() if ln.startswith("issuer=")), "")
    cn_match = _CERT_CN_RE.search(subject_line)
    # Issuer often has no CN (e.g. Cloudflare Origin CA's issuer is C/O/OU/L/ST only)
    # -- fall back to the organization name, still more useful than "?".
    issuer_match = _CERT_CN_RE.search(issuer_line) or _CERT_O_RE.search(issuer_line)
    end_match = _CERT_ENDDATE_RE.search(out)
    sans = _CERT_SAN_RE.findall(out)
    domain = _dn_value(cn_match) or path.stem
    if " " in domain and sans:
        # Cloudflare Origin Certs use a fixed, non-hostname CN ("CloudFlare Origin
        # Certificate") -- the real domain only shows up in the SANs, so prefer that
        # when the CN clearly isn't a hostname.
        domain = sans[0]
    expires = end_match.group(1).strip() if end_match else "?"
    days_remaining = None
    if end_match:
        try:
            expiry_dt = datetime.strptime(expires.replace(" GMT", ""), "%b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
            days_remaining = (expiry_dt - datetime.now(timezone.utc)).days
        except ValueError:
            pass
    return {
        "domain": domain,
        "sans": sans,
        "resolver": path.stem,  # not an ACME resolver -- these are static file-provider certs, labeled by source file
        "issuer": _dn_value(issuer_match) or "?",
        "expires": expires,
        "days_remaining": days_remaining,
        "status": _cert_expiry_status(days_remaining),
    }


def check_certs() -> list[dict]:
    """Read the TLS certs Traefik actually serves.

    ACME is disabled here (2026-07-03): the entire certificatesResolvers block in
    ~/apps/network/proxy/traefik.yml is commented out, so acme.json is dead legacy data
    nothing reads or writes -- a previous version of this function read that file and
    always came back empty, which is why the Certificate Vault panel showed "no
    certificates found" despite two real certs being live (an internal-CA wildcard for
    casalan.com, a Cloudflare origin cert for casaalmida.com). Both are declared as
    static files via Traefik's file provider instead, so this reads those declarations
    and inspects the actual cert files with openssl."""
    cert_files = _discover_cert_files()
    if not cert_files:
        return [{"note": "No TLS certificates declared in Traefik's file provider (~/apps/network/proxy/dynamic/*.yml)."}]
    parsed = [_parse_cert_file(p) for p in cert_files]
    # A cert that's declared but unreadable/missing gets its own explicit "kind": "error"
    # row instead of smuggling the message through "domain" -- the dashboard renders
    # these as their own card (red border, UNREADABLE in place of a domain), never
    # crammed into a table cell. Never dropped, whether it's the only cert or one of many.
    for c in parsed:
        if "error" in c:
            c["kind"] = "error"
    return parsed


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
        "unraid_exports": check_unraid_exports(),
        "nfs_mount_health": check_nfs_mount_health(),
        "chasingpt_ingest": check_chasingpt_ingest(),
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
    n_dupe_fsids = len(snapshot["unraid_exports"].get("duplicate_fsids", []))
    n_stale_mounts = sum(1 for m in snapshot["nfs_mount_health"] if m.get("alert"))
    n_quarantined = snapshot["chasingpt_ingest"].get("quarantined_count", 0)
    log.info(
        f"Leela scan complete — "
        f"{n_issues} container issue(s) ({n_crash} crash-looping), {n_disk} disk alert(s), "
        f"{n_missing_stacks} stack(s) with missing containers, "
        f"{len(snapshot['mounts']['missing'])} missing mount(s), "
        f"{n_dupe_fsids} duplicate Unraid fsid(s), "
        f"{n_stale_mounts} stale NFS mount(s), "
        f"{n_quarantined} quarantined chasingpt project(s)"
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
