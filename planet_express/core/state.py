"""PE's normalized state model -- the entity types a dependency graph reasons about.

v3 Phase 1 (`docs/designs/phase-1-state-model.md`). Before this, PE had no `Stack`/`Service`/
`Container` type at all: state was dicts threaded through single-purpose functions (Leela's
monitor snapshot, `config.active_stack_dirs()` returning bare `Path`s, `actions.py`'s read
functions). These types are what `planet_express.core.dependencies` connects with edges, and
what a future host-provider interface (Phase 3) will be shaped around -- not the reverse.

Two rules carried over from `core/hosts.py`, because the same failure modes apply here:

  * **Absent is not zero.** A service with no declared `container_name:` has `container_name:
    None`, not `""`. A stack this host doesn't run is simply not in the graph, not a zeroed-out
    entry.
  * **Fields reflect what compose-file-level analysis can know today**, not a speculative full
    schema. `Host`, `Disk`, `StoragePool`, `BackupJob` are intentionally thin here -- Phase 1's
    actual weight is the dependency graph (`dependencies.py`), not fleshing out every entity the
    Architecture Brief names. They exist as real types so later phases extend them instead of
    inventing a second model.
"""

from dataclasses import dataclass, field
from typing import Literal

HealthCondition = Literal["healthy", "unhealthy", "degraded", "unknown"]
Severity = Literal["critical", "high", "medium", "low"]


@dataclass(frozen=True)
class HealthState:
    """One entity's health, and why. `severity` is None exactly when `condition` is "healthy"
    or "unknown" -- there is nothing to rank when nothing is wrong or nothing is known."""

    condition: HealthCondition
    severity: Severity | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.condition in ("healthy", "unknown") and self.severity is not None:
            raise ValueError(f"{self.condition} health has no severity to carry")
        if self.condition in ("unhealthy", "degraded") and self.severity is None:
            raise ValueError(f"{self.condition} health needs a severity")


@dataclass(frozen=True)
class Service:
    """One `services.<name>:` entry in one compose file. Not yet a running container -- a
    service can be declared and never started, which is exactly the created-but-not-started
    failure this model exists to catch."""

    name: str
    stack: str
    container_name: str | None = None
    image: str | None = None
    network_mode: str | None = None
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stack:
    """One compose project: one directory, one `docker-compose.yml`, the name `casa_boot.py`
    already uses to order network before everything else."""

    name: str
    compose_path: str
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class Container:
    """A service actually running (or having run). `container_name` is the compose-assigned
    name; `container_id` is the live Docker ID, present only once something has inspected it."""

    name: str
    stack: str
    container_name: str | None = None
    container_id: str | None = None
    health: HealthState | None = None


@dataclass(frozen=True)
class Network:
    name: str
    driver: str | None = None


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class Disk:
    device: str
    mountpoint: str | None = None


@dataclass(frozen=True)
class StoragePool:
    name: str
    disks: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackupJob:
    name: str
    last_run: str | None = None
    result: str | None = None


@dataclass(frozen=True)
class Host:
    """The machine PE is running on. Thin by design -- Phase 3 is where a `HostProvider`
    (name pending resolution of the collision with `integrations/beszel.py`'s existing
    `HostProvider`, per the Addendum) decides how this gets populated on a given host."""

    name: str
    stacks: tuple[str, ...] = field(default_factory=tuple)
