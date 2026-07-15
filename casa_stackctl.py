"""
casa_stackctl.py — manual stack up/down/mount control.

Library functions used both by this file's CLI and by Farnsworth's Telegram
/up, /down, /stacks, /mounts commands — one code path, not two copies of the
same docker compose logic.

Companion to casa_boot.py, which only ever brings everything up once at boot
time, gated on mount readiness. This is for ad-hoc operator use: bring one
stack down for maintenance, bring it back up, or take everything down (e.g.
a NAS outage) without cd-ing into each stacks/<name>/ directory by hand.

Forbidden stacks (config.FORBIDDEN_STACKS) can never be brought *up* through
this tool -- same rule the boot orchestrator and monitor follow. They CAN be
brought down, since stopping something is never the unsafe direction -- that
matters if a forbidden stack ever ends up running out-of-band (Dockge, a
manual `docker compose up`, a stale process), which is exactly what happened
2026-07-07 with the `ai` stack.

CLI usage:
    python casa_stackctl.py list
    python casa_stackctl.py up <stack>|--all
    python casa_stackctl.py down <stack>|--all
    python casa_stackctl.py mounts
"""

import argparse
import logging
import os
import subprocess
import sys
import time

import config

log_prefix = "[casa_stackctl]"
log = logging.getLogger("planetexpress.stackctl")

# unit -> mount point comes from config.MOUNT_UNITS (single source of truth, shared with
# casa_leela's periodic check). autofs units report ActiveState=inactive when idle even
# though they work fine on access, so the real test is whether the path is actually
# listable, not the unit's reported state.

# Borg backup services/timers -- systemctl show works passwordlessly for both (unlike
# journalctl, which needs a password on this host), so status reporting never needs sudo.
BORG_JOBS = {
    "daily": ("daily-borg-backup.service", "daily-borg-backup.timer"),
    "weekly": ("weekly-borg-backup.service", "weekly-borg-backup.timer"),
}


def all_stack_dirs():
    """Every directory with a docker-compose.yml, forbidden or not -- used for
    `down`/`list`, where seeing/stopping a forbidden stack is the point."""
    return [p.parent for p in sorted(config.STACKS_ROOT.glob("*/docker-compose.yml"))]


def resolve_stack(name):
    compose = config.STACKS_ROOT / name / "docker-compose.yml"
    if not compose.exists():
        return None
    return compose.parent


def _run_compose_captured(stack_dir, args, timeout=180):
    compose_file = stack_dir / "docker-compose.yml"
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    tail = (result.stdout + result.stderr).strip().splitlines()
    return result.returncode == 0, "\n".join(tail[-6:])


def stack_up(target: str) -> dict:
    """target is a stack name or 'all'. Returns
    {"ok": bool, "refused"/"not_found": bool, "results": [(name, ok, tail), ...]}"""
    if target == "all":
        stacks = config.active_stack_dirs()
        stacks.sort(key=lambda d: (d.name != "network", d.name))
    else:
        if target in config.FORBIDDEN_STACKS:
            return {"ok": False, "refused": True, "results": []}
        stack_dir = resolve_stack(target)
        if stack_dir is None:
            return {"ok": False, "not_found": True, "results": []}
        stacks = [stack_dir]

    results = []
    for stack_dir in stacks:
        ok, tail = _run_compose_captured(stack_dir, ["up", "-d"])
        results.append((stack_dir.name, ok, tail))
    return {"ok": all(r[1] for r in results), "results": results}


def stack_down(target: str) -> dict:
    if target == "all":
        stacks = all_stack_dirs()
        # tear down everything else before the ingress/DNS stack
        stacks.sort(key=lambda d: d.name == "network")
    else:
        stack_dir = resolve_stack(target)
        if stack_dir is None:
            return {"ok": False, "not_found": True, "results": []}
        stacks = [stack_dir]

    results = []
    for stack_dir in stacks:
        ok, tail = _run_compose_captured(stack_dir, ["down"])
        results.append((stack_dir.name, ok, tail))
    return {"ok": all(r[1] for r in results), "results": results}


MOUNT_CHECK_RETRIES = 3
MOUNT_CHECK_RETRY_DELAY = 2  # seconds


def check_mounts() -> list[tuple[str, str, bool]]:
    """[(unit, path, reachable), ...]. Listing the path (not systemctl is-active) is what
    actually exercises autofs, matching how mounts_ready.sh verifies at boot time.

    A single failed listdir() right after Unraid comes back from an outage is expected, not
    damning: a persistent CIFS mount (casamedia) may not have reconnected its session yet, and
    an idle autofs mount (erugo) may be racing this exact check as its trigger for a fresh mount
    attempt while the NAS's SMB service is still settling. Retry a few times before calling it
    unreachable, and log the real errno on final failure instead of swallowing it to a bare bool.
    """
    results = []
    for unit, path in config.MOUNT_UNITS.items():
        last_err = None
        ok = False
        for attempt in range(1, MOUNT_CHECK_RETRIES + 1):
            try:
                os.listdir(path)
                ok = True
                break
            except OSError as e:
                last_err = e
                if attempt < MOUNT_CHECK_RETRIES:
                    time.sleep(MOUNT_CHECK_RETRY_DELAY)
        if not ok:
            log.warning(
                f"{unit} -> {path} unreachable after {MOUNT_CHECK_RETRIES} attempts: {last_err}"
            )
        results.append((unit, path, ok))
    return results


def _systemctl_show(unit: str, *props: str) -> dict:
    out = subprocess.run(
        ["systemctl", "show", unit, *[f"-p{p}" for p in props]],
        capture_output=True, text=True, timeout=10,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def check_backups() -> list[dict]:
    """Last-run result + last/next trigger time for each Borg backup job, straight from
    systemd (`systemctl show`) -- deliberately not journalctl, which needs a password on
    this host (only casa-startup.service actions and *.mount start/stop are NOPASSWD), and
    not the Borg repo itself, so this never needs the backup passphrase in the agent's
    hands. Result=success/failure comes from the service unit; trigger times come from the
    timer unit, since the oneshot service unit resets between runs."""
    results = []
    for label, (service, timer) in BORG_JOBS.items():
        svc = _systemctl_show(service, "Result", "ExecMainStatus")
        tmr = _systemctl_show(timer, "LastTriggerUSec", "NextElapseUSecRealtime")
        results.append({
            "label": label,
            "result": svc.get("Result", "unknown"),
            "exit_status": svc.get("ExecMainStatus", "?"),
            "last_run_at": tmr.get("LastTriggerUSec", "unknown"),
            "next_run_at": tmr.get("NextElapseUSecRealtime", "unknown"),
        })
    return results


def cmd_up(stack, do_all):
    target = "all" if do_all else stack
    result = stack_up(target)
    if result.get("refused"):
        print(f"{log_prefix} REFUSED: '{target}' is in FORBIDDEN_STACKS, will not start it.")
        return 1
    if result.get("not_found"):
        print(f"{log_prefix} ERROR: no docker-compose.yml for '{target}'")
        return 1
    for name, ok, tail in result["results"]:
        print(f"{log_prefix} up: {name} — {'ok' if ok else 'FAILED'}")
        if not ok and tail:
            print(tail)
    if not result["ok"]:
        return 1
    print(f"{log_prefix} Done.")
    return 0


def cmd_down(stack, do_all):
    target = "all" if do_all else stack
    result = stack_down(target)
    if result.get("not_found"):
        print(f"{log_prefix} ERROR: no docker-compose.yml for '{target}'")
        return 1
    for name, ok, tail in result["results"]:
        print(f"{log_prefix} down: {name} — {'ok' if ok else 'FAILED'}")
        if not ok and tail:
            print(tail)
    if not result["ok"]:
        return 1
    print(f"{log_prefix} Done.")
    return 0


def cmd_list():
    forbidden = set(config.FORBIDDEN_STACKS)
    for stack_dir in all_stack_dirs():
        tag = " (forbidden)" if stack_dir.name in forbidden else ""
        print(f"{stack_dir.name}{tag}")
    return 0


def cmd_mounts():
    results = check_mounts()
    for unit, path, ok in results:
        print(f"{unit} -> {path}: {'OK' if ok else 'FAIL'}")
    return 0 if all(ok for _, _, ok in results) else 1


def cmd_backups():
    results = check_backups()
    ok = True
    for r in results:
        if r["result"] != "success":
            ok = False
        print(
            f"{r['label']}: {r['result']} (exit {r['exit_status']}) "
            f"last={r['last_run_at']} next={r['next_run_at']}"
        )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Bring stacks up or down manually.")
    sub = parser.add_subparsers(dest="action", required=True)

    for verb in ("up", "down"):
        p = sub.add_parser(verb)
        p.add_argument("stack", nargs="?", help="stack name, e.g. media")
        p.add_argument("--all", action="store_true", help="apply to every stack")

    sub.add_parser("list")
    sub.add_parser("mounts")
    sub.add_parser("backups")

    args = parser.parse_args()

    if args.action == "list":
        return cmd_list()
    if args.action == "mounts":
        return cmd_mounts()
    if args.action == "backups":
        return cmd_backups()

    if not args.all and not args.stack:
        parser.error(f"{args.action} requires either a stack name or --all")

    if args.action == "up":
        return cmd_up(args.stack, args.all)
    return cmd_down(args.stack, args.all)


if __name__ == "__main__":
    sys.exit(main())
