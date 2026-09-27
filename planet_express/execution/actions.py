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

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import casa_bender as bender
import config
from planet_express.core.redact import redact
from planet_express.execution.runbook import IMAGE_REFERENCE_MAX

log = logging.getLogger("planetexpress.actions")

# Same as casa_zoidberg._run()'s default, which these checks used before the extraction.
# A shorter value turns a slow or remote daemon into a "missing/unhealthy" verdict and a
# false canary rollback (Codex review, landing 1a). Latency-sensitive callers (the 1b RPC
# read path) pass their own shorter timeout instead of lowering this.
DOCKER_TIMEOUT_SECONDS = 120
RPC_DOCKER_TIMEOUT_SECONDS = 4
DEFAULT_POLL_SECONDS = 5
VERIFY_TIMEOUT_SECONDS = 90
VERIFY_STABLE_SECONDS = 15

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
_COMPOSE_IDENTITIES_FORMAT = (
    '{{.Name}}\t{{index .Config.Labels "com.docker.compose.project"}}\t'
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


@dataclass(frozen=True)
class HealthReading:
    error: str | None  # set when docker inspect itself failed
    status: str
    restart_count: int
    health: str  # "healthy" | "unhealthy" | "starting" | "none"


def read_health(container_name: str, *, timeout=DOCKER_TIMEOUT_SECONDS) -> HealthReading:
    """One `docker inspect` for status, RestartCount and health (with the Health guard)."""
    rc, out, err = bender.run_argv(
        ["docker", "inspect", "--format", _HEALTH_FORMAT, container_name],
        timeout=timeout,
    )
    if rc != 0:
        return HealthReading(error=err, status="", restart_count=0, health="")
    parts = out.split("\t")
    return HealthReading(
        error=None,
        status=parts[0] if len(parts) > 0 else "",
        restart_count=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
        health=parts[2] if len(parts) > 2 else "",
    )


def container_health(container_name: str, baseline_restarts: int = 0) -> tuple[bool, str]:
    """Same signal Leela's check_containers() uses: running, not unhealthy, not
    restarting. A container with no healthcheck (health "none") and one whose healthcheck
    is still "starting" both count as OK; absence of a healthcheck isn't a failure.

    baseline_restarts is the RestartCount taken before the action being verified. The
    canary updater passes 0 (a freshly recreated container), so any restart fails. A
    typed restart of an existing container passes its pre-action count (landing 1b)."""
    reading = read_health(container_name)
    if reading.error is not None:
        return False, f"inspect failed: {reading.error}"
    if reading.status != "running":
        return False, f"status={reading.status}"
    if reading.health == "unhealthy":
        return False, "healthcheck failing"
    restarts = reading.restart_count - baseline_restarts
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


# ── Targets (landing 1b) ─────────────────────────────────────────────────────────
# Resolution happens before any mutating argv exists. Every refusal is a TargetError, and
# the cheap refusals (bad names, forbidden stacks, network-guarded services) come before
# any docker call at all.
#
#   names valid? ─► stack forbidden? ─► network-guarded? ─► compose file exists?
#       ─► service in `config --services`? ─► exactly one container? ─► paused? ─► Target
NETWORK_GUARDED_SUBSTRINGS = ("traefik", "adguard")  # same rule as Zoidberg and Bender's network guard
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_CONTAINER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")


def container_compose_identities(
    containers: list[str], *, timeout=DOCKER_TIMEOUT_SECONDS
) -> dict[str, tuple[str, str]]:
    """Strict Compose identities from one bounded inspect; incomplete labels are omitted."""
    if not containers:
        return {}
    if len(containers) > 100 or any(
        not isinstance(name, str) or not _CONTAINER_NAME_RE.fullmatch(name) for name in containers
    ):
        raise TargetError("invalid container name")
    rc, out, _err = bender.run_argv(
        ["docker", "inspect", "--format", _COMPOSE_IDENTITIES_FORMAT, *containers], timeout=timeout,
    )
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
        raise TargetTimeout("host slow, retry")
    if rc != 0:
        raise TargetError("could not inspect container Compose labels")
    result = {}
    for line in out.splitlines():
        name, separator, labels = line.strip().partition("\t")
        stack, second, service = labels.partition("\t")
        name = name.lstrip("/")
        if (
            separator and second and _CONTAINER_NAME_RE.fullmatch(name)
            and _NAME_RE.fullmatch(stack) and _NAME_RE.fullmatch(service)
            and ".." not in stack and ".." not in service
        ):
            result[name] = (stack, service)
    return result


class TargetError(Exception):
    """The target was refused; nothing was built or run."""


class TargetTimeout(TargetError):
    """Docker did not answer within the requested deadline."""


@dataclass(frozen=True)
class Target:
    stack: str
    service: str
    container: str

    @property
    def key(self) -> str:
        return f"{self.stack}/{self.service}"

    def as_dict(self) -> dict:
        return {"stack": self.stack, "service": self.service, "container": self.container}


@dataclass(frozen=True)
class StackTarget:
    stack: str

    @property
    def key(self) -> str:
        return f"stack:{self.stack}"

    def as_dict(self) -> dict:
        return {"stack": self.stack}


@dataclass(frozen=True)
class AllStacksTarget:
    stacks: tuple[str, ...]

    @property
    def key(self) -> str:
        return "all"

    def as_dict(self) -> dict:
        return {"scope": "all", "stacks": list(self.stacks)}


def compose_file(stack: str) -> Path:
    return Path(config.STACKS_ROOT) / stack / "docker-compose.yml"


_COMPOSE_SERVICES_CACHE_LIMIT = 256
# `docker compose config --services` also reads include:d files, .env and extends targets, whose
# changes leave the top-level file's mtime and size untouched — a stale entry would refuse a newly
# added service indefinitely (Codex review, T11; reproduced on the test VM). The design keys the
# cache on the compose file's mtime, so keep that and bound how long any entry survives.
COMPOSE_SERVICES_TTL_SECONDS = 60
_compose_services_cache: OrderedDict[tuple[Path, int, int], tuple[frozenset[str], float]] = OrderedDict()
_compose_services_cache_lock = threading.Lock()


def clear_compose_services_cache() -> None:
    """Discard cached service lists (also used to isolate tests)."""
    with _compose_services_cache_lock:
        _compose_services_cache.clear()


def resolve_target(
    stack: str, service: str, *, for_mutation: bool = True, timeout=DOCKER_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Target:
    """`timeout` is one budget for all of resolution's docker calls, not a per-call limit:
    three sequential 4s calls could otherwise outlast the dashboard's 5s RPC deadline
    (Codex review, T14)."""
    for label, value in (("stack", stack), ("service", service)):
        if not isinstance(value, str) or not _NAME_RE.fullmatch(value) or ".." in value:
            raise TargetError(f"invalid {label} name {value!r}")
    if stack in config.FORBIDDEN_STACKS:
        raise TargetError(f"stack {stack!r} is forbidden")
    if for_mutation and any(tok in f"{stack}/{service}".lower() for tok in NETWORK_GUARDED_SUBSTRINGS):
        raise TargetError(f"{stack}/{service} is network-guarded (Traefik/AdGuard); use a reviewed plan")

    compose = compose_file(stack)
    if not compose.is_file():
        raise TargetError(f"no stack named {stack!r} under {config.STACKS_ROOT}")

    deadline = clock() + timeout

    def remaining() -> float:
        left = deadline - clock()
        if left <= 0:
            raise TargetTimeout("host slow, retry")
        return left

    # Docker runs outside the lock; concurrent misses may duplicate reads.
    try:
        resolved = compose.resolve()
        stat = resolved.stat()
    except FileNotFoundError:
        raise TargetError(f"no stack named {stack!r} under {config.STACKS_ROOT}") from None
    cache_key = (resolved, stat.st_mtime_ns, stat.st_size)
    with _compose_services_cache_lock:
        entry = _compose_services_cache.get(cache_key)
    services = entry[0] if entry is not None and clock() - entry[1] <= COMPOSE_SERVICES_TTL_SECONDS else None
    if services is None:
        rc, out, _err = bender.run_argv(
            ["docker", "compose", "-f", str(compose), "config", "--services"], timeout=remaining()
        )
        if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
            raise TargetTimeout("host slow, retry")
        if rc != 0:
            raise TargetError(f"could not read the services of {stack!r}")
        services = frozenset(line.strip() for line in out.splitlines() if line.strip())
        with _compose_services_cache_lock:
            # Keep only the latest inserted version for each resolved file.
            for key in list(_compose_services_cache):
                if key[0] == resolved and key != cache_key:
                    del _compose_services_cache[key]
            _compose_services_cache[cache_key] = (services, clock())
            while len(_compose_services_cache) > _COMPOSE_SERVICES_CACHE_LIMIT:
                _compose_services_cache.popitem(last=False)
    if service not in services:
        raise TargetError(f"stack {stack!r} has no service {service!r}")

    rc, out, _err = bender.run_argv(
        ["docker", "compose", "-f", str(compose), "ps", "-a", "-q", service], timeout=remaining()
    )
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
        raise TargetTimeout("host slow, retry")
    if rc != 0:
        raise TargetError(f"could not list containers for {stack}/{service}")
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    if not ids:
        raise TargetError(f"{stack}/{service} has no container")
    if len(ids) > 1:
        raise TargetError(f"{stack}/{service} is ambiguous: {len(ids)} containers")

    rc, name, _err = bender.run_argv(
        ["docker", "inspect", "--format", "{{.Name}}", ids[0]], timeout=remaining()
    )
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
        raise TargetTimeout("host slow, retry")
    if rc != 0 or not name.strip():
        raise TargetError(f"could not inspect the container for {stack}/{service}")
    container = name.strip().lstrip("/")
    if for_mutation and container in config.PAUSED_CONTAINERS:
        raise TargetError(f"{container} is paused by the operator (paused_containers)")
    return Target(stack=stack, service=service, container=container)


# ── Action registry (landing 1b) ─────────────────────────────────────────────────
# Slice 1 lands only the restart action, through Telegram. The R0 reads (logs, inspect,
# stats) arrive with 1c's dashboard. Actions only BUILD argv; command_service runs it
# through casa_bender.run_argv.
@dataclass(frozen=True)
class ActionSpec:
    name: str
    risk: str
    description: str
    abortable: bool = False
    rollbackable: bool = False
    resumable: bool = False

    def capabilities(self) -> dict[str, bool]:
        return {"abortable": self.abortable, "rollbackable": self.rollbackable, "resumable": self.resumable}


RESTART_SERVICE = "docker.restart_service"
STATS_SERVICE = "docker.stats_service"
UP_STACK = "compose.up_stack"
DOWN_STACK = "compose.down_stack"
UP_ALL = "compose.up_all"
DOWN_ALL = "compose.down_all"
DOWN_INGRESS = "compose.down_ingress"
REGISTRY: dict[str, ActionSpec] = {
    RESTART_SERVICE: ActionSpec(RESTART_SERVICE, "R1", "Restart one compose service",
                                abortable=False, rollbackable=False, resumable=False),
    STATS_SERVICE: ActionSpec(STATS_SERVICE, "R0", "Read CPU and memory for one service"),
    UP_STACK: ActionSpec(UP_STACK, "R1", "Bring one compose stack up"),
    DOWN_STACK: ActionSpec(DOWN_STACK, "R2", "Bring one compose stack down"),
    UP_ALL: ActionSpec(UP_ALL, "R2", "Bring every active compose stack up"),
    DOWN_ALL: ActionSpec(DOWN_ALL, "R3", "Bring every compose stack down"),
    DOWN_INGRESS: ActionSpec(DOWN_INGRESS, "R3", "Bring an ingress compose stack down"),
}

STACK_ACTIONS = frozenset({UP_STACK, DOWN_STACK, UP_ALL, DOWN_ALL, DOWN_INGRESS})
ALL_STACK_ACTIONS = frozenset({UP_ALL, DOWN_ALL})
UP_ACTIONS = frozenset({UP_STACK, UP_ALL})
DOWN_ACTIONS = frozenset({DOWN_STACK, DOWN_ALL, DOWN_INGRESS})


def is_ingress_stack(stack: str) -> bool:
    value = stack.lower()
    return value == "network" or any(token in value for token in NETWORK_GUARDED_SUBSTRINGS)


def _ordered_stack_names(action: str) -> list[str]:
    if action == UP_ALL:
        paths = config.active_stack_dirs()
        return [path.name for path in sorted(paths, key=lambda path: (path.name != "network", path.name))]
    paths = [path.parent for path in sorted(Path(config.STACKS_ROOT).glob("*/docker-compose.yml"))]
    return [path.name for path in sorted(paths, key=lambda path: (path.name == "network", path.name))]


def resolve_stack_target(
    action: str, stack: str, *, approved_target: dict | None = None,
) -> StackTarget | AllStacksTarget:
    """Resolve a compose stack action without calling Docker.

    All-stack targets retain their proposal-time order. Execution only proceeds when the
    current set is identical, so a newly added or removed stack cannot be mutated under an
    approval that did not name it.
    """
    if action not in STACK_ACTIONS:
        raise TargetError(f"unknown stack action {action!r}")
    if action in ALL_STACK_ACTIONS:
        if stack != "all":
            raise TargetError(f"{action} requires the all-stacks target")
        current = _ordered_stack_names(action)
        if approved_target is not None:
            approved = approved_target.get("stacks")
            if (approved_target.get("scope") != "all" or not isinstance(approved, list)
                    or any(not isinstance(name, str) for name in approved)):
                raise TargetError("invalid approved all-stacks target")
            if set(approved) != set(current):
                raise TargetError("stack set changed since approval")
            return AllStacksTarget(tuple(approved))
        return AllStacksTarget(tuple(current))

    if not isinstance(stack, str) or not _NAME_RE.fullmatch(stack) or ".." in stack:
        raise TargetError(f"invalid stack name {stack!r}")
    if stack == "all":
        raise TargetError(f"{action} requires one stack")
    if action == UP_STACK and stack in config.FORBIDDEN_STACKS:
        raise TargetError(f"stack {stack!r} is forbidden")
    ingress = is_ingress_stack(stack)
    if action == DOWN_STACK and ingress:
        raise TargetError(f"stack {stack!r} is ingress; use {DOWN_INGRESS}")
    if action == DOWN_INGRESS and not ingress:
        raise TargetError(f"stack {stack!r} is not ingress; use {DOWN_STACK}")
    if not compose_file(stack).is_file():
        raise TargetError(f"no stack named {stack!r} under {config.STACKS_ROOT}")
    return StackTarget(stack)


def action_summary(action: str, target: dict) -> str:
    if action == RESTART_SERVICE:
        return f"Restart {target['stack']}/{target['service']}"
    direction = "up" if action in UP_ACTIONS else "down"
    if target.get("scope") == "all":
        return f"Bring every stack {direction}"
    return f"Bring {target['stack']} {direction}"


def stack_argv(action: str, stack: str) -> list[str]:
    if action in UP_ACTIONS:
        verb = ["up", "-d"]
    elif action in DOWN_ACTIONS:
        verb = ["down"]
    else:
        raise ValueError(f"not a compose stack action: {action}")
    return ["docker", "compose", "-f", str(compose_file(stack)), *verb]


def stack_container_ids(stack: str, *, timeout=DOCKER_TIMEOUT_SECONDS) -> tuple[int, list[str]]:
    """The project's containers by NAME (inspectable like IDs, and readable in the failure reasons
    that reach Telegram and the dashboard; 64-hex IDs were not — VM rehearsal, T37)."""
    rc, out, _err = bender.run_argv(
        ["docker", "compose", "-f", str(compose_file(stack)), "ps", "-a", "--format", "{{.Name}}"],
        timeout=timeout,
    )
    return rc, [line.strip() for line in out.splitlines() if line.strip()]


STACK_VERIFY_TIMEOUT_SECONDS = 180


def verify_stack_up(
    stack: str, *, timeout: float = STACK_VERIFY_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS, stable_seconds: float = VERIFY_STABLE_SECONDS,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    start = clock()
    rc, containers = stack_container_ids(stack, timeout=max(0, timeout - (clock() - start)))
    if rc != 0:
        return False, "could not list containers after up"
    if not containers:
        return False, "no containers started"
    baselines: dict[str, int] = {}
    good_since: float | None = None
    while True:
        now = clock()
        all_good = True
        waiting = []
        for container in containers:
            remaining = timeout - (clock() - start)
            if remaining <= 0:
                return False, f"not healthy within {int(timeout)}s (verification timed out)"
            reading = read_health(container, timeout=remaining)
            if reading.error is not None:
                return False, f"{container}: inspect failed: {reading.error}"
            baseline = baselines.setdefault(container, reading.restart_count)
            if reading.status != "running":
                return False, f"{container}: status={reading.status}"
            if reading.health == "unhealthy":
                return False, f"{container}: healthcheck failing"
            increase = reading.restart_count - baseline
            if increase >= 1:
                return False, f"{container}: restarted {increase}x during verification"
            if reading.health not in ("healthy", "none"):
                all_good = False
                waiting.append(f"{container}={reading.health}")
        if all_good:
            good_since = now if good_since is None else good_since
            if now - good_since >= stable_seconds:
                return True, f"all {len(containers)} containers stable for {int(now - good_since)}s"
        else:
            good_since = None
        if now - start >= timeout:
            detail = ", ".join(waiting) or "stability window not reached"
            return False, f"not healthy within {int(timeout)}s ({detail})"
        sleep(min(poll_seconds, max(0, timeout - (now - start))))


def verify_stack_down(
    stack: str, *, timeout: float = STACK_VERIFY_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS, clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    start = clock()
    while True:
        remaining = timeout - (clock() - start)
        if remaining <= 0:
            return False, f"containers still present after {int(timeout)}s"
        rc, containers = stack_container_ids(stack, timeout=remaining)
        if rc != 0:
            return False, "could not list containers after down"
        if not containers:
            return True, "no containers remain"
        now = clock()
        if now - start >= timeout:
            return False, f"containers still present after {int(timeout)}s: {', '.join(containers)}"
        sleep(min(poll_seconds, max(0, timeout - (now - start))))


def restart_argv(target: Target) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file(target.stack)), "restart", target.service]


# ── canary image references (slice 5b-3) ────────────────────────────────────────
def normalize_image_id(image_id: str) -> str:
    """`compose images -q` prints bare hex; `image inspect --format {{.Id}}` prints `sha256:<hex>`.
    Compared raw they never match, and every service then looks updated (Zoidberg, live)."""
    return image_id.strip().removeprefix("sha256:")


def compose_images_argv(stack: str, service: str) -> list[str]:
    """The image id the RUNNING container for this service uses — the rollback target."""
    return ["docker", "compose", "-f", str(compose_file(stack)), "images", "-q", service]


def compose_config_images_argv(stack: str, service: str) -> list[str]:
    """The image reference this service resolves to per its compose config (not what runs)."""
    return ["docker", "compose", "-f", str(compose_file(stack)), "config", "--images", service]


def image_id_argv(reference: str) -> list[str]:
    """What a reference currently resolves to in the local image cache."""
    return ["docker", "image", "inspect", "--format", "{{.Id}}", reference]


def container_image_id_argv(container: str) -> list[str]:
    return ["docker", "inspect", "--format", "{{.Image}}", container]


# A canary update needs a canonical **mutable** reference: `name:tag`, optionally registry- and
# namespace-qualified. `repo@sha256:…` pins a digest (there is no tag to move) and a build-only
# service has no reference at all — both are ineligible (design §4.5).
CANARY_REFERENCE_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*:[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$")


def is_canary_reference(reference: str | None) -> bool:
    """Eligible **and** storable: the same bound the step's output model applies, so a reference
    this accepts can never fail validation after a successful deploy (Codex, T42)."""
    return (bool(reference) and 3 <= len(reference) <= IMAGE_REFERENCE_MAX
            and ".." not in reference and bool(CANARY_REFERENCE_RE.fullmatch(reference)))


def compose_pull_argv(stack: str, service: str) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file(stack)), "pull", service]


def compose_up_pinned_argv(stack: str, service: str) -> list[str]:
    """Recreate one service from the image the reference already points to locally — never a fresh
    pull, so phase 2 deploys exactly the image phase 1 resolved (design §4.5)."""
    return ["docker", "compose", "-f", str(compose_file(stack)), "up", "-d", "--pull", "never",
            service]


def tag_argv(image_id: str, reference: str) -> list[str]:
    return ["docker", "tag", image_id, reference]


STATS_FORMAT = "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
# Docker prints binary units for sizes and decimal ones in some versions; hosts with at least
# 1 TiB of memory print TiB limits (Codex review, T14).
_MEMORY_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4, "PiB": 1024**5,
                 "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "PB": 1000**5}


def stats_argv(container: str) -> list[str]:
    return ["docker", "stats", "--no-stream", "--format", STATS_FORMAT, container]


def parse_stats(out: str) -> dict | None:
    def percent(value):
        if not value.strip().endswith("%"):
            raise ValueError("missing percent")
        number = float(value.strip()[:-1])
        if not math.isfinite(number) or number < 0:
            raise ValueError("invalid percent")
        return number

    def memory(value):
        match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB|PiB|kB|MB|GB|TB|PB)\s*", value)
        if match is None:
            raise ValueError("invalid memory")
        return int(float(match[1]) * _MEMORY_UNITS[match[2]])

    try:
        cpu, usage, mem = out.strip().split("\t")
        used, limit = usage.split("/")
        return {"cpu_percent": percent(cpu), "memory_percent": percent(mem),
                "memory_used_bytes": memory(used), "memory_limit_bytes": memory(limit)}
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def read_stats(container: str, *, timeout) -> dict:
    rc, out, _err = bender.run_argv(stats_argv(container), timeout=timeout)
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
        return {"ok": False, "error": "timeout"}
    stats = parse_stats(out) if rc == 0 else None
    if stats is None:
        return {"ok": False, "error": "unavailable"}
    return {"ok": True, "stats": stats}


def restart_count(container_name: str) -> int | None:
    reading = read_health(container_name)
    return None if reading.error is not None else reading.restart_count


# ── Verifier (landing 1b) ────────────────────────────────────────────────────────
def verify_after_restart(
    container_name: str,
    baseline_restarts: int | None,
    *,
    timeout: float = VERIFY_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    stable_seconds: float = VERIFY_STABLE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Verify from container state, never from the restart command's exit code.

    Fails at the first poll that shows the container not running, unhealthy, or restarted
    beyond `baseline_restarts` (the count taken before the restart; `docker compose restart`
    itself doesn't increment it). Passes once it has been running with health "healthy"
    (or no healthcheck) for `stable_seconds`. A healthcheck still "starting" keeps waiting;
    never reaching healthy within `timeout` fails."""
    start = clock()
    good_since: float | None = None
    baseline = baseline_restarts
    while True:
        sleep(poll_seconds)
        now = clock()
        reading = read_health(container_name)
        if reading.error is not None:
            return False, f"inspect failed: {reading.error}"
        if baseline is None:
            baseline = reading.restart_count
        if reading.status != "running":
            return False, f"status={reading.status}"
        if reading.health == "unhealthy":
            return False, "healthcheck failing"
        restarts = reading.restart_count - baseline
        if restarts >= 1:
            return False, f"restarted {restarts}x during verification"
        if reading.health in ("healthy", "none"):
            good_since = now if good_since is None else good_since
            if now - good_since >= stable_seconds:
                detail = "healthy" if reading.health == "healthy" else "running, no healthcheck"
                return True, f"{detail} for {int(now - good_since)}s"
        else:
            good_since = None
        if now - start >= timeout:
            return False, f"not healthy within {int(timeout)}s (health={reading.health})"


# A request may carry back at most this many line hashes; a response never returns more. A batch
# holds <= 500 lines, and merged hashes stay capped here, so any response is a valid next request.
LOG_CURSOR_HASH_LIMIT = 1000
LOG_RESPONSE_BUDGET_BYTES = 256 * 1024
# Newest bytes kept per stream while capturing `docker logs`: 500 records at the 4 KiB per-line cap
# is ~2 MiB, so 4 MiB never truncates a normal tail but bounds a pathological one.
LOG_CAPTURE_MAX_BYTES = 4 * 1024 * 1024
LOG_MAX_LINES = 500
LOG_MAX_LINE_BYTES = 4096
# Records parsed per call before the newest are kept: `--tail 500` bounds real records, but
# continuation lines are unbounded, and every one costs a redact() pass.
LOG_MAX_RECORDS = 2000
LOG_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
FACTS_FORMAT = (
    "{{.State.Status}}\t{{.State.StartedAt}}\t"
    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}\t"
    "{{if .State.Health}}{{.State.Health.FailingStreak}}{{else}}0{{end}}\t"
    "{{.RestartCount}}\t{{.HostConfig.RestartPolicy.Name}}\t"
    "{{.HostConfig.RestartPolicy.MaximumRetryCount}}\t"
    "{{json .NetworkSettings.Ports}}\t{{.Image}}"
)


def read_facts(container: str, *, timeout) -> dict:
    if timeout <= 0:
        return {"ok": False, "error": "timeout"}
    rc, out, _err = bender.run_argv(
        ["docker", "inspect", "--format", FACTS_FORMAT, container], timeout=timeout,
    )
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
        return {"ok": False, "error": "timeout"}
    if rc != 0:
        return {"ok": False, "error": "unavailable"}
    try:
        state, started, health, streak, restarts, policy, retries, ports_json, image = out.strip().split("\t")
        if health not in {"healthy", "unhealthy", "starting", "none"}:
            raise ValueError("invalid health")
        ports = []
        for port, bindings in (json.loads(ports_json) or {}).items():
            number, protocol = port.split("/")
            for binding in bindings or []:
                ports.append({"container_port": number, "protocol": protocol,
                              "host_ip": binding["HostIp"], "host_port": binding["HostPort"]})
        facts = {"state": state, "started_at": started, "health": health,
                 "failing_streak": int(streak), "restart_count": int(restarts),
                 "restart_policy": {"name": policy, "max_retries": int(retries)},
                 "ports": ports, "image_id": image}
    except (ValueError, TypeError, AttributeError, KeyError):
        return {"ok": False, "error": "unavailable"}
    return {"ok": True, "facts": facts}


# ── Which container declares which Traefik router, for launch links (T46.1) ─────
# A router is joined to its container by the names Traefik's docker provider gives it, which
# come from the container's own configuration: `traefik.http.routers.<name>.*` and
# `traefik.http.services.<name>.*` labels, or -- where there are none -- the default router
# and service Traefik names after the compose service and project. The dashboard emits a
# link only when one container declares both the router and the service it forwards to.
# None of that changes while a container lives except by `docker rename`, so the cache
# keys on id AND name. (The first version joined by backend IP address; four review rounds
# each found another way an address could be momentarily held by the wrong container.)
# .State.Status, not .State.Running: the latter is also true for paused and restarting
# containers, and the listing's {{.State}} is the Status -- the two must agree to be a key.
ROUTERS_FORMAT = ("{{.Name}}\t{{.Id}}\t{{.State.Status}}\t{{.HostConfig.NetworkMode}}\t"
                  "{{json .Config.Labels}}")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
# Docker's short id is 12 hex characters. A shorter hex ref is at least as likely to be an old
# container name (`db`, `cafe`) as an id, so it is never resolved as a prefix.
_ID_PREFIX_RE = re.compile(r"[0-9a-f]{12,64}")
# Paused and restarting containers are still in Traefik's (non `-a`) container list, so they
# still make a name ambiguous -- but a button onto one would not open.
_CLAIMING_STATUSES = {"running", "paused", "restarting"}


class UnresolvableNamespace(ValueError):
    """A sharer's network owner cannot be identified. Retrying the same read will not help."""


# Traefik's label parser matches the path case-insensitively and keeps the name's own case.
_HTTP_ROUTER_RE = re.compile(r"traefik\.http\.routers\.([^.]+)\.", re.IGNORECASE)
_HTTP_SERVICE_RE = re.compile(r"traefik\.http\.services\.([^.]+)\.", re.IGNORECASE)
# Traefik goes TCP/UDP-only -- no default HTTP router or service -- when a container has TCP or
# UDP routers or services and no HTTP routers, middlewares or services.
_HTTP_CONFIG_RE = re.compile(r"traefik\.http\.(routers|middlewares|services)\.", re.IGNORECASE)
_TCP_UDP_CONFIG_RE = re.compile(r"traefik\.(tcp|udp)\.(routers|services)\.", re.IGNORECASE)
_GO_FALSE = {"0", "f", "F", "false", "FALSE", "False"}   # strconv.ParseBool
# A labelled service that names its own server URL forwards wherever that says -- another
# machine, or another container -- so which row its router belongs on is not known.
_SERVER_URL_RE = re.compile(r"traefik\.http\.services\.([^.]+)\.loadbalancer\.server\.url$",
                            re.IGNORECASE)
# Likewise a weighted, mirroring or failover service forwards to other services, which may be
# declared on other containers. Only a plain loadBalancer service is known to be this one.
_NOT_LOADBALANCER_RE = re.compile(r"traefik\.http\.services\.([^.]+)\.(?!loadbalancer\.)",
                                  re.IGNORECASE)
_COMPOSE_SERVICE = "com.docker.compose.service"
_COMPOSE_PROJECT = "com.docker.compose.project"
_COMPOSE_ONEOFF = "com.docker.compose.oneoff"


def traefik_normalise(name: str) -> str:
    """Traefik's provider.Normalize: runs of anything but letters and digits become one `-`."""
    return "-".join(part for part in re.split(r"[\W_]+", name) if part)


def _declared(name: str, labels: dict) -> tuple[set[str], set[str], set[str]]:
    """(http routers, http services, services to withhold) this container gives Traefik's
    docker provider.

    Mirrors the provider: no routers or services at all for `traefik.enable=false`; a default
    service when none is labelled; a default router only when none is labelled, the container
    is not TCP/UDP-only, and it has at most one service. Default names are
    Normalize(`<compose service>_<compose project>`) when both labels are present, else of
    the container name.
    """
    enable = [str(v).strip() for k, v in labels.items() if k.lower() == "traefik.enable"]
    # Traefik merges case-variant keys and one of them wins; only when every spelling says
    # false is the container certainly disabled. Otherwise it stays a claimant (fail closed).
    if enable and all(v in _GO_FALSE for v in enable):
        return set(), set(), set()
    routers = {m.group(1) for key in labels if (m := _HTTP_ROUTER_RE.match(key))}
    services = {m.group(1) for key in labels if (m := _HTTP_SERVICE_RE.match(key))}
    if _COMPOSE_SERVICE in labels and _COMPOSE_PROJECT in labels:
        default = traefik_normalise(f"{labels[_COMPOSE_SERVICE]}_{labels[_COMPOSE_PROJECT]}")
    else:
        default = traefik_normalise(name)
    withheld = {m.group(1) for key in labels
                if (m := _SERVER_URL_RE.match(key)) or (m := _NOT_LOADBALANCER_RE.match(key))}
    tcp_udp_only = (any(_TCP_UDP_CONFIG_RE.match(k) for k in labels)
                    and not any(_HTTP_CONFIG_RE.match(k) for k in labels))
    if tcp_udp_only or not default:
        return routers, services, withheld
    if not services:
        services = {default}
    if not routers and len(services) == 1:
        routers = {default}
    return routers, services, withheld


def parse_router_owners(out: str, ids: list[str]) -> dict:
    """{"routers": {router: container}, "services": {service: container}, "containers":
    [[id, name, status], ...]} for every name exactly one running container declares.

    `ids` is every container, running or not: only running ones declare anything (Traefik
    ignores the rest), but a stopped one can still be sharing another's network namespace.
    Raises ValueError on anything short of a complete, well-formed read: a missing container
    would leave a name another container also declares looking unique.

    A container whose network namespace another container shares (gluetun, with qbittorrent
    in `network_mode: service:gluetun`) declares nothing here. Its labels are commonly the
    sharers' routers -- qbit's labels have to live on gluetun -- so a router on it could be
    any of theirs, and its row is the wrong place for qbit's button. `links:` places those.
    """
    rows = []
    # Split on "\n" only: str.splitlines() also breaks on U+0085 and friends, which Go's JSON
    # does not escape, so one label value could split a record (same rule as the log reader).
    for line in (out or "").split("\n"):
        if not line:
            continue
        parts = line.split("\t", 4)
        if len(parts) != 5:
            raise ValueError("malformed router line")
        name, container_id, status, network_mode, labels_json = parts
        name = name.lstrip("/")
        if not name or not _CONTAINER_ID_RE.fullmatch(container_id) or not status:
            raise ValueError("malformed router line")
        running = status == "running"
        claiming = status in _CLAIMING_STATUSES
        labels = json.loads(labels_json) if labels_json not in ("", "null") else {}
        if not isinstance(labels, dict):
            raise ValueError("malformed labels")
        rows.append((name, container_id, running, claiming, network_mode, labels, status))
    if sorted(row[1] for row in rows) != sorted(ids):
        raise ValueError("inspect did not answer for exactly the listed containers")

    by_id = {row[1]: row[0] for row in rows}
    names = set(by_id.values())
    shared = set()
    for sharer, _, _, _, network_mode, _, _ in rows:
        if not network_mode.startswith("container:"):
            continue
        ref = network_mode.partition(":")[2]
        # Docker's own order: a full id, then a name, then a unique id prefix. A name that
        # happens to be hex (`db`, `cafe`) is a name, never a prefix of someone else's id.
        if ref in by_id:
            shared.add(by_id[ref])
            continue
        prefixed = ([n for cid, n in by_id.items() if cid.startswith(ref)]
                    if ref not in names and _ID_PREFIX_RE.fullmatch(ref) else [])
        if len(prefixed) == 1:
            shared.add(prefixed[0])
        else:
            # Every container is listed, so an id that matches none means its owner was
            # recreated alone -- and the new owner, carrying the sharer's labels, cannot be told
            # apart. A name is worse: Docker resolves it only at start, so after a rename it can
            # name a different container than the one whose namespace this is. Either way,
            # refuse the read rather than trust it -- and say which container, because until it
            # is recreated or removed there are no derived links on this host.
            log.warning("Launch links withheld: %s shares the network of %r, which cannot be "
                        "identified. Remove %s, or recreate it through compose "
                        "(network_mode: service:...), which records the owner by id",
                        sharer, ref, sharer)
            raise UnresolvableNamespace("unresolvable network namespace owner")

    router_claims: dict[str, set] = {}
    service_claims: dict[str, set] = {}
    for name, _, running, claiming, _, labels, _ in rows:
        if not claiming:
            continue
        routers, services, withheld = _declared(name, labels)
        # A network-namespace owner, a `docker compose run` one-off (it carries its service's
        # labels and Traefik load-balances to it), and a paused or restarting container are
        # still counted as claimants -- so a name they share stays ambiguous -- but never as
        # an owner.
        distrusted = (not running or name in shared
                      or str(labels.get(_COMPOSE_ONEOFF, "")).strip().lower() == "true")
        routers = {(r, None if distrusted else name) for r in routers}
        withheld_folded = {w.lower() for w in withheld}
        services = {(s, None if distrusted or s.lower() in withheld_folded else name)
                    for s in services}
        for router, owner in routers:
            router_claims.setdefault(router, set()).add(owner)
        for service, owner in services:
            service_claims.setdefault(service, set()).add(owner)
    # A name two containers declare (scaled replicas, a copy-pasted label) is one Traefik
    # merges or rejects; which of them a button should open is unknown, so it opens none.
    return {"routers": {r: n.pop() for r, n in router_claims.items() if len(n) == 1 and None not in n},
            "services": {s: n.pop() for s, n in service_claims.items() if len(n) == 1 and None not in n},
            "containers": sorted([row[1], row[0], row[6]] for row in rows)}


def list_containers(*, timeout) -> dict:
    """{"ok": True, "containers": [[id, name, status], ...]} for every container, sorted, or a
    typed error. Cheap, and exactly what tells the router cache whether a container was
    created, recreated, renamed, started or stopped since the last inspect."""
    if timeout <= 0:
        return {"ok": False, "error": "timeout"}
    try:
        rc, out, _err = bender.run_argv(
            ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}\t{{.State}}"],
            timeout=timeout)
    except UnicodeDecodeError:
        return {"ok": False, "error": "unavailable"}
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
        return {"ok": False, "error": "timeout"}
    if rc != 0:
        return {"ok": False, "error": "unavailable"}
    containers = []
    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            return {"ok": False, "error": "unavailable"}
        container_id, names, state = parts
        # Legacy `--link` aliases make this `app,web/app`; the container's own name is the one
        # without a slash, which is what `docker inspect` calls it.
        own = [n for n in names.split(",") if n and "/" not in n]
        if not _CONTAINER_ID_RE.fullmatch(container_id) or len(own) != 1 or not state:
            return {"ok": False, "error": "unavailable"}
        # {{.State}} here is the inspect's .State.Status: the two must key identically, and on
        # the status itself -- a bool would let `restarting` and `exited` share a key.
        containers.append([container_id, own[0], state])
    return {"ok": True, "containers": sorted(containers)}


def read_router_owners(ids: list[str], *, timeout) -> dict:
    """{"ok": True, "routers": {...}, "services": {...}, "containers": [...]} for exactly these
    containers, or a typed error. One `docker inspect`. Read-only.

    A container *removed* between the listing and this read fails the inspect; that is tagged
    "race", as is ordinary churn the caller detects by comparing "containers". A sharer whose
    network owner cannot be identified is tagged "final": reading again will not change it.
    """
    if not all(isinstance(i, str) and _CONTAINER_ID_RE.fullmatch(i) for i in ids):
        return {"ok": False, "error": "unavailable"}
    if not ids:
        return {"ok": True, "routers": {}, "services": {}, "containers": []}
    if timeout <= 0:
        return {"ok": False, "error": "timeout"}
    try:
        rc, out, _err = bender.run_argv(
            ["docker", "inspect", "--type", "container", "--format", ROUTERS_FORMAT, *ids],
            timeout=timeout)
    except UnicodeDecodeError:
        return {"ok": False, "error": "unavailable"}
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
        return {"ok": False, "error": "timeout"}
    if rc != 0:
        # Only a container removed since the listing is churn worth a free retry; any other
        # error will repeat, and should back off at once.
        return {"ok": False, "error": "unavailable", **({"race": True} if "No such" in _err else {})}
    try:
        return {"ok": True, **parse_router_owners(out, ids)}
    except UnresolvableNamespace:
        return {"ok": False, "error": "unavailable", "final": True}
    except (ValueError, TypeError, RecursionError):
        return {"ok": False, "error": "unavailable"}


# ── Where a container's widget API is, for the dashboard's widget fetcher (T46.3) ─────
# Core resolves; the dashboard fetches. Core has docker and sudo and should not also parse
# third-party HTTP responses, so all it hands over is which widget and which addresses.
#
# Only addresses on docker BRIDGE networks. The fetcher sends a credential to whatever address
# it is given, and a macvlan or ipvlan address is on the physical LAN: the host cannot reach
# its own macvlan children, so a request to one goes out on the wire -- ARP for it answered
# by anything on the LAN, the credential sent in cleartext to whoever does.
WIDGET_TARGET_FORMAT = ("{{.Id}}\t{{.Image}}\t{{.Config.Image}}\t{{.State.Status}}\t"
                        "{{.HostConfig.NetworkMode}}\t{{json .Config.Labels}}\t"
                        "{{json .NetworkSettings.Networks}}")
_OWNER_NETWORKS_FORMAT = "{{.Id}}\t{{json .NetworkSettings.Networks}}"
WIDGET_LABEL = "planetexpress.widget"


def _run_docker(argv, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        return {"ok": False, "error": "timeout"}, ""
    try:
        rc, out, _err = bender.run_argv(argv, timeout=left)
    except UnicodeDecodeError:
        return {"ok": False, "error": "unavailable"}, ""
    if rc == bender.RUN_ARGV_TIMEOUT_EXIT:
        return {"ok": False, "error": "timeout"}, ""
    if rc != 0:
        return {"ok": False, "error": "unavailable"}, ""
    return None, out


def _json_map(text):
    try:
        value = json.loads(text) if text not in ("", "null") else {}
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def read_widget_target(container: str, *, timeout) -> dict:
    """{"ok": True, "id", "image", "running", "label", "label_from_image", "pulled_from",
    "addresses"} for one container, or a typed error. Read-only: the container, its image's
    labels and registry digests, its network-namespace owner when it shares one, and its
    networks' drivers.

    `label_from_image` says the planetexpress.widget label is baked into the image rather than
    set on the container. It matters because a label can select a keyed widget: an image that
    shipped `planetexpress.widget=sonarr` would otherwise make the dashboard send the Sonarr
    key to whatever that image runs.
    """
    if not isinstance(container, str) or not _CONTAINER_NAME_RE.fullmatch(container):
        return {"ok": False, "error": "unavailable"}
    if timeout <= 0:
        return {"ok": False, "error": "timeout"}
    deadline = time.monotonic() + timeout
    failed, out = _run_docker(["docker", "inspect", "--type", "container", "--format",
                               WIDGET_TARGET_FORMAT, container], deadline)
    if failed:
        return failed
    parts = out.split("\t")
    if len(parts) != 7:
        return {"ok": False, "error": "unavailable"}
    container_id, image_id, image, status, network_mode, labels_json, networks_json = parts
    labels, networks = _json_map(labels_json), _json_map(networks_json)
    if (labels is None or networks is None or not _CONTAINER_ID_RE.fullmatch(container_id)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)):
        return {"ok": False, "error": "unavailable"}

    label = labels.get(WIDGET_LABEL)
    # The image's own labels, and where it was pulled from. A registry digest names its
    # registry: a compose `image: evil.example/linuxserver/sonarr` says evil.example. With
    # the classic image store a local `build:` has no digest at all; with the containerd
    # store it gets one per tag (see registry.trusted_provenance for what that leaves).
    failed, out = _run_docker(["docker", "image", "inspect", "--format",
                               "{{json .Config.Labels}}\t{{json .RepoDigests}}", image_id], deadline)
    if failed and failed["error"] == "timeout":
        return failed
    image_labels, digests = None, []
    if not failed:
        halves = out.split("\t", 1)
        image_labels = _json_map(halves[0])
        try:
            parsed = json.loads(halves[1]) if len(halves) == 2 and halves[1] != "null" else []
        except (ValueError, RecursionError):
            parsed = []
        digests = [d.split("@", 1)[0] for d in parsed if isinstance(d, str) and "@sha256:" in d] \
            if isinstance(parsed, list) else []
    # Unreadable: cannot tell where the label came from, so treat it as the image's.
    label_from_image = label is not None and (image_labels is None
                                              or image_labels.get(WIDGET_LABEL) == label)

    if network_mode.startswith("container:"):
        # A sharer (qbittorrent in gluetun's namespace) has no networks of its own; its API is
        # on the owner's addresses, which ARE its own namespace's. Followed by full id only,
        # and only if the owner answering is that id: a name is resolved when asked, and after
        # a rename it names some other container -- whose addresses would get this widget's key.
        ref = network_mode.partition(":")[2]
        networks = {}
        if _CONTAINER_ID_RE.fullmatch(ref):
            failed, out = _run_docker(["docker", "inspect", "--type", "container", "--format",
                                       _OWNER_NETWORKS_FORMAT, ref], deadline)
            if failed and failed["error"] == "timeout":
                return failed
            owner = [] if failed else out.split("\t", 1)
            if len(owner) == 2 and owner[0] == ref:
                networks = _json_map(owner[1]) or {}

    by_network = {}
    for value in networks.values():
        if isinstance(value, dict) and isinstance(value.get("NetworkID"), str) \
                and _CONTAINER_ID_RE.fullmatch(value["NetworkID"]) and value.get("IPAddress"):
            by_network.setdefault(value["NetworkID"], []).append(str(value["IPAddress"]))
    addresses = []
    if by_network:
        failed, out = _run_docker(["docker", "network", "inspect", "--format",
                                   "{{.Id}}\t{{.Driver}}", *sorted(by_network)], deadline)
        if failed:
            return failed
        for line in out.split("\n"):
            network_id, _, driver = line.partition("\t")
            if driver == "bridge" and network_id in by_network:
                addresses.extend(by_network[network_id])
    return {"ok": True, "id": container_id, "image": image, "running": status == "running",
            "label": label if isinstance(label, str) else None,
            "label_from_image": label_from_image, "pulled_from": sorted(set(digests)),
            "shares_namespace": network_mode.startswith("container:"),
            "addresses": sorted(set(addresses))}


def logs_argv(container: str, cursor: str | None) -> list[str]:
    if cursor is not None and not LOG_TIMESTAMP_RE.fullmatch(cursor):
        raise ValueError("invalid cursor")
    return ["docker", "logs", "--timestamps", "--tail", "500",
            *(["--since", cursor] if cursor else []), container]


def _log_hash(ts: str, text: str) -> str:
    return hashlib.sha256((ts + "\0" + text).encode()).hexdigest()[:16]


def _timestamp_key(ts: str) -> str:
    # Fraction widths vary; lexical ordering of the original strings is incorrect.
    seconds, _, fraction = ts[:-1].partition(".")
    return seconds + fraction.ljust(9, "0")


def read_logs(container: str, *, cursor, cursor_hashes, timeout) -> dict:
    deadline = time.monotonic() + timeout
    if timeout <= 0:
        return {"ok": False, "error": "timeout"}
    rc, out, err, truncated = bender.run_argv_bounded(
        logs_argv(container, cursor), timeout=timeout, max_bytes=LOG_CAPTURE_MAX_BYTES,
    )
    if rc != 0:
        return {"ok": False, "error": "timeout" if rc == bender.RUN_ARGV_TIMEOUT_EXIT else "unavailable"}

    # Pass 1 — split into records, cheaply, without touching their text. Split on "\n" ONLY:
    # str.splitlines() also breaks on \r, \v, \f and U+2028/2029, which Docker does not use as
    # record separators, so one record became two and redact() only saw the first half — leaking the
    # rest of a secret (Codex review, T29).
    # Bounded while parsing, not after: an output of one timestamp plus a million newlines reached
    # ~218 MB of records before the cap applied (Codex review, T29). A deque keeps the newest.
    records: deque = deque(maxlen=LOG_MAX_RECORDS)
    dropped = False
    stamped = 0
    for stream, output in (("stdout", out), ("stderr", err)):
        previous = None
        pieces = output.split("\n")
        if pieces and pieces[-1] == "":
            # The terminal newline's sentinel, not a record: keeping it fabricated a blank log entry
            # on every poll (Codex review, T29). A real blank record is a bare timestamp line.
            pieces.pop()
        for raw in pieces:
            raw = raw.removesuffix("\r")
            ts, separator, text = raw.partition(" ")
            if not separator and LOG_TIMESTAMP_RE.fullmatch(raw):
                # A blank entry: run_argv_bounded keeps trailing content, but a record can still be
                # just a timestamp.
                separator, text = " ", ""
            if separator and LOG_TIMESTAMP_RE.fullmatch(ts):
                previous = ts
                stamped += 1
            elif previous is not None:
                ts, text = previous, raw
            else:
                continue  # No timestamp to attach an initial malformed line to.
            if len(records) == LOG_MAX_RECORDS:
                dropped = True
            records.append((ts, text, stream))

    skipped = truncated or dropped
    # `docker logs --tail 500` drops older records BEFORE we see them: if it returned its full 500,
    # there may have been more since the cursor, so the gap must be shown. Docker applies the tail to
    # the merged log, so count timestamped records across both streams.
    if stamped >= 500:
        skipped = True
    records = sorted(records, key=lambda record: _timestamp_key(record[0]))

    # Pass 2 — redact NEWEST FIRST, checking the deadline. redact() costs ~30µs per 8 KiB of text,
    # so a few hundred long records can outlast the caller's 5s RPC deadline while it holds a worker
    # (Codex review, T29). Stopping newest-first keeps the lines the operator is actually reading and
    # marks the gap, instead of blowing the deadline or returning the oldest slice.
    known = set(cursor_hashes)
    kept, size = [], 0
    for ts, text, stream in reversed(records):
        if time.monotonic() >= deadline:
            skipped = True
            break
        text = redact(text.rstrip())
        encoded = text.encode()
        if len(encoded) > LOG_MAX_LINE_BYTES:
            # Shortening one long line is marked by its trailing ellipsis; it is not "earlier lines
            # skipped", which the UI renders as a gap marker.
            text = encoded[:LOG_MAX_LINE_BYTES - 3].decode("utf-8", errors="ignore") + "…"
        line = {"ts": ts, "text": text, "stream": stream}
        if ts == cursor and _log_hash(ts, text) in known:
            continue
        # Budget the SERIALIZED size with the transport's own settings: the RPC frame caps a response
        # at 1 MiB of JSON, control characters (ESC in coloured output) expand to six-byte escapes,
        # and "é" is 6 bytes with ensure_ascii on — raw text length undercounts badly.
        length = len(json.dumps(line).encode()) + 1   # + the list separator
        if len(kept) == LOG_MAX_LINES or size + length > LOG_RESPONSE_BUDGET_BYTES:
            skipped = True
            break
        kept.append(line)
        size += length
    lines = kept[::-1]
    newest = lines[-1]["ts"] if lines else cursor
    hashes = [_log_hash(line["ts"], line["text"]) for line in lines if line["ts"] == newest]
    if newest == cursor:
        # The cursor didn't move: lines already seen at it are still "seen". Returning only this
        # batch's hashes made an all-duplicate poll forget them and replay them next time
        # (Codex review, T29). Newest additions last, capped to what a request may carry.
        hashes = list(dict.fromkeys([*cursor_hashes, *hashes]))[-LOG_CURSOR_HASH_LIMIT:]
    started = None
    left = deadline - time.monotonic()
    if left > 0:
        rc, value, _err = bender.run_argv(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", container], timeout=left,
        )
        if rc == 0:
            started = value.strip() or None
    return {"ok": True, "lines": lines, "cursor": newest, "cursor_hashes": hashes,
            "skipped": skipped, "started_at": started}
