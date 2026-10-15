"""The checkpoint provider seam (contract §7, v1).

`HostControlProvider` covers only units, logs and metrics, so checkpoints get their own versioned protocol:
compose-file discovery, container inspect, host observations and per-service recreate. Both implementations
share the Docker CLI mechanics (argv only, through `casa_bender.run_argv`, no shell string anywhere) and differ
in the compose binary, where unit files live and which stack orchestrator they are built on. Reads are the
only Docker calls made here except `recreate`, which a restore asks for explicitly.
"""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol

from planet_express.core.redact import redact

PROVIDER_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SERVICE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,127})/([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
# Compose's own selection: the first base file that exists, then the first override that exists.
BASE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
OVERRIDE_NAMES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml",
                  "docker-compose.override.yml")
COMPOSE_NAMES = (*BASE_NAMES, *OVERRIDE_NAMES, ".env")
# The facts restore asserts after a recreate (contract §7 "Restorable"): compared exactly.
RESTORABLE_FIELDS = ("image_id", "command", "env_hash", "mounts", "networks", "labels", "restart_policy", "health")
DOCKER_TIMEOUT_S = 30
RECREATE_TIMEOUT_S = 180

Runner = Callable[[list[str], int], tuple[int, str, str]]


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ContainerFacts":
        return cls(**{name: raw[name] for name in RESTORABLE_FIELDS})

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
        return cls(
            image_id=str(doc.get("Image", "")),
            command=[*(config.get("Entrypoint") or []), *(config.get("Cmd") or [])],
            env_hash=hashlib.sha256("\0".join(env).encode()).hexdigest(),
            mounts=mounts, networks=networks,
            labels={str(k): str(v) for k, v in sorted((config.get("Labels") or {}).items())},
            restart_policy={"name": policy.get("Name", ""), "max_retries": int(policy.get("MaximumRetryCount") or 0)},
            health=dict(health) if health else None)


@dataclass(frozen=True)
class RecreateResult:
    ok: bool
    recreated: tuple[str, ...] = ()
    error: str | None = None


class CheckpointProvider(Protocol):
    """Version 1. Everything is read-only except `recreate`."""
    version: ClassVar[int]

    def discover_compose(self, stack: str) -> list[ComposeFile]: ...
    def inspect(self, service: str) -> ContainerFacts | None: ...
    def listening_ports(self) -> list[str]: ...
    def mount_table(self) -> list[str]: ...
    def unit_hashes(self, units: Sequence[str]) -> dict[str, str]: ...
    def recreate(self, services: Sequence[str]) -> RecreateResult: ...


def default_runner(argv: list[str], timeout: int) -> tuple[int, str, str]:
    import casa_bender as bender
    return bender.run_argv(argv, timeout)


@dataclass
class _DockerCheckpointProvider:
    """Shared mechanics. `orchestrator` supplies `stacks_root`; compose and unit locations are per host kind."""
    orchestrator: Any
    run: Runner = field(default=default_runner)
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

    def project_name(self, stack: str) -> str | None:
        """The project name Compose itself resolves for this stack (`config --format json`, same -f chain), which
        accounts for `name:`, COMPOSE_PROJECT_NAME, `.env` and the directory rules. Never guessed: when the CLI
        fails or says nothing this is None, and callers treat the stack's facts as unverifiable."""
        chain = self.compose_chain(stack)
        if not chain:
            return None
        files = [arg for c in chain for arg in ("-f", c.real_path)]
        rc, out, _err = self.run([*self.compose_argv, *files, "config", "--format", "json"], DOCKER_TIMEOUT_S)
        if rc != 0:
            return None
        try:
            name = json.loads(out).get("name")
        except (ValueError, AttributeError):
            return None
        return name if isinstance(name, str) and name else None

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

    def recreate(self, services: Sequence[str]) -> RecreateResult:
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
            rc, out, err = self.run(
                [*self.compose_argv, *flag, *files, "up", "-d", "--force-recreate", "--no-deps", *names],
                RECREATE_TIMEOUT_S)
            if rc != 0:
                return RecreateResult(False, tuple(done), redact((err or out or "compose failed").strip())[:200])
            done += [f"{stack}/{n}" for n in names]
        return RecreateResult(True, tuple(done))


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
