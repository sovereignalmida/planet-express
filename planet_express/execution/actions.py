"""
planet_express/execution/actions.py — container facts and health checks shared by the
canary updater (casa_zoidberg), Amy's failure investigation (casa_farnsworth) and, from
landing 1b, the typed-action verifier.

Extracted from casa_zoidberg.py in landing 1a (T5) with behavior unchanged, pinned by
tests/test_zoidberg_health_regression.py. Every command is an argv list run through
casa_bender.run_argv: no shell, minimal environment. That also closes a real gap in the
old Amy label lookup, which interpolated a container name (possibly from an LLM-written
plan) into a shell=True command string.
"""

import time
from pathlib import Path

import casa_bender as bender

# Same as casa_zoidberg._run()'s default, which these checks used before the extraction.
# A shorter value turns a slow or remote daemon into a "missing/unhealthy" verdict and a
# false canary rollback (Codex review, landing 1a). Latency-sensitive callers (the 1b RPC
# read path) pass their own shorter timeout instead of lowering this.
DOCKER_TIMEOUT_SECONDS = 120
DEFAULT_POLL_SECONDS = 5

# Most services have no Docker healthcheck, in which case .State.Health doesn't exist and
# a bare {{.State.Health.Status}} makes the WHOLE `docker inspect` call fail, not just that
# field. The if/else guard is required, not cosmetic (caught by dry-run testing before
# the canary updater ever ran live).
_HEALTH_FORMAT = (
    "{{.State.Status}}\t{{.RestartCount}}\t"
    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
)
_COMPOSE_LABELS_FORMAT = (
    '{{index .Config.Labels "com.docker.compose.project"}}\t'
    '{{index .Config.Labels "com.docker.compose.service"}}'
)


def service_container(stack_dir: Path, service: str) -> str | None:
    """Name of the (first) container compose runs for `service`, or None."""
    rc, out, _err = bender.run_argv(
        ["docker", "compose", "-f", f"{stack_dir}/docker-compose.yml", "ps", "-q", service],
        timeout=DOCKER_TIMEOUT_SECONDS,
    )
    if rc != 0 or not out.strip():
        return None
    container_id = out.strip().splitlines()[0]
    rc, name, _err = bender.run_argv(
        ["docker", "inspect", "--format", "{{.Name}}", container_id],
        timeout=DOCKER_TIMEOUT_SECONDS,
    )
    return name.lstrip("/") if rc == 0 and name else None


def container_health(container_name: str, baseline_restarts: int = 0) -> tuple[bool, str]:
    """Same signal Leela's check_containers() uses: running, not unhealthy, not
    restarting. A container with no healthcheck (health "none") and one whose healthcheck
    is still "starting" both count as OK; absence of a healthcheck isn't a failure.

    baseline_restarts is the RestartCount taken before the action being verified. The
    canary updater passes 0 (a freshly recreated container), so any restart fails. A
    typed restart of an existing container passes its pre-action count (landing 1b)."""
    rc, out, err = bender.run_argv(
        ["docker", "inspect", "--format", _HEALTH_FORMAT, container_name],
        timeout=DOCKER_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return False, f"inspect failed: {err}"
    parts = out.split("\t")
    status = parts[0] if len(parts) > 0 else ""
    restart_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    health = parts[2] if len(parts) > 2 else ""
    if status != "running":
        return False, f"status={status}"
    if health == "unhealthy":
        return False, "healthcheck failing"
    restarts = restart_count - baseline_restarts
    if restarts >= 1:
        return False, f"restarted {restarts}x during watch window"
    return True, "ok"


def watch_until_stable(
    container_name: str,
    seconds: int,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    baseline_restarts: int = 0,
) -> tuple[bool, str]:
    """Poll container_health every poll_seconds for `seconds`; fail on the first bad
    poll, pass only if every poll was OK."""
    elapsed = 0
    last_reason = "no data"
    while elapsed < seconds:
        time.sleep(poll_seconds)
        elapsed += poll_seconds
        ok, reason = container_health(container_name, baseline_restarts)
        last_reason = reason
        if not ok:
            return False, reason
    return True, last_reason


def container_compose_labels(container: str) -> tuple[str, str]:
    """(stack, service) from a container's compose project/service labels. Falls back to
    ("unknown", container) when the container isn't compose-managed or inspect fails,
    matching the lookup Amy's investigation used before."""
    rc, out, _err = bender.run_argv(
        ["docker", "inspect", "--format", _COMPOSE_LABELS_FORMAT, container],
        timeout=DOCKER_TIMEOUT_SECONDS,
    )
    stack, _, service = (out if rc == 0 else "").strip().partition("\t")
    return stack.strip() or "unknown", service.strip() or container
