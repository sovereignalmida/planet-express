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

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import config

log_prefix = "[casa_boot]"
BOOT_TIMEOUT_SECONDS = 270
DOCKER_QUERY_TIMEOUT_SECONDS = 20

# Keep this deliberately narrow: a namespace mode using a container *name* is
# valid, but Docker only bakes a stale reference into the configuration when it
# is an ID.  Docker IDs are at least 12 hexadecimal characters.
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}")
MISSING_LABEL_VALUES = {"", "<no value>"}
INSPECT_FORMAT = (
    "{{.Id}}\t{{.Name}}\t{{.HostConfig.NetworkMode}}\t"
    "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}\t"
    "{{index .Config.Labels \"com.docker.compose.service\"}}\t"
    "{{index .Config.Labels \"com.docker.compose.oneoff\"}}\t"
    "{{.State.Status}}\t{{.State.ExitCode}}"
)


@dataclass(frozen=True)
class Container:
    container_id: str
    name: str
    network_mode: str
    config_files: str
    service: str
    one_off: str
    state: str
    exit_code: str


def _timeout(limit: int, deadline: float | None) -> float | None:
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    return min(limit, remaining) if remaining > 0 else None


def _run_compose(compose_files: list[str], args: list[str], deadline: float | None = None) -> int:
    """Run one bounded Compose command and return its exit status."""
    argv = ["docker", "compose"]
    for compose_file in compose_files:
        argv.extend(["-f", compose_file])
    argv.extend(args)
    timeout = _timeout(BOOT_TIMEOUT_SECONDS, deadline)
    if timeout is None:
        print(f"{log_prefix} ERROR: boot time budget exhausted before docker compose")
        return 124
    try:
        return subprocess.run(
            argv, check=False, timeout=timeout,
        ).returncode
    except subprocess.TimeoutExpired:
        print(f"{log_prefix} ERROR: docker compose timed out after {timeout:g}s")
        return 124
    except OSError as error:
        print(f"{log_prefix} ERROR: cannot run docker compose: {error}")
        return 127


def _docker_query(argv: list[str], deadline: float | None = None) -> str | None:
    """Return structured Docker output, or None when it cannot be read."""
    timeout = _timeout(DOCKER_QUERY_TIMEOUT_SECONDS, deadline)
    if timeout is None:
        print(f"{log_prefix} ERROR: boot time budget exhausted before docker query")
        return None
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"{log_prefix} ERROR: docker query timed out after {timeout:g}s")
        return None
    except OSError as error:
        print(f"{log_prefix} ERROR: cannot run docker query: {error}")
        return None
    if result.returncode != 0:
        print(f"{log_prefix} ERROR: docker query failed (exit {result.returncode})")
        return None
    return result.stdout


def _containers(deadline: float | None = None) -> list[Container] | None:
    """Read all containers, including stopped ones, without parsing ps columns."""
    ids = _docker_query([
        "docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}",
    ], deadline)
    if ids is None:
        return None
    container_ids = [container_id for container_id in ids.splitlines() if container_id]
    if not container_ids:
        return []

    inspected = _docker_query([
        "docker", "inspect", "--format", INSPECT_FORMAT, *container_ids,
    ], deadline)
    if inspected is None:
        return None

    containers = []
    for line in inspected.splitlines():
        fields = line.split("\t")
        if len(fields) != 8:
            print(f"{log_prefix} ERROR: malformed docker inspect output")
            return None
        container_id, name, network_mode, config_files, service, one_off, state, exit_code = fields
        containers.append(Container(
            container_id=container_id,
            name=name.removeprefix("/"),
            network_mode=network_mode,
            config_files=config_files,
            service=service,
            one_off=one_off,
            state=state,
            exit_code=exit_code,
        ))
    return containers


def _compose_files(label: str) -> list[str]:
    """Compose stores multiple -f inputs as a comma-separated label."""
    if label in MISSING_LABEL_VALUES:
        return []
    return [path for path in label.split(",") if path]


def _belongs_to_active_stack(container: Container, active_compose_files: set[str]) -> bool:
    return bool({str(Path(path).resolve()) for path in _compose_files(container.config_files)} & active_compose_files)


def _declared_services(compose_files: set[str], deadline: float | None = None) -> set[str] | None:
    """Every service name the active compose files still declare, or None if unreadable.

    A container keeps the compose labels it was created with, including the path of a file
    that is still active -- so deleting a SERVICE from an otherwise live compose file leaves
    an orphan that `_belongs_to_active_stack` still claims. Lidarr is exactly that: retired
    out of media/docker-compose.yml, container left behind, carrying media's path. Without
    this check the final verification calls it down and fails every boot, which is the
    opposite of what retiring something should do.

    Unreadable is not empty: None means "could not tell", and the caller then declines to
    treat anything as orphaned rather than silently ignoring real containers.
    """
    declared: set[str] = set()
    for path in sorted(compose_files):
        output = _docker_query(
            ["docker", "compose", "-f", path, "config", "--services"], deadline
        )
        if output is None:
            return None
        declared.update(line.strip() for line in output.splitlines() if line.strip())
    return declared


def _is_orphan_of_removed_service(
    container: Container, declared_services: set[str] | None
) -> bool:
    """True only when we positively know the service is gone from the active config."""
    if declared_services is None or not container.service:
        return False
    return container.service not in declared_services


def _namespace_target_is_live(namespace_id: str, containers: list[Container]) -> bool:
    if any(container.name == namespace_id for container in containers):
        return True
    return sum(container.container_id.startswith(namespace_id) for container in containers) == 1


def repair_dead_namespace_references(
    deadline: float | None = None, active_compose_files: set[str] | None = None,
) -> int:
    """Force-recreate Compose services bound to a vanished container namespace.

    Compose will not detect this itself: the service configuration is unchanged,
    even though Docker resolved ``container:<name>`` to a now-dead ID at create
    time.  Missing Compose labels deliberately make a container ineligible.
    """
    containers = _containers(deadline)
    if containers is None:
        print(f"{log_prefix} ERROR: cannot check dead namespace references")
        return 1

    repairs = []
    for container in containers:
        if container.name in config.PAUSED_CONTAINERS or container.one_off.lower() == "true":
            continue
        if not container.network_mode.startswith("container:"):
            continue
        namespace_id = container.network_mode.removeprefix("container:")
        if not CONTAINER_ID_RE.fullmatch(namespace_id) or _namespace_target_is_live(
            namespace_id, containers,
        ):
            continue
        compose_files = _compose_files(container.config_files)
        if not compose_files or container.service in MISSING_LABEL_VALUES:
            print(
                f"{log_prefix} Skipping {container.name}: dead namespace reference "
                "but no Compose labels"
            )
            continue
        if active_compose_files is not None and not _belongs_to_active_stack(
            container, active_compose_files,
        ):
            continue
        repairs.append((container, compose_files))

    failed = 0
    for container, compose_files in repairs:
        print(f"{log_prefix} Recreating {container.name}: dead namespace reference")
        if _run_compose(
            compose_files, ["up", "-d", "--force-recreate", container.service], deadline,
        ) != 0:
            failed += 1
            print(f"{log_prefix} ERROR: recreate failed for {container.name}")
    return failed


def bring_up_all_stacks() -> int:
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    stacks = config.active_stack_dirs()
    # "network" (Traefik/DNS/Gluetun) goes first — everything else routes through
    # it, so it's the one real ordering guarantee worth keeping. Everything else
    # runs in whatever order config.active_stack_dirs() returns; no artificial
    # waits between them.
    stacks.sort(key=lambda d: (d.name != "network", d.name))
    active_compose_files = {
        str((stack_dir / "docker-compose.yml").resolve()) for stack_dir in stacks
    }
    print(f"{log_prefix} {len(stacks)} active stack(s): {', '.join(s.name for s in stacks)}")

    failed: list[Path] = []
    for stack_dir in stacks:
        compose_file = stack_dir / "docker-compose.yml"
        print(f"{log_prefix} Starting stack: {stack_dir.name}")
        returncode = _run_compose([str(compose_file)], ["up", "-d"], deadline)
        if returncode != 0:
            print(f"{log_prefix} ERROR: {stack_dir.name} failed (exit {returncode})")
            failed.append(stack_dir)

    repair_failures = repair_dead_namespace_references(deadline, active_compose_files)

    retry_failed: list[Path] = []
    if failed:
        print(f"{log_prefix} Retrying failed stacks once: {', '.join(stack.name for stack in failed)}")
        for stack_dir in failed:
            compose_file = stack_dir / "docker-compose.yml"
            returncode = _run_compose([str(compose_file)], ["up", "-d"], deadline)
            if returncode != 0:
                retry_failed.append(stack_dir)
                print(f"{log_prefix} ERROR: {stack_dir.name} retry failed (exit {returncode})")

    containers = _containers(deadline)
    if containers is None:
        print(f"{log_prefix} ERROR: cannot verify final container state")
        return 1
    declared_services = _declared_services(active_compose_files, deadline)
    if declared_services is None:
        print(f"{log_prefix} WARNING: could not read declared services; "
              "not treating any container as a retired orphan")
    down = [
        container for container in containers
        if (
            _belongs_to_active_stack(container, active_compose_files)
            and not _is_orphan_of_removed_service(container, declared_services)
            and container.name not in config.PAUSED_CONTAINERS
            and container.one_off.lower() != "true"
            and container.state != "running"
        )
    ]
    if down:
        summary = ", ".join(
            f"{container.name} (state {container.state}, exit {container.exit_code})"
            for container in down
        )
        print(f"{log_prefix} Containers still down: {summary}")
        return 1
    if retry_failed:
        print(f"{log_prefix} Failed retry stacks: {', '.join(stack.name for stack in retry_failed)}")
        return 1
    if repair_failures:
        print(f"{log_prefix} Failed namespace recreates: {repair_failures}")
        return 1

    if failed:
        print(f"{log_prefix} All stacks started after retry.")
        return 0

    print(f"{log_prefix} All stacks started.")
    return 0


if __name__ == "__main__":
    sys.exit(bring_up_all_stacks())
