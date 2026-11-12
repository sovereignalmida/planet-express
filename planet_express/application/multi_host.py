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
    NOT_PERMITTED,
    HostDetails,
    HostMetrics,
    Liveness,
    RemoteContainer,
    liveness,
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
    source: str = "beszel"               # or "ssh" or "link": where this card's values come from (docs/designs/multi-host.md)
    ssh: str | None = None
    problem: str | None = None           # ssh: the collector's fixed cause word (this_host, not_permitted, no_contact, not_trying)
    locality_checked: bool = True        # ssh: whether the reading was compared with this host's own Docker engine


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

    def _failed_view(self, reason: str, cause=None) -> FleetView:
        return FleetView(tuple(
            HostCard(entry.system_id, entry.name, entry.link, True, unknown(reason, cause),
                     containers_liveness=unknown(reason, cause))
            for entry in self._inventory.hosts
        ), None, reason)

    def _collect(self) -> FleetView:
        deadline = self._clock() + REFRESH_BUDGET_SECONDS
        fleet = self._call_before(deadline, self._provider.hosts)
        if fleet is None:
            return self._failed_view("ran out of time reading the collector")
        if not fleet.liveness.is_current:
            return self._failed_view(fleet.liveness.reason or "the collector could not be read",
                                     fleet.liveness.cause)

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


# ── the SSH collector's hosts (release A): Core writes state/hosts.json, the page reads it ──────────────────────────────────────

NOT_COLLECTED = "Core has not read this host yet (the collector runs every minute while Core is up)"
# A reading older than this is not drawn at all, even as "last known" (review, release A: stale for ever hid a dead host).
SSH_UNKNOWN_AFTER = 15 * 60
# Core tries every host every minute; no try for this long means the collector itself is not running.
SSH_NOT_TRYING_AFTER = 5 * 60


def beszel_inventory(inventory: MultiHostConfig) -> MultiHostConfig:
    """The entries the Beszel cache still reads: a system id and no SSH alias. An entry with an alias is read by Core."""
    return MultiHostConfig(hosts=[e for e in inventory.hosts if e.system_id and not e.ssh],
                           local_system_id=inventory.local_system_id if any(e.system_id and not e.ssh for e in inventory.hosts)
                           else None)


def _f(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _port_text(ports) -> str | None:
    if not isinstance(ports, list):
        return None
    text = ", ".join(f"{p['host_port']}→{p['container']}" for p in ports
                     if isinstance(p, dict) and isinstance(p.get("host_port"), int) and isinstance(p.get("container"), str))
    return text[:256] or None


def ssh_card(entry, record: dict | None, now: float) -> HostCard:
    """One SSH host's card from its record in hosts.json (or none yet)."""
    def blank(reason, problem=None, cause=None):
        return HostCard(entry.ssh, entry.name, entry.link, True, unknown(reason, cause), containers_liveness=unknown(reason, cause),
                        source="ssh", ssh=entry.ssh, problem=problem)

    if not isinstance(record, dict) or record.get("ssh") != entry.ssh:
        return blank(NOT_COLLECTED)
    error = record.get("error") if isinstance(record.get("error"), str) else None
    problem = record.get("cause") if isinstance(record.get("cause"), str) else None
    cause = NOT_PERMITTED if problem in ("not_permitted", "this_host") else None
    attempted = _f(record.get("attempted_at"))
    if attempted is None or now - attempted > SSH_NOT_TRYING_AFTER:
        return blank("Core has not tried this host for " + (f"{int((now - attempted) // 60)} min" if attempted else "a while")
                     + ": is Core running?", "not_trying")
    read_at = _f(record.get("read_at"))
    if read_at is None:
        return blank(error or "no reading", problem, cause)
    if now - read_at > SSH_UNKNOWN_AFTER:
        return blank(f"no good reading for {int((now - read_at) // 60)} min" + (f": {error}" if error else ""), problem, cause)
    live = liveness(read_at, now=now, reason=error)
    engine = record.get("engine") if isinstance(record.get("engine"), dict) else {}
    m = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
    load = m.get("load")
    temp = _f(m.get("temp_c"))
    metrics = HostMetrics(cpu_pct=_f(m.get("cpu_pct")), mem_pct=_f(m.get("mem_pct")), mem_used_gib=_f(m.get("mem_used_gib")),
                          mem_total_gib=_f(m.get("mem_total_gib")), disk_pct=_f(m.get("disk_pct")),
                          disk_used_gib=_f(m.get("disk_used_gib")), disk_total_gib=_f(m.get("disk_total_gib")),
                          load=tuple(_f(x) for x in load) if isinstance(load, list) and load and all(_f(x) is not None for x in load) else None,
                          temps={"hottest zone": temp} if temp is not None else None)
    text = lambda v: v if isinstance(v, str) else None          # noqa: E731
    integer = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None          # noqa: E731
    details = HostDetails(cores=None, threads=integer(m.get("threads")) or integer(engine.get("ncpu")), arch=text(engine.get("arch")),
                          kernel=text(engine.get("kernel")), cpu_model=text(m.get("cpu_model")),
                          memory_bytes=integer(engine.get("mem_bytes")), os_name=text(engine.get("os")))
    raw = record.get("containers")
    containers = None if not isinstance(raw, list) else tuple(
        RemoteContainer(name=c["name"], image=text(c.get("image")), status=text(c.get("state")), health=text(c.get("health")),
                        ports=_port_text(c.get("ports")), restarts=integer(c.get("restarts")))
        for c in raw if isinstance(c, dict) and isinstance(c.get("name"), str))
    return HostCard(entry.ssh, entry.name, entry.link, True, live, details=details, metrics=metrics, containers=containers,
                    containers_liveness=live, status=error, source="ssh", ssh=entry.ssh, problem=problem if error else None,
                    locality_checked=record.get("locality_checked") is not False)


def load_hosts_file(path) -> dict[str, dict]:
    """hosts.json by SSH alias (unique in config); {} when it is absent or unreadable (every SSH host then reads as not collected yet)."""
    import json
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    hosts = data.get("hosts") if isinstance(data, dict) else None
    return {h["ssh"]: h for h in hosts or [] if isinstance(h, dict) and isinstance(h.get("ssh"), str)}


def _safe_card(entry, record, now) -> HostCard:
    """A record the page cannot read is that host's problem, never the page's (review, release A)."""
    try:
        return ssh_card(entry, record, now)
    except Exception:  # noqa: BLE001
        return HostCard(entry.ssh, entry.name, entry.link, True, unknown("Core's record for this host could not be read"),
                        containers_liveness=unknown("Core's record for this host could not be read"), source="ssh", ssh=entry.ssh)


class Fleet:
    """The Hosts page's view: SSH hosts from Core's file, the rest from the Beszel cache, in inventory order."""

    def __init__(self, inventory: MultiHostConfig, beszel: FleetCache, hosts_file, *, wall=time.time):
        self._inventory, self._beszel, self._file, self._wall = inventory, beszel, hosts_file, wall

    def view(self) -> FleetView:
        records = load_hosts_file(self._file) if any(e.ssh for e in self._inventory.hosts) else {}
        beszel = self._beszel.view() if any(e.system_id and not e.ssh for e in self._inventory.hosts) else FleetView((), None)
        by_id = {c.id: c for c in beszel.cards if c.configured}
        now = self._wall()
        cards = []
        for entry in self._inventory.hosts:
            if entry.ssh:
                cards.append(_safe_card(entry, records.get(entry.ssh), now))
            elif entry.system_id and entry.system_id in by_id:
                cards.append(by_id[entry.system_id])
            elif not entry.system_id:
                cards.append(HostCard(entry.name, entry.name, entry.link, True, unknown("link only: PE does not read this host"),
                                      source="link"))
        # A hub row for a host now read over SSH (it still has its system_id during the move) is that host, not an unconfigured
        # one: shown once (Codex, release A).
        listed = {e.system_id for e in self._inventory.hosts if e.system_id}
        cards += [c for c in beszel.cards if not c.configured and c.id not in listed]
        return FleetView(tuple(cards), beszel.locality_id, beszel.locality_reason)


def remote_summary(inventory: MultiHostConfig, hosts_file, *, wall=time.time) -> dict | None:
    """For the Overview tile: containers on the SSH hosts that are reading (current or stale) and how many hosts are unknown."""
    entries = [e for e in inventory.hosts if e.ssh]
    if not entries:
        return None
    records, now = load_hosts_file(hosts_file), wall()
    cards = [_safe_card(e, records.get(e.ssh), now) for e in entries]
    # Only a fresh reading is counted: a stale host's old containers are not "on" it now (review, release A).
    reading = [c for c in cards if c.liveness.state == "current" and c.containers is not None]
    # A stopped container may be stopped on purpose; judging that is release B's (findings). Here: unhealthy or restarting only.
    down = sum(1 for c in reading for x in c.containers if x.health == "unhealthy" or x.status in ("restarting", "dead"))
    return {"hosts": len(reading), "unknown": len(cards) - len(reading), "containers": sum(len(c.containers) for c in reading),
            "problems": down}
