"""The read-only, cached multi-host view (T48 S4).

The collector is deliberately not part of either Leela's scan or a request handler.  A
page gets the last complete answer immediately and merely asks this module to refresh it
in the background.  This is the same separation the icon cache uses: a slow dependency
may make its *next* answer late, never the page currently being served.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from config_schema import MultiHostConfig
from planet_express.core.hosts import (
    HostDetails,
    HostMetrics,
    Liveness,
    RemoteContainer,
    locality,
    unknown,
)
from planet_express.integrations.beszel import (
    FLEET_BUDGET_SECONDS,
    BeszelHubProvider,
    ContainerReading,
    FixtureHostProvider,
    HostProvider,
)

# The captured readings are intentionally old.  The fixture path is development data, not a
# clock test, so date it just after the capture rather than presenting every card as stale.
FIXTURE_NOW = 1790852220.0  # 2026-10-01 10:57:00 UTC
REFRESH_BUDGET_SECONDS = 20
REFRESH_INTERVAL_SECONDS = 60
REFRESH_PENDING = "waiting for the background collector refresh"
NO_COLLECTOR_ROW = "the collector returned no row for this configured host"


@dataclass(frozen=True)
class HostCard:
    """One deliberately presentation-friendly, observation-only host row."""

    id: str
    name: str
    link: str | None
    configured: bool
    liveness: Liveness
    details: HostDetails | None = None
    metrics: HostMetrics | None = None
    containers: tuple[RemoteContainer, ...] | None = None
    containers_liveness: Liveness | None = None
    status: str | None = None


@dataclass(frozen=True)
class FleetView:
    cards: tuple[HostCard, ...]
    locality_id: str | None
    locality_reason: str | None = None


def provider_for(credentials: Callable[[], tuple[str, str]]) -> HostProvider:
    """Pick live hub only when both credentials exist; otherwise use real captured data."""
    user, password = credentials()
    if user and password:
        return BeszelHubProvider(credentials=credentials, budget=FLEET_BUDGET_SECONDS)
    return FixtureHostProvider(wall=lambda: FIXTURE_NOW)


def pin_error(multi_host: MultiHostConfig, provider: HostProvider,
              local_names: Callable[[], tuple[str, ...] | None]) -> str | None:
    """Return the apply-time pin disagreement, if evidence can actually settle it.

    An unavailable collector or local Docker read must not turn a valid pin into an invalid
    config.  Conversely, once all inputs are readable, a disagreement is loud rather than
    allowing a pin to silently override the evidence that prevents duplicate local cards.
    """
    if multi_host.local_system_id is None:
        return None
    names = local_names()
    if names is None:
        return None
    fleet = provider.hosts()
    if not fleet.liveness.is_current:
        return None
    by_system: dict[str, tuple[str, ...]] = {}
    for reading in fleet.hosts:
        containers = provider.containers(reading.host.id)
        if containers.containers is None:
            return None
        by_system[reading.host.id] = tuple(c.name for c in containers.containers)
    derived = locality(names, by_system)
    if derived is not None and derived != multi_host.local_system_id:
        return ("multi_host.local_system_id disagrees with the local system derived from "
                "the Docker container names")
    return None


class FleetCache:
    """A one-refresh-at-a-time cache.  ``view()`` never calls the provider synchronously."""

    def __init__(self, inventory: MultiHostConfig, provider: HostProvider,
                 local_names: Callable[[], tuple[str, ...] | None], *, clock=time.monotonic):
        self._inventory = inventory
        self._provider = provider
        self._local_names = local_names
        self._clock = clock
        self._lock = threading.Lock()
        self._refreshing = False
        self._next_refresh = 0.0
        self._view: FleetView | None = None

    def view(self) -> FleetView:
        """The cached view, scheduling (but never awaiting) its replacement."""
        with self._lock:
            cached = self._view
            if (self._inventory.hosts and not self._refreshing
                    and self._clock() >= self._next_refresh):
                self._refreshing = True
                self._next_refresh = self._clock() + REFRESH_INTERVAL_SECONDS
                thread = threading.Thread(target=self._refresh, daemon=True,
                                          name="multi-host-refresh")
                thread.start()
        return cached if cached is not None else self._pending_view()

    def _pending_view(self) -> FleetView:
        return FleetView(tuple(
            HostCard(entry.system_id, entry.name, entry.link, True,
                     unknown(REFRESH_PENDING), containers_liveness=unknown(REFRESH_PENDING))
            for entry in self._inventory.hosts
        ), None, REFRESH_PENDING)

    def _refresh(self) -> None:
        try:
            answer = self._collect()
        except Exception:  # noqa: BLE001 -- provider code must never take a page down
            answer = self._failed_view("the background collector refresh failed")
        with self._lock:
            self._view = answer
            self._refreshing = False

    def _failed_view(self, reason: str) -> FleetView:
        return FleetView(tuple(
            HostCard(entry.system_id, entry.name, entry.link, True, unknown(reason),
                     containers_liveness=unknown(reason))
            for entry in self._inventory.hosts
        ), None, reason)

    def _collect(self) -> FleetView:
        deadline = self._clock() + REFRESH_BUDGET_SECONDS
        fleet = self._call_before(deadline, self._provider.hosts)
        if fleet is None:
            return self._failed_view("ran out of time reading the collector")
        if not fleet.liveness.is_current:
            return self._failed_view(fleet.liveness.reason or "the collector could not be read")

        container_readings: dict[str, ContainerReading] = {}
        pending: list[tuple[str, threading.Event, dict]] = []
        for reading in fleet.hosts:
            completed = threading.Event()
            result: dict = {}

            def read(system_id=reading.host.id, done=completed, holder=result):
                try:
                    holder["reading"] = self._provider.containers(system_id)
                except Exception:  # noqa: BLE001 -- provider failure is an unknown reading
                    holder["reading"] = ContainerReading(
                        system_id, unknown("the collector container read failed"))
                finally:
                    done.set()

            thread = threading.Thread(target=read, daemon=True, name="multi-host-containers")
            thread.start()
            pending.append((reading.host.id, completed, result))
        # Container calls run together: four per-call timeouts must not become four serial
        # waits.  Any call still outstanding at the one whole-refresh deadline is unknown.
        for system_id, completed, result in pending:
            completed.wait(max(0.0, deadline - self._clock()))
            container_readings[system_id] = result.get("reading", ContainerReading(
                system_id, unknown("ran out of time reading collector containers")))

        names = self._local_names()
        by_system = {
            system_id: tuple(container.name for container in reading.containers or ())
            for system_id, reading in container_readings.items()
            if reading.containers is not None
        }
        derived = locality(names or (), by_system) if names is not None else None
        # A pin only supplies an answer when derivation cannot.  A successful contradictory
        # derivation is refused at config apply time; it must never be quietly overridden here.
        local_id = derived if derived is not None else self._inventory.local_system_id
        locality_reason = None if local_id else "locality is unknown; unconfigured rows are withheld"

        readings = {reading.host.id: reading for reading in fleet.hosts}
        cards: list[HostCard] = []
        configured_ids = {entry.system_id for entry in self._inventory.hosts}
        for entry in self._inventory.hosts:
            if entry.system_id == local_id:
                continue
            reading = readings.get(entry.system_id)
            containers = container_readings.get(entry.system_id)
            if reading is None:
                cards.append(HostCard(entry.system_id, entry.name, entry.link, True,
                                      unknown(NO_COLLECTOR_ROW),
                                      containers_liveness=unknown(NO_COLLECTOR_ROW)))
                continue
            cards.append(HostCard(
                entry.system_id, entry.name, entry.link, True, reading.liveness,
                details=reading.host.details, metrics=reading.metrics,
                containers=containers.containers if containers else None,
                containers_liveness=containers.liveness if containers else unknown(NO_COLLECTOR_ROW),
                status=reading.host.status,
            ))

        # The collector's names are display text only.  It never supplies a link and it is
        # intentionally impossible for this module to reach the execution/action packages.
        #
        # The gate is `local_id`, not `derived`.  Rows are withheld while locality is
        # UNKNOWN, and a pin is one of the two ways it becomes known -- gating on the
        # derivation alone suppressed every unconfigured row on exactly the hosts a pin
        # exists to rescue, where docker evidence is absent or ambiguous.  The pin cannot
        # contradict a successful derivation: that is refused at config apply time.
        if local_id is not None:
            for system_id, reading in readings.items():
                if system_id in configured_ids or system_id == local_id:
                    continue
                containers = container_readings.get(system_id)
                cards.append(HostCard(
                    system_id, reading.host.name or f"unconfigured {system_id}", None, False,
                    reading.liveness, details=reading.host.details, metrics=reading.metrics,
                    containers=containers.containers if containers else None,
                    containers_liveness=containers.liveness if containers else unknown(NO_COLLECTOR_ROW),
                    status=reading.host.status,
                ))
        return FleetView(tuple(cards), local_id, locality_reason)

    def _call_before(self, deadline: float, call):
        """Run one untrusted provider call outside the cache thread, bounded by its deadline."""
        completed = threading.Event()
        result: dict = {}

        def run():
            try:
                result["value"] = call()
            except Exception:  # noqa: BLE001 -- provider implementations are external to requests
                result["value"] = None
            finally:
                completed.set()

        thread = threading.Thread(target=run, daemon=True, name="multi-host-fleet")
        thread.start()
        completed.wait(max(0.0, deadline - self._clock()))
        return result.get("value")
