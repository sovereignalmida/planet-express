"""The other hosts, as the Hosts page and the Overview show them (docs/designs/multi-host.md).

Core reads every host with an `ssh:` alias every minute and writes `state/hosts.json` (application/fleet_collector.py); this
module turns that file into cards. It does no I/O but reading that file, and never reaches a host itself. Beszel, the collector
before release C, is gone: an entry without an alias is shown with its link only.
"""

import json
import time
from dataclasses import dataclass

from config_schema import MultiHostConfig
from planet_express.core.hosts import (
    NOT_PERMITTED,
    HostDetails,
    HostMetrics,
    Liveness,
    RemoteContainer,
    liveness,
    unknown,
)


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
    source: str = "ssh"                  # or "link": an entry PE does not read
    ssh: str | None = None
    problem: str | None = None           # ssh: the collector's fixed cause word (this_host, not_permitted, no_contact, not_trying)
    locality_checked: bool = True        # ssh: whether the reading was compared with this host's own Docker engine


@dataclass(frozen=True)
class FleetView:
    cards: tuple[HostCard, ...]


# ── the SSH collector's hosts (release A): Core writes state/hosts.json, the page reads it ──────────────────────────────────────

NOT_COLLECTED = "Core has not read this host yet (the collector runs every minute while Core is up)"
# A reading older than this is not drawn at all, even as "last known" (review, release A: stale for ever hid a dead host).
SSH_UNKNOWN_AFTER = 15 * 60
# Core tries every host every minute; no try for this long means the collector itself is not running.
SSH_NOT_TRYING_AFTER = 5 * 60


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
    """The Hosts page's view: every configured host in inventory order, SSH hosts from Core's file, the rest as links."""

    def __init__(self, inventory: MultiHostConfig, hosts_file, *, wall=time.time):
        self._inventory, self._file, self._wall = inventory, hosts_file, wall

    def view(self) -> FleetView:
        records = load_hosts_file(self._file) if any(e.ssh for e in self._inventory.hosts) else {}
        now = self._wall()
        cards = []
        for entry in self._inventory.hosts:
            if entry.ssh:
                cards.append(_safe_card(entry, records.get(entry.ssh), now))
            else:
                cards.append(HostCard(entry.name, entry.name, entry.link, True,
                                      unknown("PE does not read this host: add an ssh alias to read its containers"),
                                      source="link"))
        return FleetView(tuple(cards))


def remote_summary(inventory: MultiHostConfig, hosts_file, *, wall=time.time) -> dict | None:
    """For the Overview tile: containers on the SSH hosts with a fresh reading, their problems by the same rule as the alerts and
    incidents (remote_health.container_issue), and how many hosts are not reading now."""
    from planet_express.application.remote_health import container_issue
    entries = [e for e in inventory.hosts if e.ssh]
    if not entries:
        return None
    records, now = load_hosts_file(hosts_file), wall()
    hosts = containers = problems = 0
    for entry in entries:
        card = _safe_card(entry, records.get(entry.ssh), now)
        raw = (records.get(entry.ssh) or {}).get("containers")
        if card.liveness.state != "current" or not isinstance(raw, list):
            continue
        hosts += 1
        rows = [c for c in raw if isinstance(c, dict) and isinstance(c.get("name"), str)]
        containers += len(rows)
        problems += sum(1 for c in rows if container_issue(c, now)[0])
    return {"hosts": hosts, "unknown": len(entries) - hosts, "containers": containers, "problems": problems}


MAX_OTHER_HOSTS_TEXT = 6000


def other_hosts_text(inventory=None, hosts_file=None, *, wall=time.time) -> str:
    """The other hosts for a Console turn's INVENTORY.md (release C): what Core last read, read-only. PE does not change other
    hosts, so the text says a plan cannot name them (and a plan's scope is checked against this host's stacks only)."""
    if inventory is None or hosts_file is None:
        import config
        inventory, hosts_file = config.MULTI_HOST, config.STATE_HOSTS
    entries = [e for e in inventory.hosts if e.ssh]
    if not entries:
        return ""
    records, now = load_hosts_file(hosts_file), wall()
    out = ["", "Other hosts (read over SSH by PE every minute; READ-ONLY: PE does not change them, and a plan must not name them):"]
    for entry in entries:
        card = _safe_card(entry, records.get(entry.ssh), now)
        if card.liveness.state == "unknown" or card.containers is None:
            out.append(f"- {entry.name}: not reading ({card.liveness.reason or 'no reading'})")
            continue
        age = f", read {int(card.liveness.age or 0)}s ago" if card.liveness.age is not None else ""
        out.append(f"- {entry.name} ({len(card.containers)} containers{age}):")
        for c in card.containers:
            bits = [c.status or "state not reported"]
            if c.health:
                bits.append(c.health)
            if c.restarts:
                bits.append(f"{c.restarts} restarts")
            if c.ports:
                bits.append(f"ports {c.ports}")
            out.append(f"  - {c.name}: {', '.join(bits)}")
    text = "\n".join(out) + "\n"
    return text if len(text) <= MAX_OTHER_HOSTS_TEXT else text[:MAX_OTHER_HOSTS_TEXT].rsplit("\n", 1)[0] + "\n  (cut short)\n"
