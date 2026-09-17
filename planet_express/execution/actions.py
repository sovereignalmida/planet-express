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

# Same as casa_zoidberg._run()'s default, which these checks used before the extraction.
# A shorter value turns a slow or remote daemon into a "missing/unhealthy" verdict and a
# false canary rollback (Codex review, landing 1a). Latency-sensitive callers (the 1b RPC
# read path) pass their own shorter timeout instead of lowering this.
DOCKER_TIMEOUT_SECONDS = 120
RPC_DOCKER_TIMEOUT_SECONDS = 4
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


@dataclass(frozen=True)
class HealthReading:
    error: str | None  # set when docker inspect itself failed
    status: str
    restart_count: int
    health: str  # "healthy" | "unhealthy" | "starting" | "none"


def read_health(container_name: str) -> HealthReading:
    """One `docker inspect` for status, RestartCount and health (with the Health guard)."""
    rc, out, err = bender.run_argv(
        ["docker", "inspect", "--format", _HEALTH_FORMAT, container_name],
        timeout=DOCKER_TIMEOUT_SECONDS,
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
REGISTRY: dict[str, ActionSpec] = {
    RESTART_SERVICE: ActionSpec(RESTART_SERVICE, "R1", "Restart one compose service",
                                abortable=False, rollbackable=False, resumable=False),
    STATS_SERVICE: ActionSpec(STATS_SERVICE, "R0", "Read CPU and memory for one service"),
}


def restart_argv(target: Target) -> list[str]:
    return ["docker", "compose", "-f", str(compose_file(target.stack)), "restart", target.service]


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
VERIFY_TIMEOUT_SECONDS = 90
VERIFY_STABLE_SECONDS = 15


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
