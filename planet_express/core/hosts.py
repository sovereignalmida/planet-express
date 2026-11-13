"""PE's own host, metric and remote-container types, and which host is this one.

Pure: no network, no filesystem, no docker. A collector hands plain data in and gets these
types back, so the two questions that decide whether the multi-host surface is correct --
"is this reading still true?" -- is answerable in a unit test rather than against a live host.

Two rules the rest of T48 leans on:

  * **Absent is not zero.** Every optional field is None. A host whose agent is too old to
    report `os_name` has no OS name; a host reporting 0% CPU is idle. Collapsing the first
    into `""` or the second into `0` is the bug this project has now shipped four times --
    an empty container list, unreadable labels, cancelled downloads, and containers that
    were simply not seen. A renderer can show "unknown" for None; it cannot recover the
    distinction once a mapper has thrown it away.
  * **Locality is derived, never declared.** Core refuses an SSH alias whose Docker engine ID is this host's own
    (application/fleet_collector.py), so this host is never shown twice -- once controllable, once as read-only numbers.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from planet_express.core.numbers import finite

# Twice the collection interval. The `1m` stats bucket is a 60-second interval, so this is
# measured from the bucket type rather than guessed at from `info.dt`, whose meaning is
# undetermined and which this module deliberately never reads.
STALE_AFTER = 120

# How far ahead of us a reading may be stamped and still be treated as clock skew. NTP-synced
# hosts sit within a second or two of each other, and a fleet reporting every 60s cannot
# explain a reading two intervals in the future as skew -- past this, the timestamp is not
# trustworthy enough to date the reading at all. Bounding it is what stops a fast-clocked host
# reading `current` forever; see liveness().
SKEW_TOLERANCE = STALE_AFTER


LivenessState = Literal["current", "stale", "unknown"]

# Why an unknown reading is unknown, for the renderer. The display text is `reason`; nothing may
# infer a cause by matching that text, because rewording a reason would silently change the badge.
UnknownCause = Literal["no_contact", "not_permitted", "clock_unusable"]
NO_CONTACT: UnknownCause = "no_contact"
NOT_PERMITTED: UnknownCause = "not_permitted"
CLOCK_UNUSABLE: UnknownCause = "clock_unusable"

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
    restarts: int | None = None          # the SSH collector reads it; Beszel does not


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
    cause: UnknownCause | None = None

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
        return Liveness(UNKNOWN, reason or "no reading", None, CLOCK_UNUSABLE)
    ahead = at - now
    # Clock skew between hosts is real and small, so a reading stamped slightly ahead of us is
    # the freshest thing we have and its age floors at zero rather than going negative.
    #
    # But the floor is bounded. Unbounded, a host whose clock runs fast reads `current`
    # forever: its age pins at zero no matter how long ago the agent actually died, and
    # nothing on the page ever looks wrong. That is this project's recurring bug inverted --
    # not absent data rendered as present, but untrustworthy data rendered as fresh, which is
    # worse because it is silent. Past the tolerance we cannot date the reading at all, so it
    # is unknown, which is the honest answer.
    if ahead > SKEW_TOLERANCE:
        return Liveness(UNKNOWN, reason or f"reading is stamped {int(ahead)}s in the future", None,
                        CLOCK_UNUSABLE)
    age = max(-ahead, 0.0)
    if age > STALE_AFTER:
        return Liveness(STALE, reason or f"reading is {int(age)}s old", age)
    return Liveness(CURRENT, reason, age)


def unknown(reason: str, cause: UnknownCause | None = None) -> Liveness:
    """Liveness for a host there is no reading for at all. Age stays None, never 0.

    `cause` is None for the ordinary "could not reach it" case, which renders as no contact."""
    return Liveness(UNKNOWN, reason, None, cause)
