"""PE's own host, metric and remote-container types, and which host is this one.

Pure: no network, no filesystem, no docker. A collector hands plain data in and gets these
types back, so the two questions that decide whether the multi-host surface is correct --
"is this reading still true?" and "which of these systems is the machine we are running
on?" -- are answerable in a unit test against captured data rather than against a live hub.

Two rules the rest of T48 leans on:

  * **Absent is not zero.** Every optional field is None. A host whose agent is too old to
    report `os_name` has no OS name; a host reporting 0% CPU is idle. Collapsing the first
    into `""` or the second into `0` is the bug this project has now shipped four times --
    an empty container list, unreadable labels, cancelled downloads, and containers that
    were simply not seen. A renderer can show "unknown" for None; it cannot recover the
    distinction once a mapper has thrown it away.
  * **Locality is derived, never declared.** An operator-declared `local: true` flag passes
    every cardinality check while pointing at the wrong system, so `locality()` below
    answers from evidence PE already has: its own container names, read from the docker
    socket by its caller. Ambiguity returns None, because showing this host twice -- once
    controllable, once as read-only remote numbers that will not agree -- is worse than
    showing one host as unknown.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from planet_express.core.numbers import finite

# Twice the collection interval. The `1m` stats bucket is a 60-second interval, so this is
# measured from the bucket type rather than guessed at from `info.dt`, whose meaning is
# undetermined and which this module deliberately never reads.
STALE_AFTER = 120

# The local set has to be big enough that the answer does not turn on one coincidence:
# `beszel-agent` alone exists on every host in the fleet.
MIN_LOCAL_NAMES = 5
# Half the local set, and three times the runner-up. Measured on 2026-10-01 the real
# separation was coverage 1.0 and 85 names against a runner-up of 1, so these thresholds sit
# nowhere near the live data -- they exist to refuse the close cases, not to admit this one.
MIN_COVERAGE = 0.5
MARGIN = 3

LivenessState = Literal["current", "stale", "unknown"]

CURRENT: LivenessState = "current"
STALE: LivenessState = "stale"
UNKNOWN: LivenessState = "unknown"


@dataclass(frozen=True)
class HostDetails:
    """Named hardware facts, one row per host.

    Every field is optional because agent versions differ: Unraid runs 0.17.0 against 0.20.0
    elsewhere and reports an empty `os_name`. An older agent reporting less is the normal
    case, not an error, and `memory_bytes` is bytes here while `HostMetrics.mem_total_gib` is
    GiB -- the same quantity under a different unit in a different collection, which is why
    the unit is in the field name on both sides.
    """

    hostname: str | None = None
    cores: int | None = None
    threads: int | None = None
    arch: str | None = None
    kernel: str | None = None
    cpu_model: str | None = None
    memory_bytes: int | None = None
    os_name: str | None = None


@dataclass(frozen=True)
class HostMetrics:
    """One reading from the newest `1m` bucket, decoded into named fields.

    A field the collector did not send, or sent as something that is not a usable number, is
    None. `load` is the three load averages and `temps` maps sensor name to degrees Celsius;
    a host with no sensors has no temps, which is not the same as a host reporting 0.
    """

    cpu_pct: float | None = None
    mem_pct: float | None = None
    mem_used_gib: float | None = None
    mem_total_gib: float | None = None
    disk_pct: float | None = None
    disk_used_gib: float | None = None
    disk_total_gib: float | None = None
    load: tuple[float, ...] | None = None
    temps: Mapping[str, float] | None = None


@dataclass(frozen=True)
class RemoteContainer:
    """A container on another host. Observation only -- there is no control path to it.

    `name` is the only required field: without it there is nothing to identify or to compare
    against the local set. It carries no labels, because the collector's `containers` table
    has none, and that is the outcome we would have chosen anyway -- a label authored on
    another host must not steer rendering on this one.

    `updatable` is a tri-state on purpose: True means an image update is available, False
    means the collector checked and there is none, None means nobody could tell us.
    """

    name: str
    image: str | None = None
    status: str | None = None
    health: str | None = None
    cpu: float | None = None
    memory: float | None = None
    net: float | None = None
    ports: str | None = None
    updatable: bool | None = None


@dataclass(frozen=True)
class Host:
    """One host as PE renders it: identity from config, live values filled in if readable.

    `id` is the collector's stable system id; it is the only handle, because the name is
    editable in the collector's UI and the address changes. `link` is the URL of the host's
    own UI and comes from PE's inventory alone -- the collector stores no link, and a link
    is exactly the field we would not let a remote host set.

    `name` and `link` are None for a host the collector reports that the inventory does not
    list. `updated` is epoch seconds, the input to `liveness()`.
    """

    id: str
    name: str | None = None
    link: str | None = None
    status: str | None = None
    updated: float | None = None
    details: HostDetails | None = None


@dataclass(frozen=True)
class Liveness:
    """Whether a reading is worth rendering as a number, with why and how old.

    `age` is seconds since the reading and is None whenever there is no reading to age --
    the one place a renderer must not substitute 0, since "no reading" and "a reading from
    this instant" are opposite situations.
    """

    state: LivenessState
    reason: str | None = None
    age: float | None = None

    @property
    def is_current(self) -> bool:
        return self.state == CURRENT


def parse_timestamp(value) -> float | None:
    """Epoch seconds from the collector's timestamp string, or None if it is not one.

    The collector writes `2026-10-01 10:56:34.933Z`, which is ISO 8601 with a space instead
    of `T`. Unparseable input is None rather than an exception or a zero: a timestamp we
    cannot read makes the reading unknown, and it is the caller's business to say so.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # An epoch second is ~1.8e9, so numbers.MAX is the right scale guard here; it is also
        # what stops `inf` or a 400-digit int from reaching the arithmetic in liveness().
        return float(value) if finite(value) else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A collector timestamp is UTC whether or not it says so; a naive one read as local time
    # would be hours off and would flip hosts between current and stale by timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def liveness(updated, *, now: float, reason: str | None = None) -> Liveness:
    """Classify a reading by its age. `updated` is epoch seconds or a collector timestamp.

    `reason` overrides the one this function would write, for the caller that already knows
    why a reading is missing -- a host absent from the collector's answer, or a collector
    that is down entirely.
    """
    at = parse_timestamp(updated)
    if at is None:
        return Liveness(UNKNOWN, reason or "no reading", None)
    # Clock skew between hosts is real and small. A reading stamped slightly ahead of us is
    # the freshest thing we have, not an anomaly worth hiding a host over, so the age floors
    # at zero instead of going negative or turning the host unknown.
    age = max(now - at, 0.0)
    if age > STALE_AFTER:
        return Liveness(STALE, reason or f"reading is {int(age)}s old", age)
    return Liveness(CURRENT, reason, age)


def unknown(reason: str) -> Liveness:
    """Liveness for a host there is no reading for at all. Age stays None, never 0."""
    return Liveness(UNKNOWN, reason, None)


def coverage(local_names: Iterable[str], names: Iterable[str]) -> float | None:
    """|S ∩ L| / |L| -- how much of *this* host's container set a system accounts for.

    The denominator is always the local set. Dividing by the remote set instead would let a
    host reporting thousands of containers win on volume, and would score a host reporting
    one container that happens to match at 1.0.
    """
    local = _names(local_names)
    if not local:
        return None
    return len(local & _names(names)) / len(local)


def locality(local_names: Iterable[str], by_system: Mapping[str, Iterable[str]]) -> str | None:
    """The system id that is this machine, or None when the evidence does not settle it.

    `local_names` are the container names PE read from its own docker socket; `by_system`
    maps each system id to the container names the collector reports for it. Compared by
    exact name.

    None is a real answer and the caller must handle it: it means locality is unknown, and
    while it is unknown the unconfigured collector rows are withheld rather than rendered,
    because one of them may be this host.
    """
    local = _names(local_names)
    if len(local) < MIN_LOCAL_NAMES:
        return None

    # Collector-supplied keys: a blank or non-string id could not be matched against the
    # inventory later anyway, and must not become the answer here.
    overlaps = sorted(
        ((len(local & _names(names)), system_id)
         for system_id, names in by_system.items()
         if isinstance(system_id, str) and system_id),
        reverse=True,
    )
    if not overlaps:
        return None

    best, system_id = overlaps[0]
    runner_up = overlaps[1][0] if len(overlaps) > 1 else 0
    if best / len(local) < MIN_COVERAGE:
        return None
    # A tie fails this on its own: with best == runner_up the margin can only hold at zero,
    # which the coverage test above has already refused.
    if best < MARGIN * runner_up:
        return None
    return system_id


def _names(values: Iterable[str]) -> frozenset[str]:
    """Container names as a set, dropping anything that is not a usable name.

    Duplicates collapse, which is correct for coverage: two containers called `beszel-agent`
    on one host are still one name shared with ours.
    """
    if isinstance(values, str) or not isinstance(values, Iterable):
        return frozenset()
    return frozenset(v for v in values if isinstance(v, str) and v)
