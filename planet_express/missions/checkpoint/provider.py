"""The checkpoint provider seam (contract §7, v1).

`HostControlProvider` covers only units, logs and metrics, so checkpoints get their own versioned protocol:
compose-file discovery, container inspect, each service's effective compose config (as a hash), host observations, and
the two actions a restore may ask for: `up` (`compose up -d --no-deps` for named services, never `--force-recreate`: Compose
recreates only a container whose config changed) and `restart`. Both implementations share the Docker CLI mechanics (argv only,
through `casa_bender.run_argv`, no shell string anywhere) and differ in the compose binary, where unit files live and which stack
orchestrator they are built on. Reads are the only Docker calls made here except `up` and `restart`, which a restore asks for
explicitly.
"""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol

from planet_express.core.redact import redact

PROVIDER_VERSION = 2          # 2: `recreate` (force-recreate) replaced by `up` and `restart`; `service_configs`; facts carry `state`
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SERVICE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,127})/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
# Compose's own selection: the first base file that exists, then the first override that exists.
BASE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
OVERRIDE_NAMES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml",
                  "docker-compose.override.yml")
COMPOSE_NAMES = (*BASE_NAMES, *OVERRIDE_NAMES, ".env")
# The facts restore asserts after acting (contract §7 "Restorable"): compared exactly, through `comparable_facts`. A container's
# run state (`state`) is recorded beside them but is not one of them: it changes without anyone changing the configuration.
RESTORABLE_FIELDS = ("image_id", "command", "env_hash", "mounts", "networks", "labels", "restart_policy", "health")
# Before trust mode the per-session Docker proxy added this provenance marker to containers it created. Such containers may still
# carry it; a checkpoint restore recreates directly and never adds it. It is bookkeeping, not service configuration, so it must
# not make an otherwise exact restore look incomplete.
PROXY_CREATED_LABEL = "io.planetexpress.created-by-proxy"
# Compose rewrites these on every recreate. `config-hash` is among them: measured on Compose v2, a service with
# `network_mode: service:<other>` gets a different hash after every recreate (the resolved container id is hashed), so it cannot
# tell a faithful restore from an unfaithful one. Compose-level settings are covered by the compose files, which restore
# byte-for-byte and are verified by hash. The identity labels (project, service, working_dir, ...) stay in the comparison.
# `version`, `project.config_files` and `project.environment_file` record HOW Compose was invoked (which binary, which `-f` paths,
# which `--env-file`), not what the service is: the same service brought up by the operator from the stack directory and by PE
# with `-f <realpath>` differs only there, and a Compose upgrade between checkpoint and restore changes `version` everywhere.
VOLATILE_COMPOSE_LABELS = frozenset({f"com.docker.compose.{n}" for n in
                                     ("replace", "container-number", "depends_on", "image", "config-hash", "version",
                                      "project.config_files", "project.environment_file")})


def comparable_labels(labels: Any) -> dict[str, str]:
    """The labels a restore compares: everything but proxy bookkeeping and Compose's per-recreate metadata."""
    return {str(k): str(v) for k, v in sorted((labels or {}).items())
            if k != PROXY_CREATED_LABEL and k not in VOLATILE_COMPOSE_LABELS}


def comparable_facts(raw: Any) -> dict[str, Any] | None:
    """The one projection every "is this service as the checkpoint recorded it?" uses (restore, cancel, crash recovery): the
    restorable fields only, labels through `comparable_labels`. Applied to both sides, so a manifest written before a label was
    classed as bookkeeping, or before `state` was recorded, compares the same way as a new one. A recreate with the same
    configuration therefore matches: container id, name, creation and start times, network endpoint ids and run state are not here."""
    if raw is None:
        return None
    if isinstance(raw, ContainerFacts):
        raw = raw.to_dict()
    facts = {name: raw.get(name) for name in RESTORABLE_FIELDS}
    facts["labels"] = comparable_labels(facts.get("labels") if isinstance(facts.get("labels"), dict) else {})
    return facts


DOCKER_TIMEOUT_S = 30
# `compose config` prints every resolved value, secrets included: bounded, and only `name` is ever kept.
CONFIG_MAX_BYTES = 2 * 1024 * 1024
# `up -d` waits for each named service's dependencies to meet their `depends_on` conditions (`service_healthy` can take
# minutes for a database or a search index), so the bound grows with the number of services and is capped.
UP_BASE_TIMEOUT_S = 120
UP_PER_SERVICE_TIMEOUT_S = 30
UP_MAX_TIMEOUT_S = 900
REASON_MAX = 300


def action_timeout(count: int) -> int:
    """The time one `up` or `restart` of `count` services may take."""
    return min(UP_MAX_TIMEOUT_S, UP_BASE_TIMEOUT_S + UP_PER_SERVICE_TIMEOUT_S * max(1, count))


def _failure_reason(what: str, rc: int, out: str, err: str, timeout: int) -> str:
    """Why a compose action failed, never empty: the exit code and the END of what it printed (where Compose puts the
    error), redacted and bounded. A timeout says so."""
    if rc == 124:
        return f"{what} timed out after {timeout}s"
    text = (err or out or "").strip()
    tail = " ".join(redact(text).split())[-REASON_MAX:] if text else "no output"      # redact per line first, then fold
    return f"{what} exited {rc}: {tail}"

Runner = Callable[[list[str], int], tuple[int, str, str]]
BoundedRunner = Callable[[list[str], int, int], tuple[int, str, str, bool]]


class ProviderError(Exception):
    """A provider read failed or is ambiguous. The message never carries container environment or file contents."""


@dataclass(frozen=True)
class ComposeFile:
    path: str          # as discovered
    real_path: str     # after realpath
    kind: str          # compose | env | override


@dataclass(frozen=True)
class ContainerFacts:
    image_id: str
    command: list[str]
    env_hash: str                  # a hash only: environment values are never stored in facts
    mounts: list[dict[str, Any]]
    networks: list[str]
    labels: dict[str, str]
    restart_policy: dict[str, Any]
    health: dict[str, Any] | None
    # {"status": running|exited|created|restarting|paused|dead, "health": healthy|unhealthy|starting|None}. Recorded, not
    # restorable: a restore uses it to decide whether a service needs starting, and reports a service left not running.
    state: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ContainerFacts":
        return cls(**{name: raw[name] for name in RESTORABLE_FIELDS}, state=raw.get("state"))

    @classmethod
    def from_inspect(cls, doc: dict[str, Any]) -> "ContainerFacts":
        config = doc.get("Config") or {}
        host = doc.get("HostConfig") or {}
        env = sorted(config.get("Env") or [])
        mounts = sorted(
            ({"type": m.get("Type", ""), "source": m.get("Source", ""), "destination": m.get("Destination", ""),
              "rw": bool(m.get("RW", True))} for m in doc.get("Mounts") or []),
            key=lambda m: (m["destination"], m["source"]))
        networks = sorted(((doc.get("NetworkSettings") or {}).get("Networks") or {}).keys())
        policy = host.get("RestartPolicy") or {}
        health = config.get("Healthcheck")
        run = doc.get("State") if isinstance(doc.get("State"), dict) else None
        state = None
        if run is not None:
            probe = run.get("Health") if isinstance(run.get("Health"), dict) else {}
            state = {"status": str(run.get("Status") or ""), "health": (str(probe["Status"]) if probe.get("Status") else None)}
        return cls(
            image_id=str(doc.get("Image", "")),
            command=[*(config.get("Entrypoint") or []), *(config.get("Cmd") or [])],
            env_hash=hashlib.sha256("\0".join(env).encode()).hexdigest(),
            mounts=mounts, networks=networks,
            labels=comparable_labels(config.get("Labels")),
            restart_policy={"name": policy.get("Name", ""), "max_retries": int(policy.get("MaximumRetryCount") or 0)},
            health=dict(health) if health else None, state=state)


@dataclass(frozen=True)
class RecreateResult:
    """The outcome of `up` or `restart`. `error` is always set when `ok` is False, and names the stack, the exit code (or the
    timeout) and the tail of what Compose printed."""
    ok: bool
    recreated: tuple[str, ...] = ()       # the services the action was carried out for
    error: str | None = None


@dataclass(frozen=True)
class StackConfig:
    """What `compose config` resolves for a stack: the project name and, per ACTIVE service (a service behind a profile that
    is not enabled is absent), a sha256 of its effective configuration. The resolved document holds secrets: only hashes
    are kept."""
    name: str | None
    services: dict[str, str]


class CheckpointProvider(Protocol):
    """Version 2. Everything is read-only except `up` and `restart`."""
    version: ClassVar[int]

    def discover_compose(self, stack: str) -> list[ComposeFile]: ...
    def inspect(self, service: str) -> ContainerFacts | None: ...
    def project_name(self, stack: str) -> str | None: ...
    def service_configs(self, stack: str) -> StackConfig | None: ...
    def listening_ports(self) -> list[str]: ...
    def mount_table(self) -> list[str]: ...
    def unit_hashes(self, units: Sequence[str]) -> dict[str, str]: ...
    def up(self, services: Sequence[str]) -> RecreateResult: ...
    def restart(self, services: Sequence[str]) -> RecreateResult: ...
    def stop(self, services: Sequence[str]) -> RecreateResult: ...


def default_runner(argv: list[str], timeout: int) -> tuple[int, str, str]:
    import casa_bender as bender
    return bender.run_argv(argv, timeout)


def default_bounded_runner(argv: list[str], timeout: int, max_bytes: int) -> tuple[int, str, str, bool]:
    import casa_bender as bender
    return bender.run_argv_bounded(argv, timeout, max_bytes)


@dataclass
class _DockerCheckpointProvider:
    """Shared mechanics. `orchestrator` supplies `stacks_root`; compose and unit locations are per host kind."""
    orchestrator: Any
    run: Runner = field(default=default_runner)
    run_bounded: BoundedRunner = field(default=default_bounded_runner)
    compose_argv: Sequence[str] = ("docker", "compose")
    unit_dirs: Sequence[str] = ()
    read_text: Callable[[str], str] = lambda path: Path(path).read_text(encoding="utf-8", errors="replace")
    version: ClassVar[int] = PROVIDER_VERSION

    @property
    def stacks_root(self) -> Path:
        return Path(self.orchestrator.stacks_root)

    def discover_compose(self, stack: str) -> list[ComposeFile]:
        if not NAME_RE.fullmatch(stack):
            raise ProviderError(f"{stack!r} is not a stack name")
        directory = self.stacks_root / stack
        found = []
        for name in COMPOSE_NAMES:
            candidate = directory / name
            if candidate.is_symlink() or candidate.exists():
                kind = "env" if name == ".env" else "override" if "override" in name else "compose"
                found.append(ComposeFile(str(candidate), str(candidate.resolve()), kind))
        return found

    def compose_chain(self, stack: str) -> list[ComposeFile]:
        """The compose files `compose up` would merge, in its order: one base, then one override."""
        found = {Path(c.path).name: c for c in self.discover_compose(stack)}
        base = next((found[n] for n in BASE_NAMES if n in found), None)
        override = next((found[n] for n in OVERRIDE_NAMES if n in found), None)
        return [c for c in (base, override) if c is not None]

    def _resolved(self, stack: str) -> StackConfig | None:
        """One `config --format json` over the stack's -f chain, reduced at once to the project name and a hash per service.
        None when the CLI fails, times out, prints too much or prints something unreadable: nothing is guessed."""
        chain = self.compose_chain(stack)
        if not chain:
            return None
        files = [arg for c in chain for arg in ("-f", c.real_path)]
        rc, out, _err, truncated = self.run_bounded(
            [*self.compose_argv, *files, "config", "--format", "json"], DOCKER_TIMEOUT_S, CONFIG_MAX_BYTES)
        if rc != 0 or truncated:
            return None                      # failed, timed out or too large: unverifiable, output discarded
        try:
            doc = json.loads(out)
            name = doc.get("name")
            raw = doc.get("services")
            services = ({str(k): hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                         for k, v in raw.items()} if isinstance(raw, dict) else {})
        except (ValueError, AttributeError, TypeError):
            return None
        finally:
            out = doc = raw = ""             # the document holds resolved secrets: nothing but `name` and hashes survive
        return StackConfig(name if isinstance(name, str) and name else None, services)

    def project_name(self, stack: str) -> str | None:
        """The project name Compose itself resolves for this stack (`config --format json`, same -f chain), which
        accounts for `name:`, COMPOSE_PROJECT_NAME, `.env` and the directory rules. Never guessed: when the CLI
        fails or says nothing this is None, and callers treat the stack's facts as unverifiable."""
        resolved = self._resolved(stack)
        return resolved.name if resolved is not None else None

    def service_configs(self, stack: str) -> StackConfig | None:
        """Each active service's effective configuration as Compose resolves it from the files on disk NOW (interpolation,
        `.env`, `env_file`, overrides and profiles applied), as a hash. A comment-only edit to `.env` changes no hash. None
        when Compose cannot resolve the stack or names no project."""
        if not NAME_RE.fullmatch(stack):
            raise ProviderError(f"{stack!r} is not a stack name")
        resolved = self._resolved(stack)
        return resolved if resolved is not None and resolved.name else None

    def _container_id(self, service: str) -> str | None:
        match = SERVICE_RE.fullmatch(service)
        if not match:
            raise ProviderError(f"{service!r} is not <stack>/<service>")
        stack, name = match.groups()
        project = self.project_name(stack)
        if project is None:
            return None                      # unknown project: no container can be matched, never a guess
        rc, out, _err = self.run(
            ["docker", "ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}",
             "--filter", f"label=com.docker.compose.service={name}"], DOCKER_TIMEOUT_S)
        if rc != 0:
            raise ProviderError(f"{service}: docker ps failed")
        ids = sorted(line.strip() for line in out.splitlines() if line.strip())
        if len(ids) > 1:
            raise ProviderError(f"{service}: {len(ids)} containers match; a scaled service cannot be checkpointed")
        return ids[0] if ids else None

    def inspect(self, service: str) -> ContainerFacts | None:
        cid = self._container_id(service)
        if cid is None:
            return None
        rc, out, _err = self.run(["docker", "inspect", cid], DOCKER_TIMEOUT_S)
        if rc != 0:
            raise ProviderError(f"{service}: docker inspect failed")
        try:
            doc = json.loads(out)
            return ContainerFacts.from_inspect(doc[0] if isinstance(doc, list) else doc)
        except (ValueError, IndexError, TypeError, KeyError, AttributeError):
            raise ProviderError(f"{service}: docker inspect returned something unreadable") from None

    def listening_ports(self) -> list[str]:
        rc, out, _err = self.run(["ss", "-H", "-ltnu"], DOCKER_TIMEOUT_S)
        if rc != 0:
            return []
        rows = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                rows.add(f"{parts[0]} {parts[4]}")
        return sorted(rows)

    def mount_table(self) -> list[str]:
        try:
            text = self.read_text("/proc/self/mounts")
        except OSError:
            return []
        return sorted(line.strip() for line in text.splitlines() if line.strip())

    def unit_hashes(self, units: Sequence[str]) -> dict[str, str]:
        out = {}
        for unit in units:
            if not NAME_RE.fullmatch(unit):
                raise ProviderError(f"{unit!r} is not a unit name")
            digest = "absent"
            for directory in self.unit_dirs:
                try:
                    digest = hashlib.sha256(self.read_text(f"{directory}/{unit}").encode()).hexdigest()
                    break
                except OSError:
                    continue
            out[unit] = digest
        return out

    def _compose_action(self, services: Sequence[str], verb: str, args: Sequence[str]) -> RecreateResult:
        by_stack: dict[str, list[str]] = {}
        for service in services:
            match = SERVICE_RE.fullmatch(service)
            if not match:
                return RecreateResult(False, (), f"{service!r} is not <stack>/<service>")
            by_stack.setdefault(match.group(1), []).append(match.group(2))
        done: list[str] = []
        for stack, names in sorted(by_stack.items()):
            chain = self.compose_chain(stack)
            if not chain:
                return RecreateResult(False, tuple(done), f"{stack}: no compose file")
            files = [arg for c in chain for arg in ("-f", c.real_path)]
            project = self.project_name(stack)
            flag = ["-p", project] if project else []         # unresolved: let Compose decide for itself
            timeout = action_timeout(len(names))
            rc, out, err = self.run([*self.compose_argv, *flag, *files, verb, *args, *names], timeout)
            if rc != 0:
                return RecreateResult(False, tuple(done), f"{stack}: " + _failure_reason(f"compose {verb}", rc, out, err, timeout))
            done += [f"{stack}/{n}" for n in names]
        return RecreateResult(True, tuple(done))

    def up(self, services: Sequence[str]) -> RecreateResult:
        """`compose up -d --no-deps <services>`: creates a missing container, starts a stopped one, and recreates one only when
        Compose finds its configuration changed. Never `--force-recreate`, and only the services named (naming a service
        behind a profile activates it, so the caller names only services that had a container at the checkpoint)."""
        return self._compose_action(services, "up", ("-d", "--no-deps"))

    def restart(self, services: Sequence[str]) -> RecreateResult:
        """`compose restart <services>`: for a running container whose configuration is right but which is unhealthy."""
        return self._compose_action(services, "restart", ())

    def stop(self, services: Sequence[str]) -> RecreateResult:
        """`compose stop <services>`: a service that was stopped at the checkpoint, after `up` recreated it, is left stopped."""
        return self._compose_action(services, "stop", ())


class SystemdCheckpointProvider(_DockerCheckpointProvider):
    """Docker Compose plugin on a systemd host, over `SystemdStackOrchestrator`'s stacks root."""

    def __init__(self, orchestrator: Any, **kw: Any):
        kw.setdefault("compose_argv", ("docker", "compose"))
        kw.setdefault("unit_dirs", ("/etc/systemd/system", "/lib/systemd/system", "/usr/lib/systemd/system"))
        super().__init__(orchestrator, **kw)


class MosCheckpointProvider(_DockerCheckpointProvider):
    """Standalone `docker-compose` on MOS, over `MosStackOrchestrator`'s stacks root. MOS parity is claimed only
    once this passes its conformance and privileged runs on the MOS VM."""

    def __init__(self, orchestrator: Any, **kw: Any):
        kw.setdefault("compose_argv", ("docker-compose",))
        kw.setdefault("unit_dirs", ("/etc/init.d",))
        super().__init__(orchestrator, **kw)
