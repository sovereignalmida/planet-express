"""
casa_boot.py — Boot-time stack bring-up.

Replaces the stack-orchestration half of the old start_stacks.sh. That script
re-derived "which stacks, in what order" via a subprocess call into this same
config.py plus shell string-splitting — a round-trip that broke silently once
already (an IFS collision swallowed the forbidden-stacks check for hours with
no visible error). Calling config.active_stack_dirs() directly here means
there's no shell boundary left to introduce that class of bug again.

Gated on mount readiness by systemd (Requires=/After= casa-mounts.service),
not by anything in this script — if mounts_ready.sh fails, this never runs.

No artificial waits between stacks or containers. `docker compose up -d`
returns once containers are created; that's enough to move to the next
stack. Bender's own 6-hourly monitor cycle (Leela) is the real safety net
for anything that comes up unhealthy — this script's only job is "get
everything running," fast, matching how it's done by hand.

Usage:
    python casa_boot.py
"""

import subprocess
import sys

import config

log_prefix = "[casa_boot]"


def bring_up_all_stacks() -> int:
    stacks = config.active_stack_dirs()
    # "network" (Traefik/DNS/Gluetun) goes first — everything else routes through
    # it, so it's the one real ordering guarantee worth keeping. Everything else
    # runs in whatever order config.active_stack_dirs() returns; no artificial
    # waits between them.
    stacks.sort(key=lambda d: (d.name != "network", d.name))
    print(f"{log_prefix} {len(stacks)} active stack(s): {', '.join(s.name for s in stacks)}")

    failed = []
    for stack_dir in stacks:
        compose_file = stack_dir / "docker-compose.yml"
        print(f"{log_prefix} Starting stack: {stack_dir.name}")
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        )
        if result.returncode != 0:
            print(f"{log_prefix} ERROR: {stack_dir.name} failed (exit {result.returncode})")
            failed.append(stack_dir.name)

    if failed:
        print(f"{log_prefix} Failed stacks: {', '.join(failed)}")
        return 1

    print(f"{log_prefix} All stacks started.")
    return 0


if __name__ == "__main__":
    sys.exit(bring_up_all_stacks())
